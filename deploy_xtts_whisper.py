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
CHUTE_GPU_COUNT = int(os.getenv("CHUTE_GPU_COUNT", "1"))
CHUTE_MIN_VRAM_GB_PER_GPU = int(os.getenv("CHUTE_MIN_VRAM_GB_PER_GPU", "16"))  # Chutes minimum; XTTS ~1.5GB + Whisper ~1.5GB
CHUTE_SHUTDOWN_AFTER_SECONDS = int(os.getenv("CHUTE_SHUTDOWN_AFTER_SECONDS", "86400"))
CHUTE_CONCURRENCY = int(os.getenv("CHUTE_CONCURRENCY", "6"))  # Lightweight model

SERVICE_PORTS = [int(p.strip()) for p in os.getenv("CHUTE_PORTS", "8020,8080").split(",") if p.strip()]
if not SERVICE_PORTS:
    raise RuntimeError("CHUTE_PORTS must specify at least one port")
DEFAULT_SERVICE_PORT = SERVICE_PORTS[0]
ENTRYPOINT = os.getenv("CHUTE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")

CHUTE_BASE_IMAGE = os.getenv("CHUTE_BASE_IMAGE", "elbios/xtts-whisper:latest")
CHUTE_PYTHON_VERSION = os.getenv("CHUTE_PYTHON_VERSION", "3.11")
CHUTE_NAME = "xtts-whisper"
CHUTE_TAG = "tts-stt-v0.1.16"

# Chute environment variables (used during discovery and runtime)
CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "XTTS_MODEL_ID": "tts_models/multilingual/multi-dataset/xtts_v2",
    # Cache directories (/cache owned by chutes user)
    "HF_HOME": "/cache/huggingface",
    "TORCH_HOME": "/cache/torch",
    "WHISPER_MODELS_DIR": "/cache/whispercpp",
    # Disable base image's Vast.ai watchdog (Chutes has its own shutdown_after_seconds)
    "MAX_IDLE_SECONDS": "86400",
}

# Static routes for whisper.cpp (port 8080) - doesn't expose OpenAPI
# XTTS routes (port 8020) are auto-discovered - see routes.json
# https://github.com/ggml-org/whisper.cpp/tree/master/examples/server
CHUTE_STATIC_ROUTES = [
    {"port": 8080, "method": "GET", "path": "/load"},
    {"port": 8080, "method": "POST", "path": "/inference"},
    {"port": 8080, "method": "POST", "path": "/v1/audio/transcriptions"},
]
CHUTE_TAGLINE = "elbios/xtts-whisper (XTTS + Whisper.cpp)"
CHUTE_DOC = """
### XTTS + Whisper Wrapper

This chute boots the upstream `elbios/xtts-whisper:latest` image and proxies its
service endpoints via Chutes cords.

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
    .add(source="deploy_xtts_whisper.routes.json", dest="/app/deploy_xtts_whisper.routes.json")
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

# Start the wrapped services (XTTS + Whisper) - base image entrypoint is overridden by chutes run
register_service_launcher(chute, ENTRYPOINT, SERVICE_PORTS, timeout=120, soft_fail=True)


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
