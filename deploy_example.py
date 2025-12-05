"""
Generic Chute Deployment Template

This template wraps a Docker image for deployment on Chutes.ai.
Copy this file and customize the configuration section for your service.

Usage:
  1. Copy to deploy_<yourservice>.py
  2. Update CHUTE_* variables for your service
  3. Run route discovery: ./deploy.sh -> Build -> select module -> run discovery
  4. Build and deploy: ./deploy.sh -> Build/Deploy
"""
import os
from configparser import ConfigParser

from chutes.chute import Chute, NodeSelector

from tools.chute_wrappers import (
    build_wrapper_image,
    load_route_manifest,
    register_passthrough_routes,
    register_startup_wait,
    register_health_check,
)

# =============================================================================
# Auth Configuration (auto-loaded from ~/.chutes/config.ini or environment)
# =============================================================================
chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))
USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")

# =============================================================================
# Chute Configuration - CUSTOMIZE THESE FOR YOUR SERVICE
# =============================================================================

# Basic identification
CHUTE_NAME = "example-service"
CHUTE_TAG = "v0.1.0"
CHUTE_BASE_IMAGE = os.getenv("CHUTE_BASE_IMAGE", "your-registry/your-image:latest")

# Human-readable metadata
CHUTE_TAGLINE = "Example Service (customize this)"
CHUTE_DOC = """
### Example Service

Describe your service here. This appears in the Chutes.ai UI.

#### Endpoints
- GET /health - Health check
- POST /your-endpoint - Your endpoint description
"""

# Chute environment variables (passed to container during discovery and runtime)
# Add any env vars your base image needs
CHUTE_ENV = {
    # "MODEL_NAME": "your-model",
    # "WHISPER_MODEL": "large-v3-turbo",
}

# Static routes for services without OpenAPI (merged with discovered routes, deduped)
# Key order: port, method, path, target_path
CHUTE_STATIC_ROUTES = [
    # {"port": 8080, "method": "GET", "path": "/load", "target_path": "/load"},
    # {"port": 8080, "method": "POST", "path": "/inference", "target_path": "/inference"},
]

# =============================================================================
# Resource Configuration - Adjust based on your service requirements
# =============================================================================
CHUTE_GPU_COUNT = int(os.getenv("CHUTE_GPU_COUNT", "1"))
CHUTE_MIN_VRAM_GB_PER_GPU = int(os.getenv("CHUTE_MIN_VRAM_GB_PER_GPU", "16"))  # Chutes minimum is 16GB
CHUTE_SHUTDOWN_AFTER_SECONDS = int(os.getenv("CHUTE_SHUTDOWN_AFTER_SECONDS", "3600"))
CHUTE_CONCURRENCY = int(os.getenv("CHUTE_CONCURRENCY", "1"))
CHUTE_PYTHON_VERSION = os.getenv("CHUTE_PYTHON_VERSION", "3.10")  # System Python for chutes-inspecto.so

# =============================================================================
# Network Configuration
# =============================================================================
LOCAL_HOST = "127.0.0.1"
# Comma-separated list of ports your service exposes
SERVICE_PORTS = [int(p.strip()) for p in os.getenv("CHUTE_PORTS", "8080").split(",") if p.strip()]
if not SERVICE_PORTS:
    raise RuntimeError("CHUTE_PORTS must specify at least one port")
DEFAULT_SERVICE_PORT = SERVICE_PORTS[0]

# Entrypoint script in the base image (if any)
ENTRYPOINT = os.getenv("CHUTE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")

# =============================================================================
# Image Build Configuration
# =============================================================================
# build_wrapper_image sets up a Debian-based image with Chutes runtime deps.
# It extends your CHUTE_BASE_IMAGE with necessary tooling.

image = build_wrapper_image(
    username=USERNAME,
    name=CHUTE_NAME,
    tag=CHUTE_TAG,
    base_image=CHUTE_BASE_IMAGE,
    python_version=CHUTE_PYTHON_VERSION,
)

# =============================================================================
# Chute Definition
# =============================================================================

chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    tagline=CHUTE_TAGLINE,
    readme=CHUTE_DOC,
    image=image,
    node_selector=NodeSelector(
        gpu_count=CHUTE_GPU_COUNT,
        min_vram_gb_per_gpu=CHUTE_MIN_VRAM_GB_PER_GPU,
    ),
    concurrency=CHUTE_CONCURRENCY,
    allow_external_egress=True,
    shutdown_after_seconds=CHUTE_SHUTDOWN_AFTER_SECONDS,
)

# Register routes, startup wait, and health check
# Routes from manifest are merged with static routes (duplicates logged and skipped)
register_passthrough_routes(chute, load_route_manifest(static_routes=CHUTE_STATIC_ROUTES), DEFAULT_SERVICE_PORT)
register_startup_wait(chute, SERVICE_PORTS, LOCAL_HOST)
register_health_check(chute, SERVICE_PORTS, LOCAL_HOST)


# =============================================================================
# Local Testing
# =============================================================================

if __name__ == "__main__":
    print(f"Chute: {chute.name}")
    print(f"Image: {image.name}:{image.tag}")
    print(f"Base Image: {CHUTE_BASE_IMAGE}")
    print(f"Service Ports: {SERVICE_PORTS}")
    print(f"GPU: {CHUTE_GPU_COUNT}x (min {CHUTE_MIN_VRAM_GB_PER_GPU}GB VRAM)")
    print(f"Concurrency: {CHUTE_CONCURRENCY}")
    print(f"\nEnvironment:")
    for k, v in CHUTE_ENV.items():
        print(f"  {k}={v}")
    print(f"\nCords:")
    for cord in chute.cords:
        print(f"  {cord._public_api_method:6} {cord._public_api_path} -> port {cord._passthrough_port or 'N/A'}")
