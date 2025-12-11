# SkyrimNet Voice Chutes

TTS/STT chute bundle wrapping Docker images for deployment on [Chutes.ai](https://chutes.ai). For a walkthrough of why we're building these private voice chutes, see the [YouTube explainer](https://www.youtube.com/watch?v=bM10Ca_pbDc).

---

## Quick Start

```bash
# 1. Setup environment (creates .venv, installs deps, registers with Chutes)
./setup.sh

# 2. Activate and use deploy.sh for everything else
source .venv/bin/activate
./deploy.sh
```

---

## Architecture & Methodology

**Strategy:** Keep the upstream SkyrimNet images intact and inject the Chutes runtime so the exact binaries and model stacks keep running. Only fall back to replaying the Docker history onto the `parachutes/python` base (the `deploy_*_auto.py` path) when you explicitly need a fresh, auditable foundation. Even Conda-based or CUDA-heavy vendor images should be wrapped as-is so their behavior stays 1:1 with the reference builds.

### Tooling
We ship the same tooling as `chutes-jumpmaster`, pre-configured for the SkyrimNet assets:
- **Auto-Discovery (`deploy.sh --discover` / `tools/discover_routes.py`):** Boots the upstream image locally, probes OpenAPI/Swagger endpoints, and writes `deploy_*.routes.json`. Run this first so cords match what the service actually exposes.
- **Wrapper SDK (`tools/chute_wrappers.py`):** Injects system Python, the `chutes` user, OpenCL libs, and helper scripts into any base image. Handles route registration, startup waits, and health checks while letting the original container keep its own entrypoint.
- **Image Generator (`tools/create_chute_from_image.py`):** Replays an existing Docker image’s metadata onto the Chutes base image (`deploy_*_auto.py`) when you truly need a rebuilt, Python-first variant.

### Typical Paths
1. **Auto-Discovery + Wrapper (default).** Discover routes, then call `build_wrapper_image()` so the upstream container inherits the Chutes runtime with zero functional drift.
2. **Image Generator (metadata replay).** Use `tools/create_chute_from_image.py` to rebuild the Dockerfile on `parachutes/python` while preserving the upstream entrypoint—handy for auditing or when Conda layers need to be recreated deterministically.
3. **Vanilla chutes (pure Python).** If you want to author a chute from scratch, start in `chutes-jumpmaster/vanilla_examples/` and copy the pattern back here once the service is stable.

### Platform Context
Chutes behaves like a less restrictive, GPU-aware AWS Lambda. Containers stay warm, keep caches, and expose arbitrary HTTP routes. The router expects JSON payloads today, so XTTS/Whisper workflows must wrap audio bytes (base64) or add a thin proxy that converts legacy `multipart/form-data` into JSON.
- **Future direction:** Images can also emit JSON-wrapped audio, which unlocks multipart-style flows without waiting on router changes.

---

## Chutes Overview

| Module | TTS Model | Model Size | Ports | Min VRAM | Concurrency |
|--------|-----------|------------|-------|----------|-------------|
| `deploy_xtts_whisper.py` | XTTS v2 | ~1.5GB | 8020 + 8080 | 16GB | 6 |
| `deploy_vibevoice_whisper.py` | VibeVoice 1.5B | ~3GB | 7860 + 8080 | 16GB | 5 |
| `deploy_higgs_whisper.py` | Higgs Audio 3B | ~6GB | 7860 + 8080 | 16GB | 3 |
| `deploy_zonos_whisper.py` | Zonos 8.8B | ~18GB | 7860 + 8080 | 24GB | 2 |

All chutes include Whisper large-v3-turbo (~1.5GB) for STT.

---

## Workflow

### 1. Setup (`./setup.sh`)

Interactive wizard that:
- Installs `uv` and creates `.venv` (Python 3.11)
- Installs `chutes` SDK and `bittensor<8`
- Helps create/manage Bittensor wallets & Chutes registration

### 2. Deploy (`./deploy.sh`)

Interactive menu for all lifecycle tasks. Press Enter to accept defaults.

| Option | Description |
|--------|-------------|
| **1** Account info | Show username + wallet details from `~/.chutes/config.ini`. |
| **2** List images | Display wrapper images already built. |
| **3** List chutes | Show deployed chutes tied to your account. |
| **4** Build chute from `deploy_*.py` | Wraps `CHUTE_BASE_IMAGE` using the module config. |
| **5** Create `deploy_*_auto.py` | Replays an upstream image onto the Chutes base image. |
| **6** Run in Docker | GPU sanity test of the wrapped container. |
| **7** Run dev mode | Executes the module locally for pure-Python chutes. |
| **8** Deploy chute | Builds (if needed) and schedules on Chutes.ai. |
| **9** Warmup once | Ping the chute so it spins up before traffic. |
| **10** Keep warm loop | Repeated warmups to keep VRAM allocated. |
| **11** Chute status | Calls `chutes chutes get` for live info. |
| **12** Instance logs | Streams logs from active instances. |
| **13** Delete chute | Interactively delete with safety checks. |
| **14** Delete image | Remove local/remote wrapper images. |

**Command Line Usage:**
```bash
# Full lifecycle example
./deploy.sh --discover deploy_xtts_whisper    # Generate .routes.json
./deploy.sh --build deploy_xtts_whisper --local
./deploy.sh --deploy deploy_xtts_whisper --accept-fee
```

### 3. Route Discovery Detail

If you are bringing a new image:
```bash
./deploy.sh --discover deploy_new_service
```
This tool (`tools/discover_routes.py`) will:
1.  Spin up the container with GPUs.
2.  Spider common docs paths (`/openapi.json`, `/docs`, etc.).
3.  Extract all paths and methods.
4.  Write a manifest that `chute_wrappers.py` uses to register cords automatically.

If no manifest exists when you choose “Build chute,” the prompt now defaults to **Yes** so discovery runs before the build.

---

## Deploy Script Structure

Each `deploy_*.py` defines the configuration and invokes the wrapper tools:

```python
from tools.chute_wrappers import build_wrapper_image, load_route_manifest, register_passthrough_routes

# 1. Config & Static Routes (for non-OpenAPI services like whisper.cpp)
CHUTE_STATIC_ROUTES = [{"port": 8080, "method": "POST", "path": "/inference"}]

# 2. Build Wrapper (injects Chutes runtime into base image)
image = build_wrapper_image(..., base_image="elbios/xtts-whisper:latest", ...)

# 3. Define Chute & Hardware
chute = Chute(..., image=image, node_selector=NodeSelector(gpu_count=1), ...)

# 4. Register Routes (Merges static + discovered routes)
register_passthrough_routes(chute, load_route_manifest(static_routes=CHUTE_STATIC_ROUTES), default_port=8020)
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| **Multipart 400 Errors** | Chutes requires JSON. Update client to send JSON-wrapped base64. |
| **No routes discovered** | Service lacks OpenAPI; define `CHUTE_STATIC_ROUTES` in the deploy script. |
| **Container exits immediately** | Check `CHUTE_ENV` in deploy script; missing vars often cause crashes. |
| **Build Segfaults** | Ensure `build_wrapper_image` is used; Conda Python is incompatible with `chutes-inspecto.so`. |

---

## Links

- [Chutes Documentation](https://chutes.ai/docs)
- [SDK Image Reference](https://chutes.ai/docs/sdk-reference/image)
- [Registration Token](https://rtok.chutes.ai/users/registration_token)
