# Private Chutes Reference

Concise guide for the SkyrimNet TTS/STT chute bundle. Everything important lives in this repo (`deploy_*.py`, `setup.sh`, `deploy.sh`, `env.example`, `config.ini.example`). For a high-level walkthrough of why we’re building these private voice chutes, see the short explainer video on YouTube [[link](https://www.youtube.com/watch?v=bM10Ca_pbDc)]. For full CLI docs see https://chutes.ai/docs.

---

## Chutes overview

| Module | Description | Suggested GPU | Build command |
| --- | --- | --- | --- |
| `deploy_xtts_whisper.py` | Coqui XTTS v2 voice cloning + Whisper STT. | ≥16 GB VRAM | `chutes build deploy_xtts_whisper:chute --wait` |
| `deploy_vibevoice_whisper.py` | VibeVoice-1.5B long-form dialogue (multi-speaker). | ≥24 GB VRAM | `chutes build deploy_vibevoice_whisper:chute --wait` |
| `deploy_higgs_whisper.py` | Boson Higgs Audio v2 expressive narration. | ≥32 GB VRAM | `chutes build deploy_higgs_whisper:chute --wait` |
| `deploy_zonos_whisper.py` | Zyphra Zonos v0.1 hybrid multilingual cloning. | ≥24 GB VRAM | `chutes build deploy_zonos_whisper:chute --wait` |

All chutes expose:
- `POST /speak` → returns `{ audio_b64, sample_rate, duration_seconds, meta }`
- `POST /transcribe` → Whisper transcript/segments metadata

### Image / runtime notes

- **XTTS + Whisper**:
  - *Image*: base Python 3.11.9 image with `ffmpeg`, `espeak-ng`, and Python deps (`TTS`, `torch/torchaudio`, `faster-whisper`, `librosa`, `soundfile`). Bundles a local placeholder voice (`assets/default_voice.wav`) into `/app/assets/xtts_default.wav`.
  - *Runtime*: inline Pydantic schemas, temporary audio helpers, XTTS generator with locking, and faster-whisper transcriber. `/speak` enforces text/script, supports optional voice cloning; `/transcribe` proxies faster-whisper. `NodeSelector` is pinned to Chutes-approved GPUs (`h100`, `h200`, `b200`).

- **VibeVoice + Whisper**:
  - *Image*: installs `ffmpeg`, `vibevoice==0.0.1`, torch stack, `soundfile`, `librosa`, `faster-whisper`, and copies the same placeholder voice to `/app/assets/vibevoice_default.wav`.
  - *Runtime*: script auto-formatting (adds “Speaker i” tags when needed), voice loading/resampling, CFG + token overrides, and a whisper endpoint. All helpers live inside the file. GPU selector includes only `h100`, `h100_sxm`, `h200` per platform rules.

- **Higgs Audio + Whisper**:
  - *Image*: installs `ffmpeg` and pulls Boson’s `higgs-audio` repo at a fixed commit plus torch stack and whisper deps.
  - *Runtime*: builds ChatML prompts (`boson_multimodal` helpers imported lazily), enforces text/script check, and returns expressive narration audio; whisper cord matches the others. Requests `h100_sxm`, `h100_nvl`, or `mi300x`.

- **Zonos + Whisper**:
  - *Image*: adds `ffmpeg`, `espeak-ng`, `libespeak-ng1`, installs `zonos==0.1.0.dev0`, torch stack, whisper deps, and copies the placeholder voice to `/app/assets/zonos_default.wav`.
  - *Runtime*: handles torchaudio resampling, voice cloning, CFG + max token overrides, and whisper transcription. Helpers are all embedded so the module is self-contained. GPU selector limited to `h100`, `h100_sxm`, `h200`.

Each deploy script embeds its own request models, temporary audio helpers, and Whisper wrapper—no shared imports—so remote miners can run the lone `.py` file uploaded by `chutes build`.

Environment variables (`env.example` → `.env`):
```
CHUTES_USERNAME=...
WHISPER_MODEL=large-v3-turbo          # fallback
XTTS_MODEL_ID=...
VIBEVOICE_MODEL_ID=...
HIGGS_MODEL_ID=...
HIGGS_AUDIO_TOKENIZER=...
ZONOS_MODEL_ID=...
# optional per-engine overrides e.g. XTTS_WHISPER_MODEL, VIBEVOICE_WHISPER_MODEL, etc.
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

## Local dev testing

```bash
cat > speak_payload.json <<'EOF'
{
  "text": "Welcome to SkyrimNet private chutes!",
  "language": "en",
  "cfg_scale": 1.3
}
EOF

# terminal 1 (Ctrl+C to stop)
chutes run deploy_xtts_whisper:chute --dev --port 8000 --debug

# terminal 2 (while the chute runs)
curl -sS -X POST http://127.0.0.1:8000/speak \
     -H "Content-Type: application/json" \
     --data @speak_payload.json \
     --output output.wav
```

Swap the payload and endpoint to exercise `/transcribe`. When satisfied, stop the dev server with `Ctrl+C`.

---

## Request payload cheatsheet

```jsonc
// /speak
{
  "text": "fallback text if script omitted",
  "script": "Speaker 0: ...\nSpeaker 1: ...",
  "language": "en-us",
  "voice_sample_b64": "<base64 wav/mp3>",
  "temperature": 0.2,
  "top_p": 0.9,
  "cfg_scale": 1.3,
  "max_new_tokens": 2048,
  "num_speakers": 2
}
```

```jsonc
// /transcribe
{
  "audio_b64": "<base64 wav/mp3>",
  "translate_to_english": false,
  "language": null
}
```

Engine-specific notes live near the Pydantic models in each deploy script (e.g., VibeVoice expects `Speaker i:` formatting, Higgs uses `temperature`/`top_p`, XTTS/Zonos lean on `voice_sample_b64`, etc.).

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
