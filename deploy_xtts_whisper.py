import asyncio
import os
import signal
import subprocess
from typing import Dict

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


async def proxy_request(request: Request, target_url: str) -> Response:
    """Forward the incoming FastAPI request to the local vendor service."""
    body = await request.body()
    headers = _strip_hop_headers(dict(request.headers))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                content=body if body else None,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Upstream XTTS error: {exc}"
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
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


async def fetch_json(url: str) -> Dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"XTTS control plane unavailable: {exc}"
        ) from exc


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
    self._entrypoint_proc = subprocess.Popen(["bash", "-lc", ENTRYPOINT])
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


@chute.cord(public_api_path="/speakers/", public_api_method="GET")
async def speakers(self):
    return await fetch_json(f"{XTTS_BASE}/speakers/")


@chute.cord(
    public_api_path="/tts_to_audio/",
    public_api_method="POST",
    output_content_type="audio/wav",
)
async def tts_to_audio(self, request: Request) -> Response:
    return await proxy_request(request, f"{XTTS_BASE}/tts_to_audio/")


@chute.cord(public_api_path="/v1/audio/transcriptions", public_api_method="POST")
async def transcribe(self, request: Request) -> Response:
    return await proxy_request(request, f"{WHISPER_BASE}/inference")
