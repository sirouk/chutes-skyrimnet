import os
import json
import base64
import aiohttp
import hashlib
from contextvars import ContextVar
from configparser import ConfigParser
from typing import Any
from loguru import logger
from fastapi import Response, HTTPException, Request

from chutes.chute import Chute, NodeSelector
from tools.chute_wrappers import build_wrapper_image, register_service_launcher

# -----------------------------------------------------------------------------
# User Context for Siloing
# -----------------------------------------------------------------------------
_user_id_context: ContextVar[str] = ContextVar("user_id", default="default")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))

USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")

CHUTE_NAME = "zonos-whisper"
CHUTE_TAG = "tts-stt-v0.1.30"
CHUTE_BASE_IMAGE = "elbios/zonos-whisper:latest"
CHUTE_PYTHON_VERSION = "3.11"
CHUTE_GPU_COUNT = 1
CHUTE_MIN_VRAM_GB_PER_GPU = 24
CHUTE_SHUTDOWN_AFTER_SECONDS = 86400
CHUTE_CONCURRENCY = 2

SERVICE_PORTS = [7860, 8080]
ENTRYPOINT = "/usr/local/bin/docker-entrypoint.sh"

ZONOS_BASE = "http://localhost:7860"
WHISPER_BASE = "http://localhost:8080"

CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "ZONOS_MODEL_ID": "Zyphra/Zonos-v0.1-hybrid",
    "HF_HOME": "/cache/huggingface",
    "TORCH_HOME": "/cache/torch",
    "WHISPER_MODELS_DIR": "/cache/whispercpp",
    "MAX_IDLE_SECONDS": "86400",
}

CHUTE_TAGLINE = "elbios/zonos-whisper (Zyphra Zonos + Whisper.cpp)"
CHUTE_DOC = """
### Zonos Wrapper

This chute boots the upstream `elbios/zonos-whisper:latest` image and proxies its
service endpoints via Chutes cords with JSON-to-multipart translation.

#### Zonos Endpoints (TTS - Text to Speech)
- POST /api/generate_audio - Generate audio from text (Gradio)
- POST /api/predict - Gradio prediction API
- POST /queue/join - Gradio queue management
- POST /queue/status - Gradio queue status
- GET /file - Fetch generated files

#### Whisper Endpoints (STT - Speech to Text)
- POST /inference - Transcribe audio to text
- GET /load - Load/check model status
"""

# -----------------------------------------------------------------------------
# Image
# -----------------------------------------------------------------------------
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

@chute.middleware("http")
async def add_user_id_to_context(request: Request, call_next):
    user_id = request.query_params.get("silo_id")
    if not user_id:
        user_id = request.headers.get("X-Silo-ID", "default")

    token = _user_id_context.set(user_id)
    try:
        path = request.url.path
        if path != "/" and path.endswith("/"):
            request.scope["path"] = path.rstrip("/")
        return await call_next(request)
    finally:
        _user_id_context.reset(token)

# -----------------------------------------------------------------------------
# Proxy Helpers
# -----------------------------------------------------------------------------

def _encode_multipart(payload: dict) -> aiohttp.FormData:
    form = aiohttp.FormData()
    for key, value in payload.items():
        if value is None:
            continue
        if key.endswith("_base64"):
            field_name = key.rsplit("_base64", 1)[0]
            try:
                file_bytes = base64.b64decode(value)
                form.add_field(field_name, file_bytes, filename=f"{field_name}.wav", content_type="audio/wav")
            except Exception as e:
                logger.warning(f"Failed to decode {key}: {e}")
        elif isinstance(value, (dict, list)):
            form.add_field(key, json.dumps(value), content_type="application/json")
        else:
            form.add_field(key, str(value))
    return form

async def _consume_response(resp: aiohttp.ClientResponse, url: str) -> Any:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    body = await resp.read()
    parsed_json: Any = None
    if "application/json" in content_type:
        try:
            parsed_json = json.loads(body.decode("utf-8"))
        except Exception:
            logger.warning(f"Unable to decode JSON payload from {url}")

    if resp.status >= 400:
        detail = parsed_json if parsed_json is not None else body.decode("utf-8", errors="replace")
        logger.warning(f"Upstream {url} returned {resp.status}: {detail}")
        if resp.status >= 500:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "UpstreamError",
                    "upstream_status": resp.status,
                    "message": f"Upstream service returned error: {detail}",
                },
            )
        raise HTTPException(status_code=resp.status, detail=detail)

    if parsed_json is not None:
        return parsed_json

    return Response(content=body, media_type=content_type or "application/octet-stream", status_code=resp.status)

async def _proxy_get(base: str, path: str, params: dict = None) -> Any:
    url = f"{base}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                return await _consume_response(resp, url)
    except Exception as e:
        logger.error(f"Error proxying GET {url}: {e}")
        raise HTTPException(status_code=429, detail={"error": "ProxyError", "message": str(e)})

async def _proxy_post_multipart(base: str, path: str, payload: dict) -> Any:
    url = f"{base}{path}"
    try:
        form = _encode_multipart(payload)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form) as resp:
                return await _consume_response(resp, url)
    except Exception as e:
        logger.error(f"Error proxying POST (multipart) {url}: {e}")
        raise HTTPException(status_code=429, detail={"error": "ProxyError", "message": str(e)})

async def _proxy_post_json(base: str, path: str, payload: dict) -> Any:
    url = f"{base}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return await _consume_response(resp, url)
    except Exception as e:
        logger.error(f"Error proxying POST (json) {url}: {e}")
        raise HTTPException(status_code=429, detail={"error": "ProxyError", "message": str(e)})

# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------
register_service_launcher(chute, ENTRYPOINT, SERVICE_PORTS, timeout=300, soft_fail=True)

# -----------------------------------------------------------------------------
# Zonos Cords (port 7860)
# -----------------------------------------------------------------------------

@chute.cord(public_api_path="/api/generate_audio", public_api_method="POST")
async def generate_audio(self, args: dict) -> Any:
    """
    Zonos generate_audio endpoint.
    """
    has_base64 = any(k.endswith("_base64") for k in (args or {}).keys())
    if has_base64:
        return await _proxy_post_multipart(ZONOS_BASE, "/api/generate_audio", args or {})
    return await _proxy_post_json(ZONOS_BASE, "/api/generate_audio", args or {})

@chute.cord(public_api_path="/api/predict", public_api_method="POST")
async def predict(self, args: dict) -> Any:
    return await _proxy_post_json(ZONOS_BASE, "/api/predict", args or {})

@chute.cord(public_api_path="/queue/join", public_api_method="POST")
async def queue_join(self, args: dict) -> Any:
    return await _proxy_post_json(ZONOS_BASE, "/queue/join", args or {})

@chute.cord(public_api_path="/queue/status", public_api_method="POST")
async def queue_status(self, args: dict) -> Any:
    return await _proxy_post_json(ZONOS_BASE, "/queue/status", args or {})

@chute.cord(public_api_path="/file", public_api_method="GET")
async def get_file(self, args: dict) -> Any:
    return await _proxy_get(ZONOS_BASE, "/file", params=args)

# -----------------------------------------------------------------------------
# Whisper.cpp Cords (port 8080)
# -----------------------------------------------------------------------------

@chute.cord(public_api_path="/load", public_api_method="GET")
async def whisper_load_get(self, args: dict) -> Any:
    try:
        return await _proxy_get(WHISPER_BASE, "/load")
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"status": "unavailable", "detail": "Use POST /whisper_load to load a model."}
        raise

@chute.cord(public_api_path="/whisper_load", public_api_method="POST")
async def whisper_load_post(self, args: dict) -> Any:
    return await _proxy_post_json(WHISPER_BASE, "/load", args or {})

@chute.cord(public_api_path="/inference", public_api_method="POST")
async def whisper_inference(self, args: dict) -> Any:
    return await _proxy_post_multipart(WHISPER_BASE, "/inference", args or {})

if __name__ == "__main__":
    print(f"Chute: {chute.name}")
    for cord in chute.cords:
        print(f"  {cord._public_api_method:6} {cord._public_api_path}")
