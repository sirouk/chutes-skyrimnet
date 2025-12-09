import os
import subprocess
import asyncio
from configparser import ConfigParser
from loguru import logger
from chutes.chute import Chute, NodeSelector
from chutes.image import Image

# Load auth
chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))
USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")

# Config from deploy_xtts_whisper.py (bumped version)
CHUTE_NAME = "xtts-whisper"
CHUTE_TAG = "tts-stt-v0.1.12"
CHUTE_TAGLINE = "elbios/xtts-whisper (XTTS + Whisper.cpp)"
CHUTE_DOC = """
### XTTS + Whisper Wrapper

This chute provides the XTTS and Whisper services, compatible with the `elbios/xtts-whisper` interface.

#### XTTS Endpoints (TTS - Text to Speech)
- POST /tts_to_audio - Generate audio from text
- POST /tts_stream - Stream audio generation
- GET /speakers_list - List available speakers
- POST /clone_speaker - Clone a speaker voice
- POST /set_tts_settings - Configure TTS settings

#### Whisper Endpoints (STT - Speech to Text)
- POST /inference - Transcribe audio to text
- GET /load - Load/check model status
"""

# =============================================================================
# 1. Image Configuration (Manual Build)
# =============================================================================
# This replicates the setup from elbios/xtts-whisper starting from a clean slate.
image = (
    Image(
        username=USERNAME,
        name=CHUTE_NAME,
        tag=CHUTE_TAG,
        readme=CHUTE_DOC,
    )
    .from_base("parachutes/python:3.12")
    
    # Install system dependencies
    .run_command("apt-get update && apt-get install -y git make build-essential gcc g++ libsndfile1-dev libasound2-dev ffmpeg wget curl && rm -rf /var/lib/apt/lists/*")
    
    # Install Python dependencies
    .run_command("pip install --upgrade pip")
    .run_command("pip install xtts-api-server loguru aiohttp")
    
    # Build Whisper.cpp (Server)
    .run_command("git clone https://github.com/ggerganov/whisper.cpp /opt/whispercpp")
    # Attempt CUDA build, fallback to CPU if nvcc missing
    .run_command("cd /opt/whispercpp && make clean && (make -j server GGML_CUDA=1 || make -j server)")
    
    # Create cache directories
    .run_command("mkdir -p /cache/huggingface /cache/torch /cache/whispercpp /cache/xtts")
    
    # Environment Variables
    .with_env("HF_HOME", "/cache/huggingface")
    .with_env("TORCH_HOME", "/cache/torch")
    .with_env("WHISPER_MODELS_DIR", "/cache/whispercpp")
    .with_env("WHISPER_MODEL", "large-v3-turbo")
    .with_env("COQUI_TOS_AGREED", "1")
    .with_env("XTTS_MODEL_ID", "tts_models/multilingual/multi-dataset/xtts_v2")
)

# =============================================================================
# 2. Chute Configuration
# =============================================================================
chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    tagline=CHUTE_TAGLINE,
    readme=CHUTE_DOC,
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16),
    concurrency=6,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)

# =============================================================================
# 3. Startup Logic (Service Launcher)
# =============================================================================
@chute.on_startup()
async def start_services(self):
    """
    Manually launch the services (XTTS + Whisper) and wait for them to be ready.
    This replicates the logic of docker-entrypoint.sh.
    """
    # Helper for waiting for ports
    async def _wait_for_ports(ports, host="127.0.0.1", timeout=600):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            ready = 0
            for port in ports:
                try:
                    _, writer = await asyncio.open_connection(host, port)
                    writer.close()
                    await writer.wait_closed()
                    ready += 1
                except OSError:
                    pass
            if ready == len(ports):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Timed out waiting for ports: {ports}")
            await asyncio.sleep(1)

    # --- A. Download Whisper Model ---
    model_name = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    model_path = f"/cache/whispercpp/ggml-{model_name}.bin"
    if not os.path.exists(model_path):
        logger.info(f"Downloading Whisper model: {model_name}")
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model_name}.bin"
        subprocess.run(["wget", "-O", model_path, url], check=True)
    
    # --- B. Start Whisper Server ---
    logger.info("Starting Whisper Server on port 8080...")
    whisper_cmd = [
        "/opt/whispercpp/server",
        "--host", "127.0.0.1",
        "--port", "8080",
        "--model", model_path,
        # Add --gpu 0 if compiled with CUDA, otherwise this flag might error if binary doesn't support it? 
        # Standard whisper.cpp binary ignores unknown flags? No.
        # Safe bet: check if we have GPUs.
    ]
    # We'll assume GPU build succeeded.
    whisper_proc = subprocess.Popen(whisper_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    # --- C. Start XTTS Server ---
    logger.info("Starting XTTS Server on port 8020...")
    # XTTS downloads models automatically to ~/.local/share/tts or similar. 
    # We let it handle itself or set env vars if needed.
    xtts_cmd = [
        "python", "-m", "xtts_api_server",
        "--listen",
        "--port", "8020",
        "-d", "cuda"
    ]
    xtts_proc = subprocess.Popen(xtts_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # --- D. Log Output Helper ---
    async def log_stream(proc, name):
        while proc.poll() is None:
            line = await asyncio.get_running_loop().run_in_executor(None, proc.stdout.readline)
            if line:
                logger.info(f"[{name}] {line.rstrip()}")
            await asyncio.sleep(0.01)
    
    asyncio.create_task(log_stream(whisper_proc, "whisper"))
    asyncio.create_task(log_stream(xtts_proc, "xtts"))

    # --- E. Wait for Ports ---
    logger.info("Waiting for services to come up...")
    await _wait_for_ports([8020, 8080])
    logger.success("All services ready!")

# =============================================================================
# 4. Define Cords (Routes)
# =============================================================================

# XTTS Routes (Port 8020)
@chute.cord(path="get_speakers_list", public_api_path="/speakers_list", method="GET", passthrough=True, passthrough_port=8020)
def get_speakers_list(data): pass

@chute.cord(path="get_speakers", public_api_path="/speakers", method="GET", passthrough=True, passthrough_port=8020)
def get_speakers(data): pass

@chute.cord(path="get_languages", public_api_path="/languages", method="GET", passthrough=True, passthrough_port=8020)
def get_languages(data): pass

@chute.cord(path="get_folders", public_api_path="/get_folders", method="GET", passthrough=True, passthrough_port=8020)
def get_folders(data): pass

@chute.cord(path="get_models_list", public_api_path="/get_models_list", method="GET", passthrough=True, passthrough_port=8020)
def get_models_list(data): pass

@chute.cord(path="get_tts_settings", public_api_path="/get_tts_settings", method="GET", passthrough=True, passthrough_port=8020)
def get_tts_settings(data): pass

@chute.cord(path="get_sample", public_api_path="/sample/{file_name}", method="GET", passthrough=True, passthrough_port=8020)
def get_sample(data): pass

@chute.cord(path="set_output", public_api_path="/set_output", method="POST", passthrough=True, passthrough_port=8020)
def set_output(data): pass

@chute.cord(path="set_speaker_folder", public_api_path="/set_speaker_folder", method="POST", passthrough=True, passthrough_port=8020)
def set_speaker_folder(data): pass

@chute.cord(path="switch_model", public_api_path="/switch_model", method="POST", passthrough=True, passthrough_port=8020)
def switch_model(data): pass

@chute.cord(path="set_tts_settings", public_api_path="/set_tts_settings", method="POST", passthrough=True, passthrough_port=8020)
def set_tts_settings(data): pass

@chute.cord(path="tts_stream", public_api_path="/tts_stream", method="GET", passthrough=True, passthrough_port=8020, stream=True)
def tts_stream(data): pass

@chute.cord(path="tts_to_audio", public_api_path="/tts_to_audio/", method="POST", passthrough=True, passthrough_port=8020)
def tts_to_audio(data): pass

@chute.cord(path="tts_to_file", public_api_path="/tts_to_file", method="POST", passthrough=True, passthrough_port=8020)
def tts_to_file(data): pass

@chute.cord(path="create_latents", public_api_path="/create_latents", method="POST", passthrough=True, passthrough_port=8020)
def create_latents(data): pass

@chute.cord(path="store_latents", public_api_path="/store_latents", method="POST", passthrough=True, passthrough_port=8020)
def store_latents(data): pass

@chute.cord(path="create_and_store_latents", public_api_path="/create_and_store_latents", method="POST", passthrough=True, passthrough_port=8020)
def create_and_store_latents(data): pass

# Whisper Routes (Port 8080)
@chute.cord(path="whisper_load_get", public_api_path="/load", method="GET", passthrough=True, passthrough_port=8080)
def whisper_load_get(data): pass

@chute.cord(path="whisper_load_post", public_api_path="/load", method="POST", passthrough=True, passthrough_port=8080)
def whisper_load_post(data): pass

@chute.cord(path="whisper_inference", public_api_path="/inference", method="POST", passthrough=True, passthrough_port=8080)
def whisper_inference(data): pass

@chute.cord(path="whisper_transcriptions", public_api_path="/v1/audio/transcriptions", method="POST", passthrough=True, passthrough_port=8080, passthrough_path="/inference")
def whisper_transcriptions(data): pass
