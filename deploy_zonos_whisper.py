import asyncio
import os
import signal
import subprocess

from chutes.chute import Chute, NodeSelector
from chutes.image import Image

USERNAME = os.getenv("CHUTES_USERNAME", "skyrimnet")
ENTRYPOINT = os.getenv("ZONOS_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")
SERVICE_PORT = int(os.getenv("ZONOS_HTTP_PORT", "7860"))
WHISPER_PORT = int(os.getenv("ZONOS_WHISPER_PORT", "8080"))
LOCAL_HOST = "127.0.0.1"


async def wait_for_port(port: int, host: str = LOCAL_HOST, timeout: int = 300) -> None:
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
        name="zonos-whisper",
        tag="wrap-1.0.0",
        readme="Wrapper image for elbios/zonos-whisper with Chutes tooling baked in.",
    )
    .from_base("elbios/zonos-whisper:latest")
    .run_command("pip install --no-cache-dir chutes httpx python-multipart fastapi")
)

chute = Chute(
    username=USERNAME,
    name="zonos-whisper",
    tagline="Pass-through wrapper for elbios/zonos-whisper (Zyphra Zonos + Whisper.cpp).",
    readme="""
### Zonos Wrapper

This chute runs `elbios/zonos-whisper:latest` unchanged and proxies the local Gradio +
Whisper servers so clients can keep using the Zonos UI/API without uploading new code.

**Exposed cords**
1. `POST /api/generate_audio` → `POST http://127.0.0.1:7860/api/generate_audio`
2. `POST /api/predict/` → `POST http://127.0.0.1:7860/api/predict/`
3. `POST /queue/join` → `POST http://127.0.0.1:7860/queue/join`
4. `POST /queue/status` → `POST http://127.0.0.1:7860/queue/status`
5. `GET /file?path=...` → `GET http://127.0.0.1:7860/file?path=...`
6. `POST /v1/audio/transcriptions` → `POST http://127.0.0.1:8080/inference`
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=24),
    concurrency=1,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


ZONOS_ENV_KEYS = [
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
]
ZONOS_ENV_PREFIXES = ["ZONOS_", "WHISPER_"]


@chute.on_startup()
async def boot(self):
    try:
        self._entrypoint_proc = subprocess.Popen(["bash", "-lc", ENTRYPOINT])
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Zonos entrypoint '{ENTRYPOINT}' not found. "
            "Ensure the script exists in the base image or override ZONOS_ENTRYPOINT."
        ) from exc
    await wait_for_port(SERVICE_PORT)
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
    public_api_path="/api/generate_audio",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/api/generate_audio",
)
async def generate_audio(self):
    ...


@chute.cord(
    public_api_path="/api/predict/",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/api/predict/",
)
async def api_predict(self):
    ...


@chute.cord(
    public_api_path="/queue/join",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/queue/join",
)
async def queue_join(self):
    ...


@chute.cord(
    public_api_path="/queue/status",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/queue/status",
)
async def queue_status(self):
    ...


@chute.cord(
    public_api_path="/file",
    public_api_method="GET",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/file",
)
async def fetch_file(self):
    ...


@chute.cord(
    public_api_path="/v1/audio/transcriptions",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=WHISPER_PORT,
    passthrough_path="/inference",
)
async def transcribe(self):
    ...
