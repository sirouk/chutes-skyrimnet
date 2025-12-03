#!/usr/bin/env python3
"""Discover HTTP routes and emit a manifest for dynamic chute cords."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urljoin

import requests

DEFAULT_PROBE_PATHS: List[str] = [
    "/openapi.json",
    "/swagger.json",
    "/docs/openapi.json",
    "/docs.json",
]


def fetch_spec(base_url: str, probe_paths: Iterable[str]) -> dict:
    """Try a sequence of probe paths until we get an OpenAPI spec."""
    session = requests.Session()
    for probe in probe_paths:
        url = urljoin(base_url, probe)
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if "paths" in data:
                return data
        except requests.RequestException:
            continue
    raise RuntimeError(f"Unable to fetch OpenAPI spec from {base_url}")


def extract_routes(spec: dict, default_port: int) -> list[dict]:
    """Convert OpenAPI paths into the manifest format consumed by the chute blueprint."""
    routes: list[dict] = []
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        for method, definition in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            routes.append(
                {
                    "path": path,
                    "method": method.upper(),
                    "port": default_port,
                    "target_path": path,
                    "stream": definition.get("x-stream", False),
                }
            )
    return routes


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover HTTP routes and emit a manifest.")
    parser.add_argument("--base-url", help="Base URL to probe, e.g. http://127.0.0.1:8020")
    parser.add_argument(
        "--probe-paths",
        default=",".join(DEFAULT_PROBE_PATHS),
        help=f"Comma-separated list of paths to try (default: {DEFAULT_PROBE_PATHS})",
    )
    parser.add_argument("--port", type=int, help="Passthrough port to assign to discovered routes")
    parser.add_argument("--output", "-o", help="File to write (defaults to stdout)")
    parser.add_argument(
        "--chute-file",
        help="Path to a chute deployment file (e.g. deploy_xtts_whisper.py) for automated probing",
    )
    parser.add_argument(
        "--docker-gpus",
        default=None,
        help="Value for --gpus when auto-running the image (e.g. 'all')",
    )
    parser.add_argument(
        "--docker-extra-arg",
        action="append",
        default=[],
        help="Additional arguments to pass to `docker run` (repeatable)",
    )
    parser.add_argument(
        "--docker-env",
        action="append",
        default=[],
        help="Environment variables to pass to container as KEY=VALUE (repeatable)",
    )
    parser.add_argument(
        "--startup-delay",
        type=int,
        default=10,
        help="Seconds to wait after starting the container before probing (default: 10)",
    )
    parser.add_argument(
        "--probe-timeout",
        type=int,
        default=60,
        help="Seconds to keep retrying each port before giving up (default: 60)",
    )
    args = parser.parse_args()

    if not args.base_url and not args.chute_file:
        parser.error("Either --base-url or --chute-file must be provided.")

    probe_paths = [p.strip() for p in args.probe_paths.split(",") if p.strip()]

    if args.chute_file:
        payload = discover_from_chute_file(
            Path(args.chute_file),
            probe_paths,
            startup_delay=args.startup_delay,
            docker_env=args.docker_env,
            docker_gpus=args.docker_gpus,
            docker_extra_args=args.docker_extra_arg,
            probe_timeout=args.probe_timeout,
        )
        output_path = args.output or f"{Path(args.chute_file).stem}.routes.json"
        if output_path == "-":
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            with open(output_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
        return

    spec = fetch_spec(args.base_url, probe_paths)
    routes = extract_routes(spec, args.port)
    payload = {"routes": routes}

    write_manifest(payload, args.output)


def write_manifest(payload: dict, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    else:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")


def discover_from_chute_file(
    chute_file: Path,
    probe_paths: Iterable[str],
    *,
    startup_delay: int,
    docker_gpus: str | None,
    docker_extra_args: list[str],
    docker_env: list[str],
    probe_timeout: int,
) -> dict:
    os.environ["CHUTES_SKIP_ROUTE_REGISTRATION"] = "1"
    module = load_module_from_path(chute_file)
    image = getattr(module, "CHUTE_BASE_IMAGE")
    entrypoint = getattr(module, "ENTRYPOINT", None)
    name = getattr(module, "CHUTE_NAME", chute_file.stem)
    ports = getattr(module, "SERVICE_PORTS", None)
    if not ports:
        ports = sorted(
            {
                value
                for key, value in vars(module).items()
                if key.endswith("_PORT") and isinstance(value, int)
            }
        )
    if not ports:
        raise RuntimeError(f"No SERVICE_PORTS or *_PORT variables found in {chute_file}")

    # Extract CHUTE_ENV from module and merge with CLI-provided env vars
    chute_env_dict = getattr(module, "CHUTE_ENV", {})
    all_env = [f"{k}={v}" for k, v in chute_env_dict.items()]
    all_env.extend(docker_env)  # CLI args override module defaults

    print(f"[info] starting probe container from {image} for ports {ports}")
    if all_env:
        print(f"[info] passing env vars: {[e.split('=')[0] for e in all_env]}")
    container_id = start_container(image, entrypoint, ports, docker_gpus, docker_extra_args, all_env)
    try:
        wait_with_logs(container_id, startup_delay)
        routes: list[dict] = []
        for port in ports:
            base_url = get_host_url(container_id, port)
            try:
                spec = fetch_spec_with_retry(base_url, probe_paths, probe_timeout)
            except RuntimeError as exc:
                print(f"[warn] {exc}", file=sys.stderr)
                continue
            routes.extend(extract_routes(spec, port))
        if not routes:
            raise RuntimeError("No routes discovered from any exposed ports.")
        return {"routes": routes, "source": name}
    finally:
        stop_container(container_id)


def load_module_from_path(path: Path):
    # Add the chute file's directory to sys.path so relative imports work
    parent_dir = str(path.parent.resolve())
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    # Skip route registration during discovery (routes don't exist yet)
    os.environ["CHUTES_SKIP_ROUTE_REGISTRATION"] = "1"

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if not spec or not spec.loader:
        raise ImportError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def start_container(image: str, entrypoint: str | None, ports: list[int], gpus: str | None, extra_args: list[str], env_vars: list[str] | None = None) -> str:
    name = f"chutes-discover-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "-d", "--name", name]  # removed --rm to keep container for debugging
    if gpus:
        cmd.extend(["--gpus", gpus])
    for env in (env_vars or []):
        cmd.extend(["-e", env])
    for arg in extra_args:
        if arg.strip():
            cmd.extend(arg.split())
    for port in ports:
        host_port = pick_host_port()
        cmd.extend(["-p", f"{host_port}:{port}"])
        print(f"[info] mapping container port {port} -> host port {host_port}")
    cmd.append(image)
    if entrypoint:
        cmd.append(entrypoint)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()

    # Verify container is actually running
    time.sleep(2)
    inspect_result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True, text=True
    )
    if inspect_result.returncode != 0 or inspect_result.stdout.strip() != "true":
        # Container crashed - show logs
        logs = subprocess.run(
            ["docker", "logs", "--tail", "50", container_id],
            capture_output=True, text=True
        )
        # Cleanup the crashed container
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        raise RuntimeError(
            f"Container exited immediately. Last logs:\n{logs.stdout}{logs.stderr}"
        )
    return container_id


def stop_container(container_id: str) -> None:
    subprocess.run(["docker", "stop", container_id], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "rm", "-f", container_id], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_host_url(container_id: str, container_port: int) -> str:
    result = subprocess.run(
        ["docker", "port", container_id, str(container_port)],
        check=True,
        capture_output=True,
        text=True,
    )
    line = result.stdout.strip().splitlines()[0]
    host, mapped_port = line.rsplit(":", 1)
    host = host.split("/")[-1].strip()
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{mapped_port}"


def fetch_spec_with_retry(base_url: str, probe_paths: Iterable[str], timeout: int) -> dict:
    deadline = time.time() + timeout
    last_exc: RuntimeError | None = None
    while time.time() < deadline:
        try:
            return fetch_spec(base_url, probe_paths)
        except RuntimeError as exc:
            last_exc = exc
            time.sleep(5)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Unable to fetch OpenAPI spec from {base_url}")


def wait_with_logs(container_id: str, duration: int, interval: int = 15, tail_lines: int = 5) -> None:
    deadline = time.time() + duration
    while True:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        print(f"[info] waiting for services... (~{remaining}s remaining)")
        tail_container_logs(container_id, tail_lines)
        time.sleep(min(interval, max(1, remaining)))


def tail_container_logs(container_id: str, lines: int) -> None:
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        if output:
            print("-- recent logs --")
            print(output)
            print("----")
    except Exception as exc:
        print(f"[warn] unable to fetch logs for {container_id}: {exc}")


def pick_host_port(start: int = 40000, end: int = 60000, attempts: int = 50) -> int:
    for _ in range(attempts):
        candidate = random.randint(start, end)
        if is_port_free(candidate):
            return candidate
    # fallback to OS-assigned ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":
    main()

