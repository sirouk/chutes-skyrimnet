# Private Chutes Reference

Concise guide for the SkyrimNet TTS/STT chute bundle. Everything important lives in this repo (`deploy_*.py`, `setup.sh`, `deploy.sh`, `env.example`, `config.ini.example`). For a high-level walkthrough of why we’re building these private voice chutes, see the short explainer video on YouTube [[link](https://www.youtube.com/watch?v=bM10Ca_pbDc)]. For full CLI docs see https://chutes.ai/docs.

---

## Chutes overview

| Module | Description | Suggested GPU | Build command |
| --- | --- | --- | --- |
| `deploy_xtts_whisper.py` | Wrapper for `elbios/xtts-whisper:latest` (Coqui XTTS + Whisper.cpp). | ≥16 GB VRAM | `chutes build deploy_xtts_whisper:chute --wait` |
| `deploy_vibevoice_whisper.py` | Wrapper for `elbios/vibevoice-whisper:latest` (VibeVoice + Whisper.cpp). | ≥24 GB VRAM | `chutes build deploy_vibevoice_whisper:chute --wait` |
| `deploy_higgs_whisper.py` | Wrapper for `elbios/higgs-whisper:latest` (Boson Higgs Audio + Whisper.cpp). | ≥32 GB VRAM | `chutes build deploy_higgs_whisper:chute --wait` |
| `deploy_zonos_whisper.py` | Wrapper for `elbios/zonos-whisper:latest` (Zyphra Zonos + Whisper.cpp). | ≥24 GB VRAM | `chutes build deploy_zonos_whisper:chute --wait` |

Each chute now **starts the upstream Docker image verbatim** and exposes its HTTP API through thin
Chutes cords. Nothing is re-implemented inside this repo; we simply forward requests/responses.

### Runtime summary / exposed cords

- **XTTS + Whisper** (`deploy_xtts_whisper.py`)
  - Ports 8020 (XTTS FastAPI) + 8080 (Whisper.cpp)
  - Every FastAPI route provided by `xtts_api_server` is exposed via passthrough cords:
    `/speakers`, `/speakers_list`, `/languages`, `/get_folders`,
    `/get_models_list`, `/get_tts_settings`, `/sample?path=...` (proxy for `/sample/{file_path}`), `/set_output`,
    `/set_speaker_folder`, `/switch_model`, `/set_tts_settings`, `/tts_stream`,
    `/tts_to_audio/`, `/tts_to_file`, `/create_latents`, `/store_latents`,
    `/create_and_store_latents`, plus Whisper’s `POST /v1/audio/transcriptions`.

- **VibeVoice + Whisper** (`deploy_vibevoice_whisper.py`)
  - Ports 7860 (Gradio wrapper) + 8080 (Whisper.cpp)
  - Passthrough cords: `POST /api/generate_audio`, `/queue/join`, `/queue/status`,
    `/v1/audio/transcriptions`

- **Higgs Audio + Whisper** (`deploy_higgs_whisper.py`)
  - Same passthrough set as VibeVoice (`/api/generate_audio`, `/queue/*`, `/v1/audio/transcriptions`)

- **Zonos + Whisper** (`deploy_zonos_whisper.py`)
  - Ports 7860 (Blocks UI) + 8080 (Whisper.cpp)
  - Passthrough cords: `POST /api/generate_audio`, `/api/predict/`, `/queue/join`,
    `/queue/status`, `GET /file`, `POST /v1/audio/transcriptions`

Most cords use Chutes’ native **passthrough** mode, so requests go straight to the vendor process already
running inside the Docker image. The lone exception is the XTTS sample-download endpoint
(`/sample/{file_path}`), which needs a tiny helper because of its path wildcard; that helper still forwards
the payload verbatim. Either way, keep sending the **exact JSON / multipart bodies** those services
document (Gradio queue payloads, Whisper form uploads, etc.).

Environment variables (`env.example` → `.env`):
```
CHUTES_USERNAME=skyrimnet

# optional overrides if the upstream images ever change their entrypoints/ports
XTTS_ENTRYPOINT=/usr/local/bin/docker-entrypoint.sh
XTTS_HTTP_PORT=8020
XTTS_WHISPER_PORT=8080

VIBEVOICE_ENTRYPOINT=/usr/local/bin/docker-entrypoint.sh
VIBEVOICE_HTTP_PORT=7860
VIBEVOICE_WHISPER_PORT=8080

HIGGS_ENTRYPOINT=/usr/local/bin/docker-entrypoint.sh
HIGGS_HTTP_PORT=7860
HIGGS_WHISPER_PORT=8080

ZONOS_ENTRYPOINT=/usr/local/bin/docker-entrypoint.sh
ZONOS_HTTP_PORT=7860
ZONOS_WHISPER_PORT=8080
```

---

## Workflow (see `deploy.sh`)

1. **Bootstrap CLI** (`setup.sh`): create `.venv`, install `chutes`/`bittensor`, run `chutes register`. Copy `config.ini.example` → `~/.chutes/config.ini` (or keep a local `.config.ini`) and fill in your real username, IDs, and addresses.
2. **Study/update a chute**: edit env vars at top of `deploy_*.py` if needed (username, model IDs, etc.).
3. **Local validation (optional)**:
   ```bash
   chutes build deploy_xtts_whisper:chute --local --debug
   chutes run deploy_xtts_whisper:chute --dev --port 8000 --debug
   ```
   Local builds exec `/usr/bin/docker`. On macOS, symlink `/usr/local/bin/docker` → `/usr/bin/docker` if missing.
4. **Remote build** (required before deploy):
   ```bash
   chutes build deploy_xtts_whisper:chute --wait
   ```
   API enforces ≥$50 USD balance (in addition to TAO fees) before accepting an image upload.
5. **Deploy**:
   ```bash
   chutes deploy deploy_xtts_whisper:chute --accept-fee [--public]
   chutes chutes list
   chutes chutes get <chute-name>
   ```
   Use the other CLI commands as needed (`chutes report`, `chutes warmup`, etc.).

`deploy.sh` contains the ordered shell sequence (study → local build → local run → remote build → deploy). `setup.sh` explains registration with sanitized instructions.

---

## Payloads / testing

Use the vendor’s own documentation (or live Gradio UI) for request bodies and workflows. Because we
simply proxy HTTP, any payload that worked against the original container will work against the
Chutes-hosted endpoint. For local smoke tests you can still run `chutes run <module>:chute --dev`,
but note that the upstream servers expect a GPU and will fail-fast on CPU-only machines.

---

## Repository layout

```
.
├── deploy_xtts_whisper.py
├── deploy_vibevoice_whisper.py
├── deploy_higgs_whisper.py
├── deploy_zonos_whisper.py
├── deploy_example.py
├── deploy.sh
├── setup.sh
├── env.example
├── config.ini.example # dummy template
├── README.md
└── .gitignore
```

No vendored `chutes/` directory is kept—scripts rely on the `chutes` package installed via pip.

---

## Frequently used commands

```bash
chutes build <module>:chute --local --debug       # local Docker build
chutes build <module>:chute --wait                # remote build (needs ≥$50 balance)
chutes run <module>:chute --dev --dev-job-data-path test_job.json
chutes deploy <module>:chute --accept-fee [--public]
chutes chutes list && chutes chutes get <name>
chutes report <invocation_id>
chutes warmup <module>:chute
```

Run `chutes --help` or check the upstream docs for the full command set.

---

## Troubleshooting

- **`/usr/bin/docker` missing** with `--local`: install Docker Desktop and symlink it.
- **“You must have a balance of >= $50 to create images.”**: add funds before remote builds/deployments.
- **Credentials/ENV issues**: ensure `.env` and `~/.chutes/config.ini` match the templates (`env.example`, `config.ini.example`).
- **Remote imports**: already handled—each deploy script inlines everything required.

---
