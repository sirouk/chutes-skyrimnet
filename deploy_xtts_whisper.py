import asyncio
import os
from typing import Dict
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, Response

from chutes.chute import Chute, NodeSelector
from chutes.image import Image

USERNAME = os.getenv("CHUTES_USERNAME", "skyrimnet")
ENTRYPOINT = os.getenv("XTTS_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")
XTTS_PORT = int(os.getenv("XTTS_HTTP_PORT", "8020"))
WHISPER_PORT = int(os.getenv("XTTS_WHISPER_PORT", "8080"))
LOCAL_HOST = "127.0.0.1"
XTTS_BASE = f"http://{LOCAL_HOST}:{XTTS_PORT}"
WHISPER_BASE = f"http://{LOCAL_HOST}:{WHISPER_PORT}"


async def wait_for_port(port: int, host: str = LOCAL_HOST, timeout: int = 240) -> None:
    """Poll until the containerised service starts listening on `port`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Timed out waiting for {host}:{port}")
            await asyncio.sleep(2)


image = (
    Image(
        username=USERNAME,
        name="xtts-whisper",
        tag="wrap-1.0.0",
        readme="Wrapper image that reuses elbios/xtts-whisper and adds the Chutes runtime.",
    )
    .from_base("elbios/xtts-whisper:latest")
    .run_command("pip install --no-cache-dir chutes httpx python-multipart fastapi")
)

chute = Chute(
    username=USERNAME,
    name="xtts-whisper",
    tagline="Pass-through wrapper for elbios/xtts-whisper (XTTS + Whisper.cpp).",
    readme="""
### XTTS + Whisper Wrapper

This chute boots the upstream `elbios/xtts-whisper:latest` image and simply proxies its
service endpoints via Chutes cords. No model weights or helpers live in this repo any
longer — everything runs inside the vendor image.

**Exposed cords**
1. `GET /speakers/` → `GET http://127.0.0.1:8020/speakers/`
2. `POST /tts_to_audio/` → `POST http://127.0.0.1:8020/tts_to_audio/` (returns `audio/wav`)
3. `POST /v1/audio/transcriptions` → `POST http://127.0.0.1:8080/inference` (multipart passthrough)
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16),
    concurrency=4,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


@chute.on_startup()
async def boot(self):
    try:
        self._entrypoint_proc = subprocess.Popen(["bash", "-lc", ENTRYPOINT])
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"XTTS entrypoint '{ENTRYPOINT}' not found. "
            "Ensure this script exists in the base image or override XTTS_ENTRYPOINT."
        ) from exc
    await wait_for_port(XTTS_PORT)
    await wait_for_port(WHISPER_PORT)


@chute.on_shutdown()
async def shutdown(self):
    proc = getattr(self, "_entrypoint_proc", None)
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@chute.cord(
    public_api_path="/speakers",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/speakers",
)
async def speakers(self): ...


@chute.cord(
    public_api_path="/speakers_list",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/speakers_list",
)
async def speakers_list(self): ...


@chute.cord(
    public_api_path="/languages",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/languages",
)
async def languages(self): ...


@chute.cord(
    public_api_path="/get_folders",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/get_folders",
)
async def folders(self): ...


@chute.cord(
    public_api_path="/get_models_list",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/get_models_list",
)
async def models_list(self): ...


@chute.cord(
    public_api_path="/get_tts_settings",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/get_tts_settings",
)
async def tts_settings(self): ...


def _strip_hop_headers(headers: Dict[str, str]) -> Dict[str, str]:
    hop_headers = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
    }
    return {k: v for k, v in headers.items() if k.lower() not in hop_headers}


def _append_query(url: str, request: Request) -> str:
    if not request.url.query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{request.url.query}"


async def _proxy_request(
    request: Request, target_url: str, include_query: bool = True
) -> Response:
    url = _append_query(target_url, request) if include_query else target_url
    headers = _strip_hop_headers(dict(request.headers))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=await request.body(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"XTTS passthrough failed: {exc}"
        ) from exc
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers={
            key: value
            for key, value in resp.headers.items()
            if key.lower() in {"content-type", "content-length"}
        },
    )


@chute.cord(
    public_api_path="/sample",
    public_api_method="GET",
    output_content_type="audio/wav",
)
async def sample(self, request: Request):
    params = list(request.query_params.multi_items())
    file_path = None
    filtered: Dict[str, str] = {}
    for key, value in params:
        if key == "path" and file_path is None:
            file_path = value
        else:
            filtered.setdefault(key, value)
    if not file_path:
        raise HTTPException(
            status_code=400,
            detail="Query parameter `path` (relative file path) is required.",
        )
    target = f"{XTTS_BASE}/sample/{file_path}"
    if filtered:
        target = f"{target}?{urlencode(filtered, doseq=True)}"
    return await _proxy_request(request, target, include_query=False)


@chute.cord(
    public_api_path="/set_output",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/set_output",
)
async def set_output(self): ...


@chute.cord(
    public_api_path="/set_speaker_folder",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/set_speaker_folder",
)
async def set_speaker_folder(self): ...


@chute.cord(
    public_api_path="/switch_model",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/switch_model",
)
async def switch_model(self): ...


@chute.cord(
    public_api_path="/set_tts_settings",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/set_tts_settings",
)
async def set_tts_settings(self): ...


@chute.cord(
    public_api_path="/tts_to_audio/",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/tts_to_audio/",
    output_content_type="audio/wav",
)
async def tts_to_audio(self): ...


@chute.cord(
    public_api_path="/tts_to_file",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/tts_to_file",
)
async def tts_to_file(self): ...


@chute.cord(
    public_api_path="/tts_stream",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/tts_stream",
    output_content_type="audio/x-wav",
)
async def tts_stream(self): ...


@chute.cord(
    public_api_path="/create_latents",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/create_latents",
    output_content_type="application/json",
)
async def create_latents(self): ...


@chute.cord(
    public_api_path="/store_latents",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/store_latents",
)
async def store_latents(self): ...


@chute.cord(
    public_api_path="/create_and_store_latents",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=XTTS_PORT,
    passthrough_path="/create_and_store_latents",
    output_content_type="application/json",
)
async def create_and_store_latents(self): ...


@chute.cord(
    public_api_path="/v1/audio/transcriptions",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=WHISPER_PORT,
    passthrough_path="/inference",
)
async def transcribe(self): ...
