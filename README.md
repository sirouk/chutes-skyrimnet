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

## Chutes Overview

| Module | Base Image | Ports | GPU |
|--------|-----------|-------|-----|
| `deploy_xtts_whisper.py` | `elbios/xtts-whisper:latest` | 8020 (XTTS) + 8080 (Whisper) | ≥16 GB |
| `deploy_vibevoice_whisper.py` | `elbios/vibevoice-whisper:latest` | 7860 (Gradio) + 8080 (Whisper) | ≥24 GB |
| `deploy_higgs_whisper.py` | `elbios/higgs-whisper:latest` | 7860 (Gradio) + 8080 (Whisper) | ≥32 GB |
| `deploy_zonos_whisper.py` | `elbios/zonos-whisper:latest` | 7860 (Gradio) + 8080 (Whisper) | ≥24 GB |

Each chute wraps the upstream Docker image and exposes its HTTP API via passthrough cords.

---

## Workflow

### 1. Setup (`./setup.sh`)

Interactive wizard that:
- Installs `uv` if missing
- Creates `.venv` with Python 3.11
- Installs `chutes` and `bittensor<8`
- Helps create/manage Bittensor wallets
- Runs `chutes register` for account setup

Options: `--force`, `--non-interactive`, `--wallet-name NAME`

### 2. Deploy (`./deploy.sh`)

Interactive menu with options:

| Option | Description |
|--------|-------------|
| 1. List images | Show built Docker images |
| 2. List chutes | Show deployed chutes |
| 3. Build chute | Local or remote build (prompts for route discovery first) |
| 4. Run in Docker | Run wrapped image with GPU (for testing) |
| 5. Run dev mode | Run on host (Python chutes) |
| 6. Deploy chute | Deploy to Chutes.ai |
| 7. Chute status | Get status of a deployed chute |
| 8. Delete chute | Remove a deployed chute |
| 9. Account info | Show username and payment address |

Or use flags directly:

```bash
./deploy.sh --discover deploy_xtts_whisper    # Discover routes from running container
./deploy.sh --build deploy_xtts_whisper --local
./deploy.sh --run-docker deploy_xtts_whisper  # Run in Docker with GPU
./deploy.sh --deploy deploy_xtts_whisper --accept-fee
./deploy.sh --status xtts-whisper
```

### 3. Route Discovery

Routes are auto-discovered from running containers:

```bash
./deploy.sh --discover deploy_xtts_whisper
```

This:
1. Starts the base Docker image with GPU
2. Probes for OpenAPI endpoints (`/openapi.json`, `/docs.json`, etc.)
3. Generates `deploy_xtts_whisper.routes.json`

For services without OpenAPI (like whisper.cpp), define static routes in `CHUTE_STATIC_ROUTES`.

---

## Deploy Script Structure

Each `deploy_*.py` follows this pattern:

```python
from chutes.chute import Chute, NodeSelector
from tools.chute_wrappers import (
    build_wrapper_image, load_route_manifest,
    register_passthrough_routes, wait_for_services, probe_services,
)

# Identification
CHUTE_NAME = "xtts-whisper"
CHUTE_TAG = "tts-stt-v0.1.1"
CHUTE_BASE_IMAGE = "elbios/xtts-whisper:latest"
SERVICE_PORTS = [8020, 8080]

# Environment variables (used during discovery and runtime)
CHUTE_ENV = {
    "WHISPER_MODEL": "large-v3-turbo",
    "XTTS_MODEL_ID": "tts_models/multilingual/multi-dataset/xtts_v2",
}

# Static routes (for services without OpenAPI, merged with discovered routes)
CHUTE_STATIC_ROUTES = [
    {"path": "/inference", "method": "POST", "port": 8080, "target_path": "/inference"},
    {"path": "/v1/audio/transcriptions", "method": "POST", "port": 8080, "target_path": "/inference"},
]

# Build image
image = build_wrapper_image(USERNAME, CHUTE_NAME, CHUTE_TAG, CHUTE_BASE_IMAGE)

# Create chute
chute = Chute(
    username=USERNAME,
    name=CHUTE_NAME,
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16),
)

# Register routes
register_passthrough_routes(chute, load_route_manifest(static_routes=CHUTE_STATIC_ROUTES), SERVICE_PORTS[0])

@chute.on_startup()
async def boot(self):
    await wait_for_services(SERVICE_PORTS, timeout=600)

@chute.cord(public_api_path="/health", public_api_method="GET", method="GET")
async def health_check(self) -> dict:
    errors = await probe_services(SERVICE_PORTS, timeout=5)
    return {"status": "unhealthy", "errors": errors} if errors else {"status": "healthy"}
```

---

## Repository Layout

```
.
├── setup.sh                         # Environment setup wizard
├── deploy.sh                        # Interactive deploy CLI
├── config.ini.example               # Chutes config template
├── deploy_xtts_whisper.py           # XTTS + Whisper chute
├── deploy_vibevoice_whisper.py      # VibeVoice + Whisper chute
├── deploy_higgs_whisper.py          # Higgs Audio + Whisper chute
├── deploy_zonos_whisper.py          # Zonos + Whisper chute
├── deploy_example.py                # Template for new chutes
├── deploy_*.routes.json             # Generated route manifests (gitignored)
├── tools/
│   ├── chute_wrappers.py            # Image building & route registration
│   └── discover_routes.py           # Route auto-discovery
└── README.md
```

---

## Helper Functions (`tools/chute_wrappers.py`)

| Function | Description |
|----------|-------------|
| `build_wrapper_image()` | Create Chutes-compatible image from base Docker image |
| `load_route_manifest()` | Load routes from `.routes.json`, merge with static routes |
| `register_passthrough_routes()` | Register routes as passthrough cords on chute |
| `wait_for_services()` | Block until service ports accept connections |
| `probe_services()` | Health check, returns list of errors |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `/usr/bin/docker` missing | Install Docker Desktop; on macOS symlink to `/usr/bin/docker` |
| "balance >= $50" error | Add funds before remote builds |
| Container exits immediately | Check `CHUTE_ENV` for required env vars |
| No routes discovered | Service may not expose OpenAPI; use `CHUTE_STATIC_ROUTES` |
| `InvalidPath` error | Chutes SDK doesn't support path params `{id}`, file extensions, or root `/` |

---

## Links

- [Chutes Documentation](https://chutes.ai/docs)
- [SDK Image Reference](https://chutes.ai/docs/sdk-reference/image)
- [Registration Token](https://rtok.chutes.ai/users/registration_token)
