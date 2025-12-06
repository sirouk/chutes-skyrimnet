import json
import os
from pathlib import Path
from typing import Iterable

from loguru import logger

from chutes.image import Image

LOCAL_HOST = "127.0.0.1"

# OS-specific package lists for build tools and dependencies
APT_PACKAGES = " ".join([
    "libclblast-dev", "clinfo", "ocl-icd-libopencl1", "opencl-headers", "ocl-icd-opencl-dev",
    "libudev-dev", "libopenmpi-dev", "cmake", "automake", "pkg-config",
    "gcc", "g++", "vim", "git", "git-lfs", "openssh-server", "curl", "wget", "jq",
])

APK_PACKAGES = " ".join([
    "clinfo", "opencl-headers", "cmake", "automake", "pkgconf",
    "gcc", "g++", "musl-dev", "vim", "git", "git-lfs", "openssh", "curl", "wget", "jq",
])

DNF_PACKAGES = " ".join([
    "clinfo", "opencl-headers", "cmake", "automake", "pkgconfig",
    "gcc", "gcc-c++", "vim", "git", "git-lfs", "openssh-server", "curl", "wget", "jq",
])

# OS-agnostic package install command
INSTALL_PACKAGES_CMD = (
    f"if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get -y install {APT_PACKAGES}; "
    f"elif command -v apk >/dev/null 2>&1; then apk add --no-cache {APK_PACKAGES}; "
    f"elif command -v dnf >/dev/null 2>&1; then dnf install -y {DNF_PACKAGES}; "
    f"elif command -v yum >/dev/null 2>&1; then yum install -y {DNF_PACKAGES}; "
    "else echo 'WARN: No supported package manager for build tools'; fi"
)


def parse_service_ports(env_value: str | None = None, default_ports: str = "8020,8080") -> list[int]:
    raw = (env_value if env_value is not None else os.getenv("CHUTE_PORTS", default_ports)).strip()
    ports = [int(port.strip()) for port in raw.split(",") if port.strip()]
    if not ports:
        raise RuntimeError("CHUTE_PORTS must specify at least one port")
    return ports


def build_wrapper_image(
    username: str,
    name: str,
    tag: str,
    base_image: str,
    python_version: str = "3.10",
    env: dict[str, str] | None = None,
) -> Image:
    """
    Build a wrapper image with system Python (not Conda) for chutes compatibility.
    The chutes-inspecto.so binary segfaults under Conda Python; a normal apt-installed
    Python works fine.

    Args:
        python_version: System Python version to install (e.g. "3.10", "3.11", "3.12").
                        Default is "3.10" which is known to work with chutes-inspecto.so.
    """
    py_ver_fallback = python_version  # e.g. "3.10" - fallback if detection fails

    # Install system Python if needed (conda/mamba Python segfaults with chutes-inspecto.so)
    # Strategy: detect version from base, freeze packages, install system Python, reinstall packages
    # Supports: apt (Debian/Ubuntu), apk (Alpine), dnf (Fedora/RHEL8+), yum (RHEL7/CentOS)
    install_system_python_cmd = (
        # Detect Python version from base image (conda/mamba/pyenv/whatever)
        f"PY_VER=$(python3 -c 'import sys;print(f\"{{sys.version_info.major}}.{{sys.version_info.minor}}\")' 2>/dev/null || echo '{py_ver_fallback}'); "
        "echo \"Detected Python version: $PY_VER\"; "
        
        # Check if system Python already exists and isn't conda/mamba
        "if [ -x /usr/bin/python3 ] && ! readlink -f /usr/bin/python3 2>/dev/null | grep -qiE 'conda|mamba'; then "
        "echo 'System Python available, skipping install' && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python3; "
        "else "
        
        # Freeze existing packages before replacing Python
        "echo 'Freezing existing packages...' && "
        "python3 -m pip freeze 2>/dev/null > /tmp/frozen_pkgs.txt || true && "
        
        # OS-specific Python install (with fallback to python3 if specific version unavailable)
        "echo \"Installing system Python $PY_VER...\" && "
        "if command -v apt-get >/dev/null 2>&1; then "
        # Debian/Ubuntu - try specific version, fallback to python3
        "apt-get update && "
        "(apt-get -y install python${PY_VER} python${PY_VER}-venv python${PY_VER}-dev python3-pip 2>/dev/null && "
        "ln -sf /usr/bin/python${PY_VER} /usr/local/bin/python && "
        "ln -sf /usr/bin/python${PY_VER} /usr/local/bin/python3 || "
        "(echo 'Specific version unavailable, using default python3' && "
        "apt-get -y install python3 python3-venv python3-dev python3-pip && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python3)) && "
        "rm -f /usr/lib/python3*/EXTERNALLY-MANAGED; "
        "elif command -v apk >/dev/null 2>&1; then "
        # Alpine - only has default python3
        "apk add --no-cache python3 python3-dev py3-pip && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python3; "
        "elif command -v dnf >/dev/null 2>&1; then "
        # Fedora/RHEL 8+ - try specific version, fallback to python3
        "(dnf install -y python${PY_VER} python${PY_VER}-devel python3-pip 2>/dev/null && "
        "ln -sf /usr/bin/python${PY_VER} /usr/local/bin/python && "
        "ln -sf /usr/bin/python${PY_VER} /usr/local/bin/python3 || "
        "(echo 'Specific version unavailable, using default python3' && "
        "dnf install -y python3 python3-devel python3-pip && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python3)); "
        "elif command -v yum >/dev/null 2>&1; then "
        # RHEL 7/CentOS - usually only has python3
        "yum install -y python3 python3-devel python3-pip && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python && "
        "ln -sf /usr/bin/python3 /usr/local/bin/python3; "
        "else "
        "echo 'ERROR: No supported package manager found (apt/apk/dnf/yum)' && exit 1; "
        "fi && "
        
        # Verify system Python is now active
        "echo 'Verifying: '$(/usr/local/bin/python --version) && "
        
        # Reinstall frozen packages (best-effort, skip failures)
        "if [ -s /tmp/frozen_pkgs.txt ]; then "
        "echo 'Reinstalling packages...' && "
        "/usr/local/bin/python -m pip install --upgrade pip && "
        "/usr/local/bin/python -m pip install -r /tmp/frozen_pkgs.txt --ignore-installed --no-deps 2>/dev/null || "
        "echo 'Some packages failed (expected for conda-only packages)'; "
        "fi; "
        "fi"
    )

    # Link external site-packages as fallback (for pure-Python packages that pip missed)
    # Note: Only same-version packages will work; C extensions need matching Python version
    link_external_packages_cmd = (
        "python -c \""
        "import sys,os,pathlib,site;"
        "pyver=f'python{sys.version_info.major}.{sys.version_info.minor}';"
        "print(f'Linking fallback packages for {pyver}...');"
        # Only link site-packages matching current Python version (mixing versions breaks imports)
        "bases=[os.environ.get('CONDA_PREFIX',''),'/opt/conda','/opt/mamba','/root/miniconda3','/root/anaconda3'];"
        "paths=[f'{b}/lib/{pyver}/site-packages' for b in bases if b and pathlib.Path(f'{b}/lib/{pyver}/site-packages').is_dir()];"
        "seen=set();paths=[p for p in paths if p not in seen and not seen.add(p)];"
        "print(f'Fallback paths: {paths}');"
        "pth=pathlib.Path(site.getsitepackages()[0])/'chutes_compat.pth';"
        "pth.parent.mkdir(parents=True,exist_ok=True);"
        "pth.write_text(chr(10).join(paths)+chr(10)) if paths else None"
        "\""
    )

    # Create .pth file to auto-discover app directories for Python imports
    create_app_pth_cmd = (
        "python -c \""
        "import os,pathlib,site;"
        "hints=[pathlib.Path(p) for p in os.getenv('CHUTES_PYTHONPATH_HINTS','/app:/workspace:/srv').split(':') if p];"
        "hints.append(pathlib.Path.cwd());"
        "paths,seen=[],set();"
        "add=lambda p:(p.is_dir() and str(p.resolve()) not in seen and (seen.add(str(p.resolve())) or paths.append(str(p.resolve()))));"
        "looks_like_pkg=lambda p:p.is_dir() and (any((p/m).exists() for m in ['__init__.py','setup.py','pyproject.toml','setup.cfg']) or any(p.glob('*.py')));"
        "[add(b) or [add(c) for c in (list(b.iterdir()) if b.exists() else []) if looks_like_pkg(c)] for b in hints if b.exists()];"
        "pth=pathlib.Path(site.getsitepackages()[0])/'chutes_app_path.pth';"
        "pth.parent.mkdir(parents=True,exist_ok=True);"
        "pth.write_text(chr(10).join(paths)+chr(10)) if paths else None;"
        "print(f'App paths: {paths}') if paths else print('No app paths found')"
        "\""
    )

    image = (
        Image(
            username=username,
            name=name,
            tag=tag,
        )
        .from_base(base_image)
    )

    # Apply user-provided env vars early so they're available during build
    if env:
        for key, value in env.items():
            image = image.with_env(key, value)

    return (
        image
        .with_env("DEBIAN_FRONTEND", "noninteractive")
        .with_env("NEEDRESTART_SUSPEND", "y")
        # Install system Python (works with chutes-inspecto.so, unlike Conda Python)
        .run_command(install_system_python_cmd)
        # OS-agnostic system upgrade
        .run_command(
            "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get -y upgrade && apt-get autoclean -y && apt-get -y autoremove; "
            "elif command -v apk >/dev/null 2>&1; then apk update && apk upgrade; "
            "elif command -v dnf >/dev/null 2>&1; then dnf upgrade -y; "
            "elif command -v yum >/dev/null 2>&1; then yum upgrade -y; fi"
        )
        # OS-agnostic package install
        .run_command(INSTALL_PACKAGES_CMD)
        .run_command("mkdir -p /etc/OpenCL/vendors/ && echo 'libnvidia-opencl.so.1' > /etc/OpenCL/vendors/nvidia.icd")
        # Create chutes user (OS-agnostic) with generic /cache dir
        # NOTE: No sudo - miners run these images for external clients, must limit privileges
        # Deploy scripts should set HF_HOME, TORCH_HOME, etc. to /cache subdirs via .with_env()
        .run_command(
            "(id chutes 2>/dev/null || (command -v useradd >/dev/null && useradd -m chutes) || (command -v adduser >/dev/null && adduser -D chutes)) && "
            "(command -v usermod >/dev/null && usermod -s /bin/bash chutes || true) && "
            "mkdir -p /home/chutes /app /cache && "
            "chown -R chutes:chutes /home/chutes /app /cache /var/log && "
            "(command -v usermod >/dev/null && usermod -aG root chutes || (command -v adduser >/dev/null && adduser chutes root 2>/dev/null || true)) && "
            # Make install directories writable for chutes user (pip/uv package installs)
            # Strategy: chmod parent dirs so subdirs can be created, then create common leaf dirs
            # /opt and /opt/* allows creating/writing in app dirs (non-recursive, fast)
            "chmod a+rwx /opt /opt/* 2>/dev/null || true && "
            # Parent dirs (must be writable to create children)
            "for d in /usr/local/bin /usr/local/lib /usr/local/share /usr/local/include "
            "/usr/local/share/man /usr/local/share/doc "
            "/opt/conda/bin /opt/conda/lib; do "
            "mkdir -p \"$d\" 2>/dev/null && chmod a+rwx \"$d\" 2>/dev/null; done && "
            # Python package dirs: site-packages (most distros) + dist-packages (Debian/Ubuntu)
            "for d in /usr/local/lib/python*/site-packages /usr/local/lib/python*/dist-packages "
            "/usr/lib/python*/site-packages /usr/lib/python*/dist-packages "
            "/opt/conda/lib/python*/site-packages; do "
            "mkdir -p \"$d\" 2>/dev/null && chmod a+rwx \"$d\" 2>/dev/null; done || true"
        )
        .run_command(
            # Upgrade pip and create wrapper scripts that use system Python
            # Use /bin/sh for Alpine compatibility (no bash by default)
            "/usr/local/bin/python -m pip install --upgrade pip && "
            "printf '#!/bin/sh\\nexec /usr/local/bin/python -m pip \"$@\"\\n' > /usr/local/bin/pip && "
            "chmod +x /usr/local/bin/pip && "
            "cp /usr/local/bin/pip /usr/local/bin/pip3"
        )
        .run_command("mkdir -p /root/.cache && chown -R chutes:chutes /root")
        .set_user("chutes")
        .run_command("mkdir -p /home/chutes/.local/bin")
        # PATH: user bins first, then system (our symlinks), then conda as fallback
        .with_env("PATH", "/home/chutes/.local/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/opt/conda/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        .set_user("root")
        .run_command("rm -rf /home/chutes/.cache")
        # Link external packages (conda/mamba/pyenv) to system Python
        .run_command(link_external_packages_cmd)
        # Create .pth file for app directory discovery
        .run_command(create_app_pth_cmd)
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


def register_health_check(chute, ports: list[int], host: str = LOCAL_HOST) -> None:
    """Register a /health endpoint that probes all service ports."""

    @chute.cord(public_api_path="/health", public_api_method="GET", method="GET")
    async def health_check(self) -> dict:
        """Check if all services are healthy."""
        errors = await probe_services(ports, host=host, timeout=5)
        if errors:
            return {"status": "unhealthy", "errors": errors}
        return {"status": "healthy", "ports": ports}


def register_startup_wait(chute, ports: list[int], host: str = LOCAL_HOST, timeout: int = 600) -> None:
    """Register on_startup handler that waits for service ports to be ready."""

    @chute.on_startup()
    async def boot(self):
        """Wait for all services to be ready."""
        await wait_for_services(ports, host=host, timeout=timeout)


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

