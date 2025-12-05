import os
import signal
import subprocess
from configparser import ConfigParser

from chutes.chute import Chute, NodeSelector

from tools.chute_wrappers import (
    build_wrapper_image,
    load_route_manifest,
    parse_service_ports,
    register_passthrough_routes,
    wait_for_services,
    probe_services,
)

chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))
USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")
CHUTE_GPU_COUNT = int(os.getenv("CHUTE_GPU_COUNT", "1"))
CHUTE_MIN_VRAM_GB_PER_GPU = int(os.getenv("CHUTE_MIN_VRAM_GB_PER_GPU", "16"))  # Chutes minimum; VibeVoice ~3GB + Whisper ~1.5GB
CHUTE_SHUTDOWN_AFTER_SECONDS = int(os.getenv("CHUTE_SHUTDOWN_AFTER_SECONDS", "3600"))
CHUTE_CONCURRENCY = int(os.getenv("CHUTE_CONCURRENCY", "5"))  # Small model

LOCAL_HOST = "127.0.0.1"
SERVICE_PORTS = [int(p.strip()) for p in os.getenv("CHUTE_PORTS", "7860,8080").split(",") if p.strip()]
if not SERVICE_PORTS:
    raise RuntimeError("CHUTE_PORTS must specify at least one port")
DEFAULT_SERVICE_PORT = SERVICE_PORTS[0]
ENTRYPOINT = os.getenv("CHUTE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")

CHUTE_BASE_IMAGE = os.getenv("CHUTE_BASE_IMAGE", "elbios/vibevoice-whisper:latest")
CHUTE_PYTHON_VERSION = os.getenv("CHUTE_PYTHON_VERSION", "3.10")
CHUTE_NAME = "vibevoice-whisper"
CHUTE_TAG = "tts-stt-v0.1.4"

# Chute environment variables (used during discovery and runtime)
CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "VIBEVOICE_MODEL_ID": "microsoft/VibeVoice-1.5B",
}

# Static routes for whisper.cpp (port 8080) - doesn't expose OpenAPI
# Note: /v1/audio/transcriptions is already in deploy_vibevoice_whisper.routes.json
# Gradio routes (port 7860) are auto-discovered - see routes.json
# https://github.com/ggml-org/whisper.cpp/tree/master/examples/server
CHUTE_STATIC_ROUTES = [
    {"port": 8080, "method": "GET", "path": "/load", "target_path": "/load"},
    {"port": 8080, "method": "POST", "path": "/inference", "target_path": "/inference"},
    {"port": 8080, "method": "POST", "path": "/load", "target_path": "/load"},
]
CHUTE_TAGLINE = "elbios/vibevoice-whisper (VibeVoice-1.5B + Whisper.cpp)"
CHUTE_README = "Wrapper image that ships the latest elbios/vibevoice-whisper for deployment on Chutes."
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

image = build_wrapper_image(
    username=USERNAME,
    name=CHUTE_NAME,
    tag=CHUTE_TAG,
    base_image=CHUTE_BASE_IMAGE,
    python_version=CHUTE_PYTHON_VERSION,
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


@chute.on_startup()
async def boot(self):
    """
    Check if VibeVoice and Whisper services are ready.
    """
    await wait_for_services(SERVICE_PORTS, host=LOCAL_HOST, timeout=600)


# @chute.on_shutdown()
# async def shutdown(self):
#     """Gracefully terminate the entrypoint process."""
#     logger.info("✅ Services stopped.")


# =============================================================================
# Health Check
# =============================================================================

@chute.cord(public_api_path="/health", public_api_method="GET", method="GET")
async def health_check(self) -> dict:
    """Check if both services are healthy."""
    errors = await probe_services(SERVICE_PORTS, host=LOCAL_HOST, timeout=5)
    if errors:
        return {"status": "unhealthy", "errors": errors}
    return {"status": "healthy", "ports": SERVICE_PORTS}


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
