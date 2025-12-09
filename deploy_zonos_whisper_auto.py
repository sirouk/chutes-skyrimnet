import os
import subprocess
import asyncio
from configparser import ConfigParser
from loguru import logger
from chutes.chute import Chute, NodeSelector
from chutes.image import Image
import re
import chutes.chute.cord

# Monkeypatch PATH_RE to allow parameterized paths (e.g. /sample/{id})
chutes.chute.cord.PATH_RE = re.compile(r".*")

# Load auth
chutes_config = ConfigParser()
chutes_config.read(os.path.expanduser("~/.chutes/config.ini"))
USERNAME = os.getenv("CHUTES_USERNAME") or chutes_config.get("auth", "username", fallback="chutes")
CHUTE_NAME = "zonos-whisper"

image = (
    Image(
        username=USERNAME,
        name=CHUTE_NAME,
        tag="22.04",
        readme="Zonos + Whisper.cpp CUDA server",
    )
    .from_base("parachutes/python:3.12")
    .with_env("MAX_IDLE_SECONDS", "86400")
    .with_env("NVIDIA_VISIBLE_DEVICES", "all")
    .with_env("NVIDIA_DRIVER_CAPABILITIES", "compute,utility")
    .with_env("LD_LIBRARY_PATH", "/opt/conda/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/opt/conda/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64")
    .with_env("PYTORCH_VERSION", "2.7.0")
    .with_env("CUDA_HOME", "/usr/local/cuda")
    .with_env("LIBRARY_PATH", "/usr/local/cuda/lib64:/usr/local/cuda/lib64/stubs:")
    .with_env("HF_HOME", "/huggingface_cache")
    .with_env("HUGGINGFACE_HUB_CACHE", "/huggingface_cache/hub")
    .with_env("WHISPER_MODEL", "large-v3-turbo")
    .with_env("WHISPER_MODELS_DIR", "/opt/whispercpp/models")

    .with_env("CHUTE_ENTRYPOINT", "docker-entrypoint.sh")
    .set_user("root")
    .run_command("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends         ca-certificates         libjpeg-dev         libpng-dev         && rm -rf /var/lib/apt/lists/*")
    .run_command("if test -n \"${TRITON_VERSION}}\" -a \"${TARGETPLATFORM}}\" != \"linux/arm64\"; then         DEBIAN_FRONTEND=noninteractive apt install -y --no-install-recommends gcc;         rm -rf /var/lib/apt/lists/*;     fi")
    .set_workdir("/workspace")
    .run_command("apt-get update &&     apt-get install -y --no-install-recommends         ffmpeg portaudio19-dev libasound2         gcc g++ make         ca-certificates wget git espeak-ng &&     apt-get clean && rm -rf /var/lib/apt/lists/*")
    .run_command("mkdir -p /usr/local/cuda/lib64")
    .run_command("if [ -e /usr/local/cuda/lib64/stubs/libnvidia-ml.so ]; then ln -s /usr/local/cuda/lib64/stubs/libnvidia-ml.so /usr/local/cuda/lib64/stubs/libnvidia-ml.so.1; fi")
    .run_command("echo \"/usr/local/cuda/lib64\" > /etc/ld.so.conf.d/cuda.conf &&     ldconfig")
    .run_command("python -m pip install --no-cache-dir --upgrade pip uv vastai huggingface_hub \"python-dateutil>=2.8.2\" &&     git clone --depth 1 https://github.com/Elbios/Zonos.git /opt/Zonos &&     cd /opt/Zonos &&     uv pip install --system -e . -e .[compile] &&     true")
    .run_command("mkdir -p $HUGGINGFACE_HUB_CACHE")
    .run_command("if [ -f /opt/whispercpp/download-ggml-model.sh ]; then chmod +x /opt/whispercpp/download-ggml-model.sh; fi && ldconfig")
    .run_command("if [ -e /opt/whispercpp/whisper-server ]; then ln -s /opt/whispercpp/whisper-server /usr/local/bin/whisper-server; fi &&     if [ -e /opt/whispercpp/whisper-cli ]; then ln -s /opt/whispercpp/whisper-cli /usr/local/bin/whisper; fi")
    .run_command("if [ -f /usr/local/bin/docker-entrypoint.sh ]; then chmod +x /usr/local/bin/docker-entrypoint.sh; fi")
    .run_command("if command -v pip >/dev/null 2>&1; then pip install --no-cache-dir chutes --upgrade || true; fi")
    .set_user("chutes")
)

chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    tagline="Zonos + Whisper.cpp CUDA server",
    readme="Zonos + Whisper.cpp CUDA server",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16),
    concurrency=1,
    shutdown_after_seconds=3600,
    allow_external_egress=True,
)

@chute.on_startup()
async def start_services(self):
    # Startup logic extracted from docker-entrypoint.sh
    from loguru import logger
    import asyncio

    # Helper to wait for ports
    async def _wait_for_ports(ports, host="127.0.0.1", timeout=600):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            ready = 0
            for port in ports:
                try:
                    _, writer = await asyncio.open_connection(host, port)
                    writer.close()
                    await writer.wait_closed()
                    ready += 1
                except OSError:
                    pass
            if ready == len(ports):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Timed out waiting for ports: {ports}")
            await asyncio.sleep(1)

    logger.info("Starting services...")
    
    # Original Entrypoint Content for reference:
    # Could not read entrypoint script.

    # TODO: Refine these commands based on the entrypoint content above
    # Attempting to run the original entrypoint script
    cmd = ["bash", "docker-entrypoint.sh"]
    subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=os.environ.copy())

    logger.info("Waiting for ports: [7860, 8080]")
    await _wait_for_ports([7860, 8080])
    logger.success("Services ready!")

# Routes

@chute.cord(path="post_login", public_api_path="/login/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/login/")
def post_login(data): pass

@chute.cord(path="post_login", public_api_path="/login", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/login")
def post_login(data): pass

@chute.cord(path="get_logout", public_api_path="/logout", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/logout")
def get_logout(data): pass

@chute.cord(path="get_svelte_path", public_api_path="/svelte/{{path}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/svelte/{{path}}")
def get_svelte_path(data): pass

@chute.cord(path="get_root", public_api_path="/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/")
def get_root(data): pass

@chute.cord(path="get_gradio_api_deep_link", public_api_path="/gradio_api/deep_link", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/deep_link")
def get_gradio_api_deep_link(data): pass

@chute.cord(path="get_config", public_api_path="/config", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/config")
def get_config(data): pass

@chute.cord(path="get_config", public_api_path="/config/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/config/")
def get_config(data): pass

@chute.cord(path="get_static_path", public_api_path="/static/{{path}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/static/{{path}}")
def get_static_path(data): pass

@chute.cord(path="get_assets_path", public_api_path="/assets/{{path}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/assets/{{path}}")
def get_assets_path(data): pass

@chute.cord(path="get_favicon_ico", public_api_path="/favicon.ico", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/favicon.ico")
def get_favicon_ico(data): pass

@chute.cord(path="get_theme_css", public_api_path="/theme.css", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/theme.css")
def get_theme_css(data): pass

@chute.cord(path="get_robots_txt", public_api_path="/robots.txt", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/robots.txt")
def get_robots_txt(data): pass

@chute.cord(path="get_pwa_icon_size", public_api_path="/pwa_icon/{{size}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/pwa_icon/{{size}}")
def get_pwa_icon_size(data): pass

@chute.cord(path="get_pwa_icon", public_api_path="/pwa_icon", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/pwa_icon")
def get_pwa_icon(data): pass

@chute.cord(path="get_manifest_json", public_api_path="/manifest.json", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/manifest.json")
def get_manifest_json(data): pass

@chute.cord(path="get_monitoring", public_api_path="/monitoring", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/monitoring")
def get_monitoring(data): pass

@chute.cord(path="get_monitoring_key", public_api_path="/monitoring/{{key}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/monitoring/{{key}}")
def get_monitoring_key(data): pass

@chute.cord(path="get_gradio_api_user", public_api_path="/gradio_api/user/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/user/")
def get_gradio_api_user(data): pass

@chute.cord(path="get_gradio_api_user", public_api_path="/gradio_api/user", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/user")
def get_gradio_api_user(data): pass

@chute.cord(path="get_gradio_api_login_check", public_api_path="/gradio_api/login_check/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/login_check/")
def get_gradio_api_login_check(data): pass

@chute.cord(path="get_gradio_api_login_check", public_api_path="/gradio_api/login_check", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/login_check")
def get_gradio_api_login_check(data): pass

@chute.cord(path="get_gradio_api_token", public_api_path="/gradio_api/token/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/token/")
def get_gradio_api_token(data): pass

@chute.cord(path="get_gradio_api_token", public_api_path="/gradio_api/token", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/token")
def get_gradio_api_token(data): pass

@chute.cord(path="get_gradio_api_app_id", public_api_path="/gradio_api/app_id/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/app_id/")
def get_gradio_api_app_id(data): pass

@chute.cord(path="get_gradio_api_app_id", public_api_path="/gradio_api/app_id", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/app_id")
def get_gradio_api_app_id(data): pass

@chute.cord(path="get_gradio_api_dev_reload", public_api_path="/gradio_api/dev/reload", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/dev/reload")
def get_gradio_api_dev_reload(data): pass

@chute.cord(path="get_gradio_api_info", public_api_path="/gradio_api/info", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/info")
def get_gradio_api_info(data): pass

@chute.cord(path="get_gradio_api_info", public_api_path="/gradio_api/info/", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/info/")
def get_gradio_api_info(data): pass

@chute.cord(path="get_gradio_api_openapi_json", public_api_path="/gradio_api/openapi.json", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/openapi.json")
def get_gradio_api_openapi_json(data): pass

@chute.cord(path="get_gradio_api_custom_component_id_environment_type_file_name", public_api_path="/gradio_api/custom_component/{{id}}/{{environment}}/{{type}}/{{file_name}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/custom_component/{{id}}/{{environment}}/{{type}}/{{file_name}}")
def get_gradio_api_custom_component_id_environment_type_file_name(data): pass

@chute.cord(path="get_gradio_api_proxy_url_path", public_api_path="/gradio_api/proxy={{url_path}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/proxy={{url_path}}")
def get_gradio_api_proxy_url_path(data): pass

@chute.cord(path="get_gradio_api_file_path_or_url", public_api_path="/gradio_api/file={{path_or_url}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/file={{path_or_url}}")
def get_gradio_api_file_path_or_url(data): pass

@chute.cord(path="post_gradio_api_stream_event_id", public_api_path="/gradio_api/stream/{{event_id}}", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/stream/{{event_id}}")
def post_gradio_api_stream_event_id(data): pass

@chute.cord(path="post_gradio_api_stream_event_id_close", public_api_path="/gradio_api/stream/{{event_id}}/close", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/stream/{{event_id}}/close")
def post_gradio_api_stream_event_id_close(data): pass

@chute.cord(path="get_gradio_api_stream_session_hash_run_component_id_playlist_m3u8", public_api_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/playlist.m3u8", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/playlist.m3u8")
def get_gradio_api_stream_session_hash_run_component_id_playlist_m3u8(data): pass

@chute.cord(path="get_gradio_api_stream_session_hash_run_component_id_segment_id_ext", public_api_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/{{segment_id}}.{{ext}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/{{segment_id}}.{{ext}}")
def get_gradio_api_stream_session_hash_run_component_id_segment_id_ext(data): pass

@chute.cord(path="get_gradio_api_stream_session_hash_run_component_id_playlist_file", public_api_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/playlist-file", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/stream/{{session_hash}}/{{run}}/{{component_id}}/playlist-file")
def get_gradio_api_stream_session_hash_run_component_id_playlist_file(data): pass

@chute.cord(path="get_gradio_api_file_path", public_api_path="/gradio_api/file/{{path}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/file/{{path}}")
def get_gradio_api_file_path(data): pass

@chute.cord(path="post_gradio_api_reset", public_api_path="/gradio_api/reset", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/reset")
def post_gradio_api_reset(data): pass

@chute.cord(path="post_gradio_api_reset", public_api_path="/gradio_api/reset/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/reset/")
def post_gradio_api_reset(data): pass

@chute.cord(path="get_gradio_api_heartbeat_session_hash", public_api_path="/gradio_api/heartbeat/{{session_hash}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/heartbeat/{{session_hash}}")
def get_gradio_api_heartbeat_session_hash(data): pass

@chute.cord(path="post_gradio_api_api_api_name", public_api_path="/gradio_api/api/{{api_name}}/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/api/{{api_name}}/")
def post_gradio_api_api_api_name(data): pass

@chute.cord(path="post_gradio_api_api_api_name", public_api_path="/gradio_api/api/{{api_name}}", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/api/{{api_name}}")
def post_gradio_api_api_api_name(data): pass

@chute.cord(path="post_gradio_api_run_api_name", public_api_path="/gradio_api/run/{{api_name}}/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/run/{{api_name}}/")
def post_gradio_api_run_api_name(data): pass

@chute.cord(path="post_gradio_api_run_api_name", public_api_path="/gradio_api/run/{{api_name}}", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/run/{{api_name}}")
def post_gradio_api_run_api_name(data): pass

@chute.cord(path="post_gradio_api_call_api_name", public_api_path="/gradio_api/call/{{api_name}}/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/call/{{api_name}}/")
def post_gradio_api_call_api_name(data): pass

@chute.cord(path="post_gradio_api_call_api_name", public_api_path="/gradio_api/call/{{api_name}}", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/call/{{api_name}}")
def post_gradio_api_call_api_name(data): pass

@chute.cord(path="post_gradio_api_queue_join", public_api_path="/gradio_api/queue/join", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/queue/join")
def post_gradio_api_queue_join(data): pass

@chute.cord(path="post_gradio_api_cancel", public_api_path="/gradio_api/cancel", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/cancel")
def post_gradio_api_cancel(data): pass

@chute.cord(path="get_gradio_api_call_api_name_event_id", public_api_path="/gradio_api/call/{{api_name}}/{{event_id}}", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/call/{{api_name}}/{{event_id}}")
def get_gradio_api_call_api_name_event_id(data): pass

@chute.cord(path="get_gradio_api_queue_data", public_api_path="/gradio_api/queue/data", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/queue/data")
def get_gradio_api_queue_data(data): pass

@chute.cord(path="post_gradio_api_component_server", public_api_path="/gradio_api/component_server/", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/component_server/")
def post_gradio_api_component_server(data): pass

@chute.cord(path="post_gradio_api_component_server", public_api_path="/gradio_api/component_server", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/component_server")
def post_gradio_api_component_server(data): pass

@chute.cord(path="get_gradio_api_queue_status", public_api_path="/gradio_api/queue/status", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/queue/status")
def get_gradio_api_queue_status(data): pass

@chute.cord(path="get_gradio_api_upload_progress", public_api_path="/gradio_api/upload_progress", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/upload_progress")
def get_gradio_api_upload_progress(data): pass

@chute.cord(path="post_gradio_api_upload", public_api_path="/gradio_api/upload", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/upload")
def post_gradio_api_upload(data): pass

@chute.cord(path="get_gradio_api_startup_events", public_api_path="/gradio_api/startup-events", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/startup-events")
def get_gradio_api_startup_events(data): pass

@chute.cord(path="get_gradio_api_theme_css", public_api_path="/gradio_api/theme.css", method="GET", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/theme.css")
def get_gradio_api_theme_css(data): pass

@chute.cord(path="post_gradio_api_process_recording", public_api_path="/gradio_api/process_recording", method="POST", passthrough=True, passthrough_port=7860, passthrough_path="/gradio_api/process_recording")
def post_gradio_api_process_recording(data): pass

