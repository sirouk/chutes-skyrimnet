import json
import os
from pathlib import Path
from typing import Iterable

from loguru import logger

from chutes.image import Image

LOCAL_HOST = "127.0.0.1"

APT_PACKAGES = " ".join(
    [
        "libclblast-dev",
        "clinfo",
        "ocl-icd-libopencl1",
        "opencl-headers",
        "ocl-icd-opencl-dev",
        "libudev-dev",
        "libopenmpi-dev",
        "cmake",
        "automake",
        "pkg-config",
        "gcc",
        "g++",
        "vim",
        "git",
        "git-lfs",
        "openssh-server",
        "curl",
        "wget",
        "jq",
    ]
)


def parse_service_ports(env_value: str | None = None, default_ports: str = "8020,8080") -> list[int]:
    raw = (env_value if env_value is not None else os.getenv("CHUTE_PORTS", default_ports)).strip()
    ports = [int(port.strip()) for port in raw.split(",") if port.strip()]
    if not ports:
        raise RuntimeError("CHUTE_PORTS must specify at least one port")
    return ports


def build_wrapper_image(username: str, name: str, tag: str, base_image: str) -> Image:
    return (
        Image(
            username=username,
            name=name,
            tag=tag,
        )
        .from_base(base_image)
        # .with_python(PYTHON_VERSION) # Uncomment this to use a specific Python version, otherwise use image's default
        .with_env("DEBIAN_FRONTEND", "noninteractive")
        .with_env("NEEDRESTART_SUSPEND", "y")
        .run_command("apt update && apt -y upgrade && apt autoclean -y && apt -y autoremove")
        .run_command(f"apt update && apt -y install {APT_PACKAGES}")
        .run_command("mkdir -p /etc/OpenCL/vendors/ && echo 'libnvidia-opencl.so.1' > /etc/OpenCL/vendors/nvidia.icd")
        .run_command(
            "(id chutes 2>/dev/null || useradd chutes) && "
            "usermod -s /bin/bash chutes && "
            "mkdir -p /home/chutes /app /opt/whispercpp/models && "
            "chown -R chutes:chutes /home/chutes /app /opt/whispercpp /var/log && "
            "usermod -aG root chutes && "
            "chmod g+wrx /opt/conda/bin /opt/conda/lib/python*/site-packages /usr/local/bin /usr/local/lib /usr/local/share /usr/local/share/man 2>/dev/null || true"
        )
        .run_command("python -m pip install uv cmake ninja")
        .run_command("mkdir -p /root/.cache && chown -R chutes:chutes /root")
        .set_user("chutes")
        .run_command(
            "mkdir -p /home/chutes/.local/bin && "
            "printf '#!/bin/bash\\nexec uv pip --user \"$@\"\\n' > /home/chutes/.local/bin/pip && "
            "chmod 755 /home/chutes/.local/bin/pip"
        )
        .with_env("PATH", "/opt/conda/bin:/home/chutes/.local/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        .with_env("UV_SYSTEM_PYTHON", "1")
        .with_env("UV_CACHE_DIR", "/home/chutes/.cache/uv")
        .set_user("root")
        .run_command("rm -rf /home/chutes/.cache")
        .run_command(
            "PY_BIN=$(command -v python || command -v python3) && "
            "if [ -n \"$PY_BIN\" ]; then ln -sf \"$PY_BIN\" /usr/local/bin/python; fi"
        )
        .run_command(
            "python -c \""
            "import os,pathlib,site;"
            "hints=[pathlib.Path(p) for p in os.getenv('CHUTES_PYTHONPATH_HINTS','/app:/workspace:/srv').split(':') if p];"
            "hints.append(pathlib.Path.cwd());"
            "paths,seen=[],set();"
            "add=lambda p:(p.is_dir() and str(p.resolve()) not in seen and (seen.add(str(p.resolve())) or paths.append(str(p.resolve()))));"
            "looks_like_pkg=lambda p:p.is_dir() and (any((p/m).exists() for m in ['__init__.py','setup.py','pyproject.toml','setup.cfg']) or any(p.glob('*.py')));"
            "[add(b) or [add(c) for c in (list(b.iterdir()) if b.exists() else []) if looks_like_pkg(c)] for b in hints if b.exists()];"
            "pth=pathlib.Path(site.getsitepackages()[0])/'chutes_app_path.pth';"
            "(paths and pth.write_text(chr(10).join(paths)+chr(10)))"
            "\""
        )
        .set_user("chutes")
        .with_env("HOME", "/home/chutes")
        .with_env("PIP_USER", "1")
        .with_env("PYTHONUSERBASE", "/home/chutes/.local")
        .with_env("PIP_CACHE_DIR", "/home/chutes/.cache/pip")
        .with_entrypoint([])
    )


def load_route_manifest(
    manifest_env: str = "CHUTES_ROUTE_MANIFEST_JSON",
    path_env: str = "CHUTES_ROUTE_MANIFEST",
    default_filename: str | None = None,
    static_routes: list[dict] | None = None,
) -> list[dict]:
    if os.getenv("CHUTES_SKIP_ROUTE_REGISTRATION"):
        return []

    routes: list[dict] = []

    # Load from inline env var
    inline_manifest = os.getenv(manifest_env)
    if inline_manifest:
        routes = _parse_routes_json(inline_manifest)
    else:
        # Auto-detect manifest from caller's filename if not specified
        if default_filename is None and not os.getenv(path_env):
            import inspect
            caller_frame = inspect.stack()[1]
            caller_file = Path(caller_frame.filename)
            default_filename = f"{caller_file.stem}.routes.json"
            # Check in caller's directory first
            manifest_path = caller_file.parent / default_filename
            if not manifest_path.exists():
                manifest_path = Path(default_filename)
        else:
            manifest_path = Path(os.getenv(path_env, default_filename or "routes.json"))

        if manifest_path.exists():
            routes = _parse_routes_json(manifest_path.read_text())
        elif not static_routes:
            raise RuntimeError(
                f"Route manifest not found at {manifest_path}. Run tools/discover_routes.py first or "
                "set CHUTES_ROUTE_MANIFEST_JSON."
            )

    # Merge static routes (avoid duplicates by path+method)
    if static_routes:
        existing = {(r["path"], r.get("method", "GET").upper()) for r in routes}
        for route in static_routes:
            key = (route["path"], route.get("method", "GET").upper())
            if key not in existing:
                routes.append(route)
                existing.add(key)

    return routes


def register_passthrough_routes(chute, routes: Iterable[dict], default_port: int) -> None:
    if not routes:
        return
    registered = 0
    for idx, route in enumerate(routes):
        path = route.get("path", "")
        skip_reason = _should_skip_route(path)
        if skip_reason:
            logger.debug(f"Skipping route {path}: {skip_reason}")
            continue
        _register_single_route(chute, route, registered, default_port)
        registered += 1
    logger.info(f"Registered {registered} passthrough routes")


# Routes to skip (internal/UI routes that shouldn't be exposed as API endpoints)
# Only skip clearly internal Gradio/UI routes - be conservative
_SKIP_PATH_PREFIXES = (
    "/static",      # Static asset files
    "/assets",      # Asset files
    "/svelte",      # Svelte UI framework routes
    "/login",       # Auth UI
    "/logout",      # Auth UI
    "/gradio_api",  # Internal Gradio API
    "/theme",       # UI theming
    "/__",          # Internal/private routes
)
_SKIP_PATHS_EXACT = {"/", ""}


def _should_skip_route(path: str) -> str | None:
    """Return reason to skip route, or None if it should be registered."""
    # Skip routes with path parameters (curly braces) - Chutes SDK doesn't support them
    if "{" in path or "}" in path:
        return "path parameter"
    # Skip paths with dots (file extensions) - Chutes SDK doesn't support them
    if "." in path:
        return "file extension in path"
    # Skip root and empty paths - Chutes SDK doesn't support them
    if path in _SKIP_PATHS_EXACT:
        return "root/empty path"
    # Skip internal/UI routes (Gradio, static assets, etc.)
    if any(path.startswith(prefix) or path.rstrip("/").startswith(prefix) for prefix in _SKIP_PATH_PREFIXES):
        return "internal/UI route"
    return None


async def wait_for_services(ports: Iterable[int], host: str = LOCAL_HOST, timeout: int = 600) -> None:
    for port in ports:
        await _wait_for_port(port, host=host, timeout=timeout)


async def probe_services(ports: Iterable[int], host: str = LOCAL_HOST, timeout: int = 5) -> list[str]:
    errors: list[str] = []
    for port in ports:
        try:
            await _wait_for_port(port, host=host, timeout=timeout)
        except Exception as exc:
            errors.append(f"Port {port}: {exc}")
    return errors


def _parse_routes_json(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid route manifest JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("routes", [])
    if not isinstance(data, list):
        raise ValueError("Route manifest must be a list or contain a 'routes' list")
    return data


def _register_single_route(chute, route: dict, idx: int, default_port: int) -> None:
    path = route["path"]
    method = route.get("method", "GET").upper()
    passthrough_path = route.get("target_path", path)
    passthrough_port = int(route.get("port", default_port))
    stream = bool(route.get("stream", False))

    internal_path = f"{method.lower()}_{_sanitize_route_name(path)}_{idx}"
    decorator = chute.cord(
        path=internal_path,
        public_api_path=path,
        public_api_method=method,
        passthrough=True,
        passthrough_port=passthrough_port,
        passthrough_path=passthrough_path,
        stream=stream,
    )

    async def _route_handler(self, *_args, **_kwargs):
        """Auto-generated passthrough cord."""
        pass

    _route_handler.__name__ = f"cord_{internal_path}"
    decorator(_route_handler)


def _sanitize_route_name(path: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in path.strip("/"))
    return cleaned or "root"


async def _wait_for_port(port: int, host: str, timeout: int) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"Timed out waiting for {host}:{port}")
            await asyncio.sleep(1)

