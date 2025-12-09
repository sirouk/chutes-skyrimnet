#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from typing import List, Dict, Any

# Ensure we can import from the same directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from discover_routes import (
        start_container,
        stop_container,
        wait_with_logs,
        get_host_url,
        fetch_spec_with_retry,
        extract_routes,
        DEFAULT_PROBE_PATHS
    )
except ImportError:
    print("Error: Could not import discover_routes. Make sure tools/discover_routes.py exists.")
    sys.exit(1)

TEMPLATE = """import os
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
CHUTE_NAME = "{chute_name}"

image = (
    Image(
        username=USERNAME,
        name=CHUTE_NAME,
        tag="{tag}",
        readme="{readme}",
    )
    .from_base("parachutes/python:3.12")
{env_vars}
    .with_env("CHUTE_ENTRYPOINT", "{entrypoint_path}")
{build_steps}
)

chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    tagline="{tagline}",
    readme="{readme}",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16),
    concurrency=1,
    shutdown_after_seconds=3600,
    allow_external_egress=True,
)

@chute.on_startup()
async def start_services(self):
    # Startup logic extracted from {entrypoint_path}
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
                raise RuntimeError(f"Timed out waiting for ports: {{ports}}")
            await asyncio.sleep(1)

    logger.info("Starting services...")
    
    # Original Entrypoint Content for reference:
    # {entrypoint_content_comment}

    # TODO: Refine these commands based on the entrypoint content above
    # Attempting to run the original entrypoint script
    cmd = ["bash", "{entrypoint_path}"]
    subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=os.environ.copy())

    logger.info("Waiting for ports: {ports}")
    await _wait_for_ports({ports})
    logger.success("Services ready!")

# Routes
{routes}
"""

def escape_braces(text: str) -> str:
    """Escape curly braces so TEMPLATE.format won't treat them as placeholders."""
    if text is None:
        return ""
    return text.replace("{", "{{").replace("}", "}}")

def escape_braces_preserving_env(text: str) -> str:
    """
    Escape braces except when used as ${VAR} so shell env refs stay valid.
    """
    if text is None:
        return ""
    # Replace { not preceded by $ with {{, and } not preceded by $ with }}
    text = re.sub(r'(?<!\$)\{', '{{', text)
    text = re.sub(r'(?<!\$)\}', '}}', text)
    return text

def sanitize_run_command(cmd: str) -> str:
    """
    Make extracted RUN commands safer across images:
    - Keep ${VAR} intact (handled in escape).
    - Guard pip -r requirements.txt if file is missing.
    - Drop/ignore 'pip cache purge' (unsupported on uv pip wrappers).
    - Guard pip installs of wildcard wheel paths (*.whl) so missing files don't fail.
    - Guard chmod +x on missing files.
    - Guard ln -s when source missing (skip instead of fail).
    """
    # Guard requirements.txt installs
    cmd = re.sub(
        r'pip install(\s+--no-cache-dir)?\s+-r\s+requirements\.txt',
        r'if [ -f requirements.txt ]; then pip install\1 -r requirements.txt; fi',
        cmd,
    )
    # Remove pip cache purge
    cmd = cmd.replace("pip cache purge", "true")
    # Guard wildcard wheel installs
    def _wrap_wheel(m: re.Match) -> str:
        path = m.group("path")
        return f'if ls {path} 1>/dev/null 2>&1; then pip install --no-cache-dir {path}; rm -f {path}; fi'
    cmd = re.sub(
        r'pip install(?:\s+--no-cache-dir)?\s+(?P<path>[^\s;]*\*[^;\s]*)',
        _wrap_wheel,
        cmd,
    )
    # Guard chmod +x path
    cmd = re.sub(
        r'chmod \+x (?P<path>[^\s;&]+)',
        r'if [ -f \g<path> ]; then chmod +x \g<path>; fi',
        cmd,
    )
    # Guard ln -s source dest
    cmd = re.sub(
        r'ln -s(f)? (?P<src>[^\s;]+)\s+(?P<dst>[^\s;]+)',
        r'if [ -e \g<src> ]; then ln -s\1 \g<src> \g<dst>; fi',
        cmd,
    )
    return cmd

class _SafeDict(dict):
    def __missing__(self, key):
        # Leave unknown placeholders intact instead of raising KeyError
        return "{" + key + "}"

def get_docker_history(image):
    try:
        cmd = ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.splitlines()
    except subprocess.CalledProcessError as e:
        print(f"Error fetching history: {e}")
        return []

def parse_build_steps(history_lines):
    # We assume the original image built as root, so we switch to root for these steps
    steps = ['    .set_user("root")']
    ensure_chutes = (
        '    .run_command("if command -v pip >/dev/null 2>&1; then pip install --no-cache-dir chutes --upgrade || true; fi")'
    )
    
    def _escape(line: str) -> str:
        return escape_braces_preserving_env(line).replace('"', '\\"')
    
    for line in reversed(history_lines):
        line = line.strip()
        if not line: continue
        line = re.sub(r"\s*# buildkit$", "", line)
        line = line.replace("/bin/sh -c #(nop) ", "")
        
        # Handle RUN commands
        if line.startswith("RUN "):
            line = line[4:].strip()
            # Remove BuildKit args like "|4 ... /bin/sh -c"
            line = re.sub(r"^\|[0-9]+.*?/bin/sh -c\s+", "", line)
            # Remove plain /bin/sh -c
            line = line.replace("/bin/sh -c ", "")
            
            line = sanitize_run_command(line)
            safe_line = _escape(line)
            steps.append(f'    .run_command("{safe_line}")')
            
        elif line.startswith("WORKDIR"):
             parts = line.split(" ")
             if len(parts) > 1:
                 steps.append(f'    .set_workdir("{parts[1]}")')
        # Handle raw commands that might look like RUN deps
        elif "apt-get" in line or "pip install" in line:
             line = line.replace("/bin/sh -c ", "")
             line = sanitize_run_command(line)
             safe_line = _escape(line)
             steps.append(f'    .run_command("{safe_line}")')

    # Ensure chutes CLI is present even if base image lacks it
    steps.append(ensure_chutes)
    # Switch back to chutes user at the end
    steps.append('    .set_user("chutes")')
    return "\n".join(steps)

def analyze_image(image):
    try:
        data = json.loads(subprocess.check_output(["docker", "inspect", image], text=True))
        config = data[0]["Config"]
        entrypoint = config.get("Entrypoint")
        cmd = config.get("Cmd")
        env = config.get("Env", [])
        labels = config.get("Labels") or {}
        
        target_script = None
        if entrypoint:
            target_script = entrypoint[0] if isinstance(entrypoint, list) else entrypoint
        elif cmd:
            target_script = cmd[0] if isinstance(cmd, list) else cmd
            
        content = ""
        if target_script and (target_script.endswith(".sh") or target_script.endswith(".py")):
            try:
                content = subprocess.check_output(
                    ["docker", "run", "--rm", "--entrypoint", "/bin/cat", image, target_script],
                    text=True, stderr=subprocess.DEVNULL
                )
            except:
                content = "Could not read entrypoint script."
        
        # Metadata extraction
        desc = labels.get("org.opencontainers.image.description") or labels.get("description") or f"Auto-generated chute from {image}"
        readme = labels.get("readme") or desc
        version = labels.get("org.opencontainers.image.version") or labels.get("version") or labels.get("build_version")

        return {
            "entrypoint": target_script,
            "content": content,
            "env": env,
            "labels": labels,
            "description": desc,
            "readme": readme,
            "version": version
        }
    except Exception as e:
        print(f"Error analyzing image: {e}")
        return {"entrypoint": None, "content": "", "env": [], "labels": {}, "description": "", "readme": "", "version": None}

def perform_live_discovery(image, entrypoint, ports, gpus=None, env_vars=None, startup_delay=60, probe_timeout=30):
    """Spin up container and probe for routes."""
    print(f"Starting container for live route discovery on ports {ports}...")
    try:
        container_id = start_container(image, entrypoint, ports, gpus, [], env_vars or [])
        try:
            wait_with_logs(container_id, startup_delay) 
            
            discovered_routes = []
            handled_ports = set()

            for port in ports:
                print(f"Probing port {port}...")
                base_url = get_host_url(container_id, port)
                try:
                    spec = fetch_spec_with_retry(base_url, DEFAULT_PROBE_PATHS, timeout=probe_timeout)
                    routes = extract_routes(spec, port)
                    if routes:
                        print(f"  Found {len(routes)} OpenAPI routes on port {port}")
                        discovered_routes.extend(routes)
                        handled_ports.add(port)
                except Exception:
                    print(f"  No OpenAPI spec found on port {port}")

            return discovered_routes, handled_ports
        finally:
            stop_container(container_id)
    except Exception as e:
        print(f"Live discovery failed: {e}")
        return [], set()

def generate_route_code(route, idx):
    path = route["path"]
    method = route["method"]
    port = route["port"]
    target = route.get("target_path", path)
    # Escape curly braces so they don't get interpreted by TEMPLATE.format()
    path = path.replace("{", "{{").replace("}", "}}")
    target = target.replace("{", "{{").replace("}", "}}")
    
    # Sanitize path for function name: replace all non-alphanumeric chars with _
    sanitized_path = re.sub(r'[^a-zA-Z0-9_]', '_', path.strip('/'))
    # Remove duplicate underscores
    sanitized_path = re.sub(r'_+', '_', sanitized_path)
    # Remove leading/trailing underscores
    sanitized_path = sanitized_path.strip('_')
    
    func_name = f"{method.lower()}_{sanitized_path}"
    if func_name.endswith("_"): func_name += "root"
    
    # Ensure valid python identifier
    if func_name[0].isdigit():
        func_name = f"fn_{func_name}"

    return f"""
@chute.cord(path="{func_name}", public_api_path="{path}", method="{method}", passthrough=True, passthrough_port={port}, passthrough_path="{target}")
def {func_name}(data): pass
"""

def main():
    parser = argparse.ArgumentParser(description="Generate manual Chute deployment from Docker image")
    parser.add_argument("image", help="Docker image to analyze")
    parser.add_argument("--name", help="Chute name (default: derived from image name)")
    parser.add_argument("--gpus", help="GPUs to use for discovery (e.g. 'all')", default=None)
    parser.add_argument("--startup-delay", type=int, default=120, help="Seconds to wait for container startup")
    parser.add_argument("--probe-timeout", type=int, default=30, help="Seconds to retry probing each port")
    parser.add_argument("--env", action="append", default=[], help="Extra environment variables (KEY=VALUE)")
    parser.add_argument("--interactive", action="store_true", help="Enable interactive mode for manual cord entry")
    args = parser.parse_args()

    # Determine Chute Name
    if args.name:
        chute_name = args.name
    else:
        # elbios/xtts-whisper:latest -> xtts-whisper
        base = args.image.split(":")[0]
        chute_name = base.split("/")[-1] if "/" in base else base
        chute_name = chute_name.replace("_", "-").lower()

    # Analyze Image Metadata
    print(f"Analyzing {args.image}...")
    meta = analyze_image(args.image)
    
    # Determine Tag
    if ":" in args.image:
        tag_candidate = args.image.split(":")[-1]
    else:
        tag_candidate = "latest"

    if tag_candidate == "latest":
        if meta["version"]:
            tag = meta["version"]
        else:
            tag = f"v1-{uuid.uuid4().hex[:6]}"
    else:
        tag = tag_candidate

    print(f"Chute Name: {chute_name}")
    print(f"Chute Tag: {tag}")
    print(f"Description: {meta['description']}")
    
    history = get_docker_history(args.image)
    build_steps = parse_build_steps(history)
    
    # Get exposed ports from metadata
    try:
        data = json.loads(subprocess.check_output(["docker", "inspect", args.image], text=True))
        exposed = data[0]["Config"].get("ExposedPorts", {})
        ports = sorted([int(p.split("/")[0]) for p in exposed.keys()])
    except Exception as e:
        print(f"Warning: Could not inspect image ports: {e}")
        ports = []
    
    print(f"Probing the following ports: {ports}")

    discovered_routes, handled_ports = perform_live_discovery(
        args.image, None, ports, args.gpus, args.env,
        startup_delay=args.startup_delay, 
        probe_timeout=args.probe_timeout
    )

    # Generate Env Vars
    env_str = ""
    for e in args.env:
        if "=" in e:
            k, v = e.split("=", 1)
            env_str += f'    .with_env("{escape_braces_preserving_env(k)}", "{escape_braces_preserving_env(v)}")\n'

    for e in meta["env"]:
        if "=" in e:
            k, v = e.split("=", 1)
            if k not in ["PATH", "HOSTNAME", "HOME"]:
                env_str += f'    .with_env("{escape_braces_preserving_env(k)}", "{escape_braces_preserving_env(v)}")\n'

    # Generate Routes Code
    routes_str = ""
    
    # Add discovered OpenAPI routes
    for idx, route in enumerate(discovered_routes):
        routes_str += generate_route_code(route, idx)

    # Interactive Manual Cords
    if args.interactive:
        print("\n--- Manual Cord Entry ---")
        print("Press Enter to skip optional fields or accept defaults.")
        while True:
            try:
                add = input("\nAdd a manual cord? [y/N]: ").strip().lower()
            except EOFError:
                break
            if add != 'y':
                break
            
            # Public API Path
            while True:
                public_api_path = input("Public API Path (e.g. /generate): ").strip()
                if public_api_path: break
                print("Path is required.")

            # Method
            method = input("Method (GET/POST) [POST]: ").strip().upper() or "POST"

            # Auto-generate Function Name
            sanitized_path = re.sub(r'[^a-zA-Z0-9_]', '_', public_api_path.strip('/'))
            sanitized_path = re.sub(r'_+', '_', sanitized_path).strip('_')
            func_name = f"{method.lower()}_{sanitized_path}"
            if func_name.endswith("_"): func_name += "root"
            if not func_name[0].isalpha() and not func_name.startswith("_"):
                 func_name = f"fn_{func_name}"
            
            print(f"-> Auto-generated function name: {func_name}")

            # Passthrough Port
            default_port = ports[0] if ports else ""
            port_str = input(f"Passthrough Port (e.g. 8000) [{default_port}]: ").strip()
            
            if not port_str and default_port:
                port = default_port
            elif port_str:
                try:
                    port = int(port_str)
                except ValueError:
                    print("Invalid port.")
                    continue
            else:
                print("Port is required for passthrough.")
                continue

            # Passthrough Path
            default_target = public_api_path
            target_path = input(f"Target Path (on container) [{default_target}]: ").strip() or default_target

            # Stream
            stream_str = input("Stream response? [y/N]: ").strip().lower()
            stream = stream_str == 'y'

            # Generate Code - escape curly braces for TEMPLATE.format()
            escaped_public = public_api_path.replace("{", "{{").replace("}", "}}")
            escaped_target = target_path.replace("{", "{{").replace("}", "}}")
            routes_str += f"""
@chute.cord(path="{func_name}", public_api_path="{escaped_public}", method="{method}", passthrough=True, passthrough_port={port}, passthrough_path="{escaped_target}"{', stream=True' if stream else ''})
def {func_name}(data): pass
"""

    if not routes_str:
        routes_str = "\n# No OpenAPI routes discovered and no manual cords added.\n# Please add @chute.cord definitions manually.\n"

    output = TEMPLATE.format_map(_SafeDict({
        "chute_name": chute_name,
        "source_image": args.image,
        "tag": tag,
        "readme": escape_braces(meta["readme"]).replace("\n", "\\n"), # Simple escape + braces
        "tagline": escape_braces(meta["description"]),
        "build_steps": build_steps,
        "entrypoint_path": escape_braces(meta["entrypoint"] or "/unknown"),
        "entrypoint_content_comment": escape_braces(meta["content"]).replace("\n", "\n    # "),
        "ports": ports,
        "routes": routes_str,
        "env_vars": env_str
    })).replace("(Manual)", "").replace("(Manual Build)", "") # Strip "Manual" from description/comments
    
    filename = f"deploy_{chute_name.replace('-', '_')}_auto.py"
    with open(filename, "w") as f:
        f.write(output)
    
    print(f"Generated {filename}")

if __name__ == "__main__":
    main()
