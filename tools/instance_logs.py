#!/usr/bin/env python3
"""
Fetch logs from chute instances.
"""
import os
import sys
import subprocess
import httpx
from configparser import ConfigParser

# Load API key from environment or prompt
def get_api_key() -> str:
    """Get API key from environment, file, or prompt."""
    api_key = os.getenv("CHUTES_API_KEY")
    if api_key:
        return api_key
    # Try loading from a local file
    key_file = os.path.expanduser("~/.chutes/api_key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    # Prompt user
    print("API key required for instance logs.")
    print("Get one from: chutes keys list / chutes keys create <name>")
    api_key = input("Enter API key (cpk_...): ").strip()
    if api_key:
        # Optionally save for next time
        save = input("Save to ~/.chutes/api_key? [y/N]: ").strip().lower()
        if save == "y":
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, "w") as f:
                f.write(api_key)
            print(f"Saved to {key_file}")
        return api_key
    raise RuntimeError("No API key provided")


def get_base_url() -> str:
    """Get API base URL from chutes config."""
    config = ConfigParser()
    config.read(os.path.expanduser("~/.chutes/config.ini"))
    return config.get("api", "base_url", fallback="https://api.chutes.ai")


def warmup_chute(module_path: str, timeout_seconds: int = 10) -> bool:
    """Run warmup with timeout, return True if it started warming."""
    import time
    import select
    try:
        # Start warmup in background - it will keep running until instance is ready
        proc = subprocess.Popen(
            ["chutes", "warmup", module_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        
        output_lines = []
        start = time.time()
        
        while time.time() - start < timeout_seconds:
            # Check if process finished
            ret = proc.poll()
            if ret is not None:
                remaining = proc.stdout.read()
                if remaining:
                    output_lines.append(remaining)
                break
            
            # Try to read available output without blocking
            import os
            import fcntl
            fd = proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            try:
                chunk = proc.stdout.read()
                if chunk:
                    output_lines.append(chunk)
            except (IOError, TypeError):
                pass
            
            elapsed = int(time.time() - start)
            print(f"\r  Waiting for instance... {elapsed}s/{timeout_seconds}s", end="", flush=True)
            time.sleep(1)
        
        print()  # newline after progress
        
        # Check output for errors (CLI returns 0 even on error)
        output = "".join(output_lines)
        if "not found" in output.lower() or "does not belong" in output.lower():
            print("  Chute not deployed - deploy first with option 6")
            return False
        elif "error" in output.lower() and "status: warm" not in output.lower():
            print(f"  Warmup issue: check deployment")
            return False
        elif proc.poll() is None:
            proc.terminate()
            print("  Still warming up...")
            return True
        else:
            return True
    except Exception as e:
        print(f"Warmup error: {e}")
        return False


def get_chute_instances(base_url: str, api_key: str, chute_id: str) -> list[dict]:
    """Get list of instances for a chute."""
    headers = {"Authorization": api_key}
    resp = httpx.get(f"{base_url}/chutes/{chute_id}", headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"Failed to get chute: {resp.status_code} {resp.text[:200]}")
        return []
    data = resp.json()
    return data.get("instances", [])


def get_chute_id_by_name(chute_name: str) -> str | None:
    """Get chute ID by name using CLI (which has proper hotkey auth)."""
    import json
    import re
    try:
        result = subprocess.run(
            ["chutes", "chutes", "get", chute_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        # Find JSON in output (starts with { and ends with })
        output = result.stdout
        json_match = re.search(r'\{[\s\S]*\}', output)
        if json_match:
            data = json.loads(json_match.group())
            return data.get("chute_id")
    except Exception as e:
        print(f"CLI lookup error: {e}")
    return None


def fetch_instance_logs(
    base_url: str,
    api_key: str,
    instance_id: str,
    backfill: int = 100,
    timeout: int = 10,
) -> tuple[int, str]:
    """Fetch logs from an instance. Returns (status_code, content)."""
    headers = {"Authorization": api_key}
    try:
        resp = httpx.get(
            f"{base_url}/instances/{instance_id}/logs",
            headers=headers,
            params={"backfill": backfill},
            timeout=timeout,
        )
        return resp.status_code, resp.text
    except Exception as e:
        return -1, str(e)


def stream_instance_logs(
    base_url: str,
    api_key: str,
    instance_id: str,
    backfill: int = 100,
) -> None:
    """Stream logs from an instance to stdout."""
    headers = {"Authorization": api_key}
    try:
        with httpx.stream(
            "GET",
            f"{base_url}/instances/{instance_id}/logs",
            headers=headers,
            params={"backfill": backfill},
            timeout=None,
        ) as resp:
            if resp.status_code != 200:
                print(f"Error: {resp.status_code}")
                return
            for chunk in resp.iter_text():
                print(chunk, end="", flush=True)
    except KeyboardInterrupt:
        print("\n[Interrupted]")
    except Exception as e:
        print(f"Stream error: {e}")


def find_instance_with_logs(
    base_url: str,
    api_key: str,
    instances: list[dict],
    max_tries: int = 10,
) -> tuple[str | None, str]:
    """
    Try instances until one returns logs.
    Returns (instance_id, logs) or (None, error_msg).
    Prioritizes active instances, then verified, then most recent.
    """
    # Sort: active first, then verified, then by last_verified_at (most recent first)
    def sort_key(inst):
        active = inst.get("active", False)
        verified = inst.get("verified", False)
        last_verified = inst.get("last_verified_at") or ""
        return (not active, not verified, last_verified)
    
    sorted_instances = sorted(instances, key=sort_key)
    
    tried = 0
    for inst in sorted_instances:
        if tried >= max_tries:
            break
        inst_id = inst["instance_id"]
        verified = inst.get("verified", False)
        active = inst.get("active", False)
        
        print(f"  Trying {inst_id[:8]}... (active={active}, verified={verified})", end=" ")
        status, content = fetch_instance_logs(base_url, api_key, inst_id, backfill=200)
        tried += 1
        
        if status == 200 and content.strip():
            print(f"✓ ({len(content)} bytes)")
            return inst_id, content
        elif status == 200:
            print("empty")
        elif status == 404:
            print("gone")
        elif status == 403:
            print("forbidden")
        else:
            print(f"status={status}")
    
    return None, f"No instance returned logs after {tried} tries"


def check_logs(chute_name: str, warmup_module: str | None = None, stream: bool = False):
    """
    Main function to check logs for a chute.
    
    Args:
        chute_name: Chute name (will be looked up)
        warmup_module: Optional module:var path to warm up first
        stream: If True, stream logs instead of fetching once
    """
    import time
    
    try:
        api_key = get_api_key()
    except RuntimeError as e:
        print(f"Error: {e}")
        return
    
    base_url = get_base_url()
    
    # Warmup if requested (triggers instance spin-up)
    if warmup_module:
        print(f"Warming up {warmup_module} (10s)...")
        if not warmup_chute(warmup_module, timeout_seconds=10):
            return  # Exit early if warmup failed (chute not deployed)
    
    # Resolve chute name to ID using CLI
    print(f"Looking up chute '{chute_name}'...")
    chute_id = get_chute_id_by_name(chute_name)
    if not chute_id:
        # Maybe it's already a UUID
        if chute_name.count("-") == 4:
            chute_id = chute_name
        else:
            print(f"Chute not found: {chute_name}")
            return
    
    print(f"Chute ID: {chute_id}")
    
    # Retry loop - instances may take time to spin up
    max_retries = 4
    retry_delay = 8
    
    for attempt in range(max_retries):
        print(f"Getting instances... (attempt {attempt + 1}/{max_retries})")
        instances = get_chute_instances(base_url, api_key, chute_id)
        
        if not instances:
            if attempt < max_retries - 1:
                print(f"No instances yet, waiting {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            print("No instances found (chute may be cold)")
            return
        
        print(f"Found {len(instances)} instance(s)")
        
        if stream:
            # For streaming, use first verified instance
            for inst in instances:
                if inst.get("verified"):
                    print(f"Streaming logs from {inst['instance_id']}...")
                    stream_instance_logs(base_url, api_key, inst["instance_id"])
                    return
            print("No verified instances to stream from")
            return
        else:
            # Try to find an instance with logs
            inst_id, logs = find_instance_with_logs(base_url, api_key, instances)
            if inst_id:
                print(f"\n{'='*60}")
                print(f"Logs from instance {inst_id}:")
                print('='*60)
                print(logs)
                return
            elif attempt < max_retries - 1:
                print(f"No logs yet, waiting {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                print(f"\n{logs}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fetch logs from chute instances")
    parser.add_argument("chute_id", help="Chute ID or name")
    parser.add_argument("--warmup", "-w", help="Warmup module path (e.g., deploy_xtts_whisper:chute)")
    parser.add_argument("--stream", "-s", action="store_true", help="Stream logs continuously")
    
    args = parser.parse_args()
    check_logs(args.chute_id, warmup_module=args.warmup, stream=args.stream)
