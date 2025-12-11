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

**Strategy:** We deploy SkyrimNet services by "wrapping" the upstream Docker images (`elbios/*`) rather than rebuilding them from scratch. This ensures 1:1 compatibility with the reference implementation while adding the necessary Chutes runtime layer.

### Tooling
We developed custom tooling to automate this process:
- **Auto-Discovery (`deploy.sh --discover`):** Boots the upstream image locally, probes for OpenAPI/Swagger endpoints, and automatically generates a route manifest (`.routes.json`) containing all ports, paths, and methods.
- **Wrapper SDK (`tools/chute_wrappers.py`):** Prepares the image for when the Chutes runtime is injected into the base image, and helps to configure cords with discovered routes.

### Platform Context
Chutes runs persistent, GPU-accelerated containers (like AWS Lambda but stateful).
- **Multipart Limitation:** The Chutes router currently requires JSON payloads for request routing and validation. Legacy `multipart/form-data` requests (used by standard `xtts`/`whisper.cpp` clients) are not supported directly.
- **Workaround:** Clients must wrap binary data (like audio) in a JSON payload (e.g., base64 encoded strings) to pass through the Chutes router. The images providing TTS/STT services must be modified to support this.

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

Interactive menu for all lifecycle tasks. Key options:

| Option | Description |
|--------|-------------|
| **3. Create from image** | Generates a new `deploy_*.py` from *any* Docker image. |
| **4. Build chute** | Builds the wrapper image. Supports `--local` or remote builds. |
| **5. Run in Docker** | Run the wrapped image locally with GPU (verifies wrapping). |
| **7. Deploy chute** | Deploys the built image to Chutes.ai nodes. |
| **9. Chute status** | Get status of a deployed chute. |
| **10. Instance logs** | View logs for a chute instance. |
| **11. Delete chute** | Remove a deployed chute. |
| **12. Delete image** | Remove a built image. |

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
