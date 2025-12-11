import os
from configparser import ConfigParser

from chutes.chute import Chute, NodeSelector

from tools.chute_wrappers import (
    build_wrapper_image,
    load_route_manifest,
    register_passthrough_routes,
    register_service_launcher,
)

chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))

USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")

# Chute Configuration
CHUTE_NAME = "vibevoice-whisper"
CHUTE_TAG = "tts-stt-v0.1.11"
CHUTE_BASE_IMAGE = "elbios/vibevoice-whisper:latest"
CHUTE_PYTHON_VERSION = "3.11"
CHUTE_GPU_COUNT = 1
CHUTE_MIN_VRAM_GB_PER_GPU = 16  # Chutes minimum; VibeVoice ~3GB + Whisper ~1.5GB
CHUTE_SHUTDOWN_AFTER_SECONDS = 86400
CHUTE_CONCURRENCY = 5  # Small model

SERVICE_PORTS = [7860, 8080]
DEFAULT_SERVICE_PORT = SERVICE_PORTS[0]
ENTRYPOINT = "/usr/local/bin/docker-entrypoint.sh"

# Chute environment variables (used during discovery and runtime)
CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "VIBEVOICE_MODEL_ID": "microsoft/VibeVoice-1.5B",
    # Cache directories (/cache owned by chutes user)
    "HF_HOME": "/cache/huggingface",
    "TORCH_HOME": "/cache/torch",
    "WHISPER_MODELS_DIR": "/cache/whispercpp",
    # Disable base image's Vast.ai watchdog (Chutes has its own shutdown_after_seconds)
    "MAX_IDLE_SECONDS": "86400",
}

# Static routes for whisper.cpp (port 8080) - doesn't expose OpenAPI
# Gradio routes (port 7860) are auto-discovered - see routes.json
# https://github.com/ggml-org/whisper.cpp/tree/master/examples/server
CHUTE_STATIC_ROUTES = [
    {"port": 8080, "method": "GET", "path": "/load"},
    {"port": 8080, "method": "POST", "path": "/inference"},
   #{"port": 8080, "method": "POST", "path": "/v1/audio/transcriptions"}, # if --inference-path is used in whisper.cpp
]
CHUTE_TAGLINE = "elbios/vibevoice-whisper (VibeVoice-1.5B + Whisper.cpp)"
CHUTE_DOC = """
### VibeVoice Wrapper

This chute boots `elbios/vibevoice-whisper:latest` and proxies the Gradio + Whisper servers.
No custom TTS logic remains in this repo — requests are simply forwarded to the vendor image.

#### Gradio Endpoints (TTS - Text to Speech)
- POST /api/generate_audio - Generate audio from text
- POST /queue/join - Join the processing queue
- POST /queue/status - Check queue status

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
        readme=CHUTE_DOC,
        env=CHUTE_ENV,
    )
    .add(source="tools", dest="/app/tools")
    # VibeVoice creates models/inputs/outputs/temp dirs in /opt/vibevoice at runtime
    .set_user("root")
    .run_command("chmod -R a+rwx /opt/vibevoice 2>/dev/null || true")
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

# Start the wrapped services (VibeVoice + Whisper) - base image entrypoint is overridden by chutes run
register_service_launcher(chute, ENTRYPOINT, SERVICE_PORTS, timeout=180, soft_fail=True)


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
