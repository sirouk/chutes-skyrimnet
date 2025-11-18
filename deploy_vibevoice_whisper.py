import asyncio
import os
import signal
import subprocess

from chutes.chute import Chute, NodeSelector
from chutes.image import Image

USERNAME = os.getenv("CHUTES_USERNAME", "skyrimnet")
ENTRYPOINT = os.getenv("VIBEVOICE_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")
SERVICE_PORT = int(os.getenv("VIBEVOICE_HTTP_PORT", "7860"))
WHISPER_PORT = int(os.getenv("VIBEVOICE_WHISPER_PORT", "8080"))
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
        name="vibevoice-whisper",
        tag="wrap-1.0.0",
        readme="Wrapper image for elbios/vibevoice-whisper with the Chutes runtime pre-installed.",
    )
    .from_base("elbios/vibevoice-whisper:latest")
    .run_command("pip install --no-cache-dir chutes httpx python-multipart fastapi")
)

chute = Chute(
    username=USERNAME,
    name="vibevoice-whisper",
    tagline="Pass-through wrapper for elbios/vibevoice-whisper (VibeVoice-1.5B + Whisper.cpp).",
    readme="""
### VibeVoice Wrapper

This chute boots `elbios/vibevoice-whisper:latest` and proxies the Gradio + Whisper servers.
No custom TTS logic remains in this repo — requests are simply forwarded to the vendor image.

**Exposed cords**
1. `POST /api/generate_audio` → `POST http://127.0.0.1:7860/api/generate_audio`
2. `POST /queue/join` → `POST http://127.0.0.1:7860/queue/join`
3. `POST /queue/status` → `POST http://127.0.0.1:7860/queue/status`
4. `POST /v1/audio/transcriptions` → `POST http://127.0.0.1:8080/inference`
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=24),
    concurrency=2,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


@chute.on_startup()
async def boot(self):
    try:
        self._entrypoint_proc = subprocess.Popen(["bash", "-lc", ENTRYPOINT])
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"VibeVoice entrypoint '{ENTRYPOINT}' not found. "
            "Ensure the script exists in the base image or override VIBEVOICE_ENTRYPOINT."
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
async def generate_audio(self): ...


@chute.cord(
    public_api_path="/queue/join",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/queue/join",
)
async def queue_join(self): ...


@chute.cord(
    public_api_path="/queue/status",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=SERVICE_PORT,
    passthrough_path="/queue/status",
)
async def queue_status(self): ...


@chute.cord(
    public_api_path="/v1/audio/transcriptions",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=WHISPER_PORT,
    passthrough_path="/inference",
)
async def transcribe(self): ...
