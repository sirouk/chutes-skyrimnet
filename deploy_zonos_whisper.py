import os
import signal
import subprocess
from configparser import ConfigParser

from chutes.chute import Chute, NodeSelector

from tools.chute_wrappers import (
    build_wrapper_image,
    load_route_manifest,
    register_passthrough_routes,
)

chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))
USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")
CHUTE_GPU_COUNT = int(os.getenv("CHUTE_GPU_COUNT", "1"))
CHUTE_MIN_VRAM_GB_PER_GPU = int(os.getenv("CHUTE_MIN_VRAM_GB_PER_GPU", "24"))  # Zonos 8.8B ~18GB + Whisper ~1.5GB
CHUTE_SHUTDOWN_AFTER_SECONDS = int(os.getenv("CHUTE_SHUTDOWN_AFTER_SECONDS", "3600"))
CHUTE_CONCURRENCY = int(os.getenv("CHUTE_CONCURRENCY", "2"))  # Large model

SERVICE_PORTS = [int(p.strip()) for p in os.getenv("CHUTE_PORTS", "7860,8080").split(",") if p.strip()]
if not SERVICE_PORTS:
    raise RuntimeError("CHUTE_PORTS must specify at least one port")
DEFAULT_SERVICE_PORT = SERVICE_PORTS[0]
ENTRYPOINT = os.getenv("CHUTE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")

CHUTE_BASE_IMAGE = os.getenv("CHUTE_BASE_IMAGE", "elbios/zonos-whisper:latest")
CHUTE_PYTHON_VERSION = os.getenv("CHUTE_PYTHON_VERSION", "3.11")
CHUTE_NAME = "zonos-whisper"
CHUTE_TAG = "tts-stt-v0.1.11"

# Chute environment variables (used during discovery and runtime)
CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "ZONOS_MODEL_ID": "Zyphra/Zonos-v0.1-hybrid",
    # Cache directories (/cache owned by chutes user)
    "HF_HOME": "/cache/huggingface",
    "TORCH_HOME": "/cache/torch",
    "WHISPER_MODELS_DIR": "/cache/whispercpp",
    # Disable base image's Vast.ai watchdog (Chutes has its own shutdown_after_seconds)
    "MAX_IDLE_SECONDS": "31536000",
}

# Static routes for whisper.cpp (port 8080) - doesn't expose OpenAPI
# Gradio routes (port 7860) are auto-discovered - see routes.json
# https://github.com/ggml-org/whisper.cpp/tree/master/examples/server
CHUTE_STATIC_ROUTES = [
    {"port": 8080, "method": "GET", "path": "/load", "target_path": "/load"},
    {"port": 8080, "method": "POST", "path": "/inference", "target_path": "/inference"},
    {"port": 8080, "method": "POST", "path": "/load", "target_path": "/load"},
]
CHUTE_TAGLINE = "elbios/zonos-whisper (Zyphra Zonos + Whisper.cpp)"
CHUTE_README = "Wrapper image that ships the latest elbios/zonos-whisper for deployment on Chutes."
CHUTE_DOC = """
### Zonos Wrapper

This chute runs `elbios/zonos-whisper:latest` unchanged and proxies the local Gradio +
Whisper servers so clients can keep using the Zonos UI/API without uploading new code.

#### Gradio Endpoints (TTS - Text to Speech)
- POST /api/generate_audio - Generate audio from text
- POST /api/predict/ - Prediction API
- POST /queue/join - Join the processing queue
- POST /queue/status - Check queue status
- GET /file - Fetch generated files

#### Whisper Endpoints (STT - Speech to Text)
- POST /v1/audio/transcriptions - Transcribe audio to text (proxies to /inference)
"""

# =============================================================================
# Image Build Configuration
# =============================================================================
# These build steps set up a Debian-based image for Chutes runtime.
# Adjust CHUTE_BASE_IMAGE to reuse with other elbios/* or compatible images.

image = (
    build_wrapper_image(
        username=USERNAME,
        name=CHUTE_NAME,
        tag=CHUTE_TAG,
        base_image=CHUTE_BASE_IMAGE,
        python_version=CHUTE_PYTHON_VERSION,
        env=CHUTE_ENV,
    )
    .add(source="tools", dest="/app/tools")
    # Zonos may create temp/output dirs at runtime
    .set_user("root")
    .run_command("chmod -R a+rwx /opt/Zonos 2>/dev/null || true")
    .set_user("chutes")
)

chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    tagline=CHUTE_TAGLINE,
    readme=CHUTE_DOC,
    image=image,
    node_selector=NodeSelector(gpu_count=CHUTE_GPU_COUNT, min_vram_gb_per_gpu=CHUTE_MIN_VRAM_GB_PER_GPU),
    concurrency=CHUTE_CONCURRENCY,
    allow_external_egress=True,
    shutdown_after_seconds=CHUTE_SHUTDOWN_AFTER_SECONDS,
)

register_passthrough_routes(chute, load_route_manifest(static_routes=CHUTE_STATIC_ROUTES), DEFAULT_SERVICE_PORT)


# =============================================================================
# LOCAL TESTING
# =============================================================================
if __name__ == "__main__":
    print(f"Chute: {chute.name}")
    print(f"Image: {image.name}:{image.tag}")
    print(f"Service Ports: {SERVICE_PORTS}")
    print("\nCords:")
    for cord in chute.cords:
        print(f"  {cord._public_api_method:6} {cord._public_api_path} -> port {cord._passthrough_port or 'N/A'}")
