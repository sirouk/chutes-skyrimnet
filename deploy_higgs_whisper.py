import asyncio
import os

from chutes.chute import Chute, NodeSelector
from chutes.image import Image
from vendor_launcher import VendorProcessHandle, launch_vendor_process

USERNAME = os.getenv("CHUTES_USERNAME", "skyrimnet")
ENTRYPOINT = os.getenv("HIGGS_ENTRYPOINT", "/usr/local/bin/docker-entrypoint.sh")
SERVICE_PORT = int(os.getenv("HIGGS_HTTP_PORT", "7860"))
WHISPER_PORT = int(os.getenv("HIGGS_WHISPER_PORT", "8080"))
LOCAL_HOST = "127.0.0.1"
VENDOR_IMAGE = os.getenv("HIGGS_VENDOR_IMAGE", "elbios/higgs-whisper:latest")


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
        name="higgs-whisper",
        tag="wrap-1.0.0",
        readme="Wrapper image for elbios/higgs-whisper with the Chutes runtime.",
    )
    .from_base("elbios/higgs-whisper:latest")
    .run_command("pip install --no-cache-dir chutes httpx python-multipart fastapi")
)

chute = Chute(
    username=USERNAME,
    name="higgs-whisper",
    tagline="Pass-through wrapper for elbios/higgs-whisper (Boson Higgs Audio + Whisper.cpp).",
    readme="""
### Higgs Audio Wrapper

This chute launches `elbios/higgs-whisper:latest` and exposes the upstream Gradio + Whisper endpoints.

**Exposed cords**
1. `POST /api/generate_audio` → `POST http://127.0.0.1:7860/api/generate_audio`
2. `POST /queue/join` → `POST http://127.0.0.1:7860/queue/join`
3. `POST /queue/status` → `POST http://127.0.0.1:7860/queue/status`
4. `POST /v1/audio/transcriptions` → `POST http://127.0.0.1:8080/inference`
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=32),
    concurrency=1,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


HIGGS_ENV_KEYS = [
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
]
HIGGS_ENV_PREFIXES = ["HIGGS_", "WHISPER_"]


@chute.on_startup()
async def boot(self):
    self._vendor_handle: VendorProcessHandle = launch_vendor_process(
        label="higgs",
        entrypoint=ENTRYPOINT,
        vendor_image=VENDOR_IMAGE,
        service_ports=[SERVICE_PORT],
        whisper_ports=[WHISPER_PORT],
        env_keys=HIGGS_ENV_KEYS,
        env_prefixes=HIGGS_ENV_PREFIXES,
        dev_gpu_env="HIGGS_DEV_GPUS",
    )
    await wait_for_port(SERVICE_PORT)
    await wait_for_port(WHISPER_PORT)


@chute.on_shutdown()
async def shutdown(self):
    handle = getattr(self, "_vendor_handle", None)
    if handle:
        handle.stop()


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
    public_api_path="/v1/audio/transcriptions",
    public_api_method="POST",
    passthrough=True,
    passthrough_port=WHISPER_PORT,
    passthrough_path="/inference",
)
async def transcribe(self):
    ...
