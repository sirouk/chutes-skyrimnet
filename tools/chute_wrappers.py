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


def build_wrapper_image(username: str, name: str, tag: str, base_image: str, python_version: str = "3.10") -> Image:
    """
    Build a wrapper image with system Python (not Conda) for chutes compatibility.
    The chutes-inspecto.so binary segfaults under Conda Python; a normal apt-installed
    Python works fine.

    Args:
        python_version: System Python version to install (e.g. "3.10", "3.11", "3.12").
                        Default is "3.10" which is known to work with chutes-inspecto.so.
    """
    py_ver = python_version  # e.g. "3.10"
    return (
        Image(
            username=username,
            name=name,
            tag=tag,
        )
        .from_base(base_image)
        .with_env("DEBIAN_FRONTEND", "noninteractive")
        .with_env("NEEDRESTART_SUSPEND", "y")
        # Install system Python (works with chutes-inspecto.so, unlike Conda Python)
        .run_command(
            f"apt update && apt -y install python{py_ver} python{py_ver}-venv python{py_ver}-dev python3-pip && "
            "rm -f /usr/lib/python3*/EXTERNALLY-MANAGED && "
            f"ln -sf /usr/bin/python{py_ver} /usr/local/bin/python && "
            f"ln -sf /usr/bin/python{py_ver} /usr/local/bin/python3"
        )
        .run_command("apt update && apt -y upgrade && apt autoclean -y && apt -y autoremove")
        .run_command(f"apt update && apt -y install {APT_PACKAGES}")
        .run_command("mkdir -p /etc/OpenCL/vendors/ && echo 'libnvidia-opencl.so.1' > /etc/OpenCL/vendors/nvidia.icd")
        .run_command(
            "(id chutes 2>/dev/null || useradd chutes) && "
            "usermod -s /bin/bash chutes && "
            "mkdir -p /home/chutes /app /opt/whispercpp/models && "
            "chown -R chutes:chutes /home/chutes /app /opt/whispercpp /var/log && "
            "usermod -aG root chutes && "
            "chmod g+wrx /usr/local/bin /usr/local/lib /usr/local/share /usr/local/share/man 2>/dev/null || true"
        )
        .run_command("/usr/local/bin/python -m pip install --upgrade pip")
        .run_command("mkdir -p /root/.cache && chown -R chutes:chutes /root")
        .set_user("chutes")
        .with_env("PATH", "/home/chutes/.local/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        .set_user("root")
        .run_command("rm -rf /home/chutes/.cache")
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
        existing = {(r["path"], r.get("method", "GET").upper()): r for r in routes}
        duplicates = []
        for route in static_routes:
            key = (route["path"], route.get("method", "GET").upper())
            if key in existing:
                # Check if definitions differ
                existing_route = existing[key]
                if (route.get("port") != existing_route.get("port") or
                        route.get("target_path") != existing_route.get("target_path")):
                    logger.warning(
                        f"Static route {key[1]} {key[0]} differs from discovered: "
                        f"static={route}, discovered={existing_route}"
                    )
                else:
                    duplicates.append(f"{key[1]} {key[0]}")
            else:
                routes.append(route)
                existing[key] = route
        if duplicates:
            logger.info(
                f"Skipped {len(duplicates)} duplicate static route(s) already in manifest: "
                f"{', '.join(duplicates[:3])}{'...' if len(duplicates) > 3 else ''}"
            )

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

