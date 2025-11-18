import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional
from uuid import uuid4


@dataclass
class VendorProcessHandle:
    """Track the vendor process/container we spawned so we can clean it up."""

    proc: subprocess.Popen
    container_name: Optional[str] = None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.container_name:
            subprocess.run(["docker", "rm", "-f", self.container_name], check=False)


def _gpu_flags(override: Optional[str]) -> List[str]:
    if override:
        return ["--gpus", override]
    if shutil.which("nvidia-smi"):
        flags = ["--gpus", "all"]
        if os.path.exists("/usr/bin/nvidia-container-runtime"):
            flags.extend(["--runtime", "nvidia"])
        return flags
    return []


def _env_flags(keys: Iterable[str], prefixes: Iterable[str]) -> List[str]:
    flags: List[str] = []
    for key in keys:
        value = os.getenv(key)
        if value is not None:
            flags.extend(["-e", f"{key}={value}"])
    for prefix in prefixes:
        for key, value in os.environ.items():
            if key.startswith(prefix):
                flags.extend(["-e", f"{key}={value}"])
    return flags


def launch_vendor_process(
    label: str,
    entrypoint: str,
    vendor_image: str,
    service_ports: Iterable[int],
    whisper_ports: Iterable[int],
    env_keys: Iterable[str],
    env_prefixes: Iterable[str],
    dev_gpu_env: Optional[str] = None,
) -> VendorProcessHandle:
    """Run the vendor stack either via the native entrypoint or a docker fallback."""

    if os.path.exists(entrypoint):
        proc = subprocess.Popen(["bash", "-lc", entrypoint])
        return VendorProcessHandle(proc=proc)

    container_name = f"{label}-{os.getpid()}-{uuid4().hex[:6]}"
    port_flags: List[str] = []

    for port in list(service_ports) + list(whisper_ports):
        port_flags.extend(["-p", f"{port}:{port}"])

    env_flags = _env_flags(env_keys, env_prefixes)
    gpu_override = os.getenv(dev_gpu_env) if dev_gpu_env else None
    gpu_flags = _gpu_flags(gpu_override)

    subprocess.run(["docker", "rm", "-f", container_name], check=False)

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        *gpu_flags,
        *port_flags,
        *env_flags,
        vendor_image,
    ]

    proc = subprocess.Popen(cmd)
    return VendorProcessHandle(proc=proc, container_name=container_name)

