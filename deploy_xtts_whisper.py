"""
XTTS + Whisper Chute with JSON-to-Multipart Proxy

This chute wraps the elbios/xtts-whisper image. The upstream XTTS/whisper services
expect multipart form-data, but the Chutes API only supports JSON payloads.
Each cord manually translates JSON into multipart before proxying to the local service.

Base64-encoded fields (e.g., `wav_file_base64`) are decoded and sent as file uploads.
"""

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

CHUTE_NAME = "xtts-whisper"
CHUTE_TAG = "tts-stt-v0.1.30"
CHUTE_BASE_IMAGE = "elbios/xtts-whisper:latest"
CHUTE_PYTHON_VERSION = "3.11"
CHUTE_GPU_COUNT = 1
CHUTE_MIN_VRAM_GB_PER_GPU = 16
CHUTE_SHUTDOWN_AFTER_SECONDS = 86400
CHUTE_CONCURRENCY = 6

SERVICE_PORTS = [8020, 8080]
ENTRYPOINT = "/usr/local/bin/docker-entrypoint.sh"

XTTS_BASE = "http://localhost:8020"
WHISPER_BASE = "http://localhost:8080"

CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "XTTS_MODEL_ID": "tts_models/multilingual/multi-dataset/xtts_v2",
    "HF_HOME": "/cache/huggingface",
    "TORCH_HOME": "/cache/torch",
    "WHISPER_MODELS_DIR": "/cache/whispercpp",
    "MAX_IDLE_SECONDS": "86400",
}

CHUTE_TAGLINE = "elbios/xtts-whisper (XTTS + Whisper.cpp)"
CHUTE_DOC = """
### XTTS + Whisper Wrapper

This chute boots the upstream `elbios/xtts-whisper:latest` image and proxies its
service endpoints via Chutes cords with JSON-to-multipart translation.

#### XTTS Endpoints (TTS - Text to Speech)
- POST /tts_to_audio - Generate audio from text
- POST /create_and_store_latents - Clone speaker voice from audio
- GET /speakers_list - List available speakers
- GET /languages - List supported languages

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
    #.add(source="deploy_xtts_whisper.routes.json", dest="/app/deploy_xtts_whisper.routes.json")
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
    """Middleware to capture Silo ID for siloing."""
    # Users should provide 'silo_id' in query params or as a header (if using SDK).
    # Note: Authorization and X-Chutes-UserID headers are stripped by the gateway.
    user_id = request.query_params.get("silo_id")
    if not user_id:
        user_id = request.headers.get("X-Silo-ID", "default")

    token = _user_id_context.set(user_id)
    try:
        # Standardize paths by stripping trailing slashes for routing
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
    """
    Convert a JSON payload to multipart form-data.
    Fields ending in _base64 are decoded and attached as file uploads.
    """
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
            import json
            form.add_field(key, json.dumps(value), content_type="application/json")
        else:
            form.add_field(key, str(value))
    return form


async def _consume_response(resp: aiohttp.ClientResponse, url: str) -> Any:
    """Return JSON payloads directly, propagate upstream status codes."""
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
        # If it's a 5xx from upstream, we turn it into a 429 to avoid the platform killing this instance.
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
    """Simple GET proxy returning JSON or raw bytes wrapped in Response."""
    url = f"{base}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                return await _consume_response(resp, url)
    except aiohttp.ClientError as e:
        logger.error(f"Connection error to upstream {url}: {e}")
        # Use 429 to signal "busy/starting" so platform doesn't kill us
        raise HTTPException(
            status_code=429,
            detail={
                "error": "UpstreamUnreachable",
                "message": f"Upstream service at {url} is starting or unreachable: {e}",
            },
        )
    except Exception as e:
        logger.exception(f"Unexpected error proxying GET {url}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "InternalProxyError",
                "message": f"Unexpected error proxying request: {str(e)}",
            },
        )


def _get_user_id() -> str:
    """
    Extract hashed user ID from context.
    Falls back to 'default' if not present (e.g. local testing).
    """
    user_id = _user_id_context.get()
    if user_id == "default":
        return "default"
    # Return hex hash for safe folder naming
    return hashlib.md5(user_id.encode()).hexdigest()[:16]


async def _proxy_post_multipart(base: str, path: str, payload: dict) -> Any:
    """POST with JSON->multipart conversion, return JSON or raw bytes wrapped in Response."""
    url = f"{base}{path}"
    
    try:
        # Silo speaker folder if user ID is available
        user_silo = _get_user_id()
        if "speaker_name" in payload:
            payload["speaker_name"] = f"{user_silo}_{payload['speaker_name']}"
            logger.debug(f"Siloing speaker_name to: {payload['speaker_name']}")
        if "speaker_wav" in payload:
            # Only prefix if it's a name, not a path or latent id
            if not payload["speaker_wav"].startswith("/") and "|" not in payload["speaker_wav"]:
                payload["speaker_wav"] = f"{user_silo}_{payload['speaker_wav']}"
            logger.debug(f"Siloing speaker_wav to: {payload['speaker_wav']}")

        form = _encode_multipart(payload)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form) as resp:
                return await _consume_response(resp, url)
    except aiohttp.ClientError as e:
        logger.error(f"Connection error to upstream {url}: {e}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "UpstreamUnreachable",
                "message": f"Upstream service at {url} is starting or unreachable: {e}",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error proxying POST (multipart) {url}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "InternalProxyError",
                "message": f"Unexpected error proxying request: {str(e)}",
            },
        )


async def _proxy_post_json(base: str, path: str, payload: dict) -> Any:
    """POST with JSON body (for endpoints that accept JSON)."""
    url = f"{base}{path}"

    try:
        # Silo speaker name/wav
        user_silo = _get_user_id()
        if "speaker_name" in payload:
            payload["speaker_name"] = f"{user_silo}_{payload['speaker_name']}"
        if "speaker_wav" in payload:
            # Only prefix if it's a name, not a path or latent id
            if not payload["speaker_wav"].startswith("/") and "|" not in payload["speaker_wav"]:
                payload["speaker_wav"] = f"{user_silo}_{payload['speaker_wav']}"
        logger.debug(f"Siloing payload keys for user {user_silo}")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return await _consume_response(resp, url)
    except aiohttp.ClientError as e:
        logger.error(f"Connection error to upstream {url}: {e}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "UpstreamUnreachable",
                "message": f"Upstream service at {url} is starting or unreachable: {e}",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error proxying POST (json) {url}")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "InternalProxyError",
                "message": f"Unexpected error proxying request: {str(e)}",
            },
        )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
# register_passthrough_routes(chute, load_route_manifest(static_routes=CHUTE_STATIC_ROUTES), DEFAULT_SERVICE_PORT)


# -----------------------------------------------------------------------------
# Startup: launch XTTS + Whisper services
# -----------------------------------------------------------------------------
register_service_launcher(chute, ENTRYPOINT, SERVICE_PORTS, timeout=300, soft_fail=True)


# -----------------------------------------------------------------------------
# XTTS GET Cords (port 8020)
# -----------------------------------------------------------------------------

@chute.cord(public_api_path="/speakers_list", public_api_method="GET")
async def speakers_list(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/speakers_list")


@chute.cord(public_api_path="/speakers", public_api_method="GET")
async def speakers(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/speakers")


@chute.cord(public_api_path="/languages", public_api_method="GET")
async def languages(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/languages")


@chute.cord(public_api_path="/get_folders", public_api_method="GET")
async def get_folders(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/get_folders")


@chute.cord(public_api_path="/get_models_list", public_api_method="GET")
async def get_models_list(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/get_models_list")


@chute.cord(public_api_path="/get_tts_settings", public_api_method="GET")
async def get_tts_settings(self, args: dict) -> Any:
    return await _proxy_get(XTTS_BASE, "/get_tts_settings")


# -----------------------------------------------------------------------------
# XTTS POST Cords (port 8020) - JSON to Multipart
# -----------------------------------------------------------------------------

@chute.cord(public_api_path="/set_output", public_api_method="POST")
async def set_output(self, args: dict) -> Any:
    return await _proxy_post_json(XTTS_BASE, "/set_output", args or {})


@chute.cord(public_api_path="/set_speaker_folder", public_api_method="POST")
async def set_speaker_folder(self, args: dict) -> Any:
    return await _proxy_post_json(XTTS_BASE, "/set_speaker_folder", args or {})


@chute.cord(public_api_path="/switch_model", public_api_method="POST")
async def switch_model(self, args: dict) -> Any:
    return await _proxy_post_json(XTTS_BASE, "/switch_model", args or {})


@chute.cord(public_api_path="/set_tts_settings", public_api_method="POST")
async def set_tts_settings_post(self, args: dict) -> Any:
    return await _proxy_post_json(XTTS_BASE, "/set_tts_settings", args or {})


@chute.cord(public_api_path="/tts_to_audio", public_api_method="POST", output_content_type="audio/wav")
async def tts_to_audio(self, args: dict) -> Response:
    """
    Generate audio from text. Expects:
      - text: str
      - language: str
      - speaker_wav: str (voice name or path to latents)
    """
    return await _proxy_post_multipart(XTTS_BASE, "/tts_to_audio", args or {})


@chute.cord(public_api_path="/tts_to_audio/", public_api_method="POST", output_content_type="audio/wav")
async def tts_to_audio_slash(self, args: dict) -> Response:
    return await self.tts_to_audio(args)


@chute.cord(public_api_path="/tts_to_file", public_api_method="POST")
async def tts_to_file(self, args: dict) -> Any:
    return await _proxy_post_multipart(XTTS_BASE, "/tts_to_file", args or {})


@chute.cord(public_api_path="/tts_to_file/", public_api_method="POST")
async def tts_to_file_slash(self, args: dict) -> Any:
    return await self.tts_to_file(args)


@chute.cord(public_api_path="/create_latents", public_api_method="POST")
async def create_latents(self, args: dict) -> Any:
    return await _proxy_post_multipart(XTTS_BASE, "/create_latents", args or {})


@chute.cord(public_api_path="/store_latents", public_api_method="POST")
async def store_latents(self, args: dict) -> Any:
    return await _proxy_post_json(XTTS_BASE, "/store_latents", args or {})


@chute.cord(public_api_path="/create_and_store_latents", public_api_method="POST")
async def create_and_store_latents(self, args: dict) -> Any:
    """
    Clone a speaker voice from audio. Expects:
      - speaker_name: str
      - language: str
      - wav_file_base64: base64-encoded audio
    """
    return await _proxy_post_multipart(XTTS_BASE, "/create_and_store_latents", args or {})


# -----------------------------------------------------------------------------
# Whisper.cpp Cords (port 8080) - JSON to Multipart
# -----------------------------------------------------------------------------

@chute.cord(public_api_path="/load", public_api_method="GET")
async def whisper_load_get(self, args: dict) -> Any:
    try:
        return await _proxy_get(WHISPER_BASE, "/load")
    except HTTPException as exc:
        if exc.status_code == 404 and isinstance(exc.detail, str) and "/load" in exc.detail:
            # The upstream whisper server doesn't expose GET /load; report a helpful status object instead.
            return {
                "status": "unavailable",
                "detail": "Upstream whisper.cpp HTTP server does not serve GET /load; use POST /whisper_load to load a model.",
            }
        raise


@chute.cord(public_api_path="/whisper_load", public_api_method="POST")
async def whisper_load_post(self, args: dict) -> Any:
    return await _proxy_post_json(WHISPER_BASE, "/load", args or {})


@chute.cord(public_api_path="/inference", public_api_method="POST")
async def whisper_inference(self, args: dict) -> Any:
    """
    Transcribe audio. Expects:
      - file_base64: base64-encoded audio (will be sent as 'file' multipart field)
      - temperature: float (optional)
      - response_format: str (optional, e.g. 'json', 'text', 'srt', 'vtt')
    """
    payload = dict(args) if args else {}
    if "file_base64" in payload:
        payload["file_base64"] = payload["file_base64"]
    return await _proxy_post_multipart(WHISPER_BASE, "/inference", payload)


# -----------------------------------------------------------------------------
# Local Testing
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Chute: {chute.name}")
    print(f"Image: {image.name}:{image.tag}")
    print(f"Service Ports: {SERVICE_PORTS}")
    print("\nCords:")
    for cord in chute.cords:
        print(f"Chute Cord: {cord._public_api_method:6} {cord._public_api_path}")
