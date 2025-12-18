#!/usr/bin/env bash
set -euo pipefail

# Runs a deterministic set of XTTS+Whisper smoke tests against the deployed chute
# defined in deploy_xtts_whisper.py. The script exercises every public cord in
# that module (speaker + language discovery, folder/model management, all XTTS
# POST flows, and both Whisper endpoints) and asserts key response fields such as
# presence of file paths and audio content-types.
#
# Usage:
#   ./test_xtts_whisper.sh            # use defaults from .env
#   CHUTE_BASE_URL=... ./test_xtts_whisper.sh
#   SKIP_WARMUP=1 ./test_xtts_whisper.sh   # skip chutes warmup
#
# Requirements:
#   - .env must contain CHUTES_API_KEY=cpk_...
#   - curl + python3 available
#   - Sample audio lives at tests/create_and_store_latents.wav

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${ENV_FILE}"
  set +a
fi

CHUTE_BASE_URL_DEFAULT="https://sirouk-dev-xtts-whisper.chutes.ai"
CHUTE_BASE_URL="${CHUTE_BASE_URL:-${CHUTE_BASE_URL_DEFAULT}}"
CHUTE_ID_DEFAULT="16284ea5-b5bc-596d-9c37-a84229aa6165"
CHUTE_ID="${CHUTE_ID:-${CHUTE_ID_DEFAULT}}"
CHUTE_WARMUP_TARGET="${CHUTE_WARMUP_TARGET:-${CHUTE_ID}}"

CHUTES_API_KEY="${CHUTES_API_KEY:-${CHUTES_BEARER:-}}"
CHUTES_AUTHORIZATION="${CHUTES_AUTHORIZATION:-${AUTHORIZATION:-}}"
if [[ -z "${CHUTES_AUTHORIZATION}" && -n "${CHUTES_API_KEY}" ]]; then
  if [[ "${CHUTES_API_KEY}" == "Bearer "* ]]; then
    CHUTES_AUTHORIZATION="${CHUTES_API_KEY}"
  else
    CHUTES_AUTHORIZATION="Bearer ${CHUTES_API_KEY}"
  fi
fi
[[ -n "${CHUTES_AUTHORIZATION}" ]] || { echo "ERROR: CHUTES_AUTHORIZATION/CHUTES_API_KEY not set" >&2; exit 1; }

CONNECT_TIMEOUT_SECONDS="${CONNECT_TIMEOUT_SECONDS:-10}"
MAX_TIME_SECONDS="${MAX_TIME_SECONDS:-120}"
MAX_RETRIES="${MAX_RETRIES:-3}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-5}"
LANGUAGE="${LANGUAGE:-en}"
SPEAKER_NAME="${SPEAKER_NAME:-malebrute}"
TTS_TEXT="${TTS_TEXT:-Greetings from SkyrimNet. The Greybeards await you at High Hrothgar.}"
WHISPER_RESPONSE_FORMAT="${WHISPER_RESPONSE_FORMAT:-json}"
AUDIO_SAMPLE_PATH="${AUDIO_SAMPLE_PATH:-${REPO_ROOT}/tests/create_and_store_latents.wav}"
[[ -f "${AUDIO_SAMPLE_PATH}" ]] || { echo "ERROR: sample audio not found at ${AUDIO_SAMPLE_PATH}" >&2; exit 1; }

PY_BIN="${PY_BIN:-}"
if [[ -z "${PY_BIN}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PY_BIN="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="$(command -v python3)"
  fi
fi
[[ -n "${PY_BIN}" ]] || { echo "ERROR: python3 not found (set PY_BIN)" >&2; exit 1; }

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

body_path() {
  printf "%s/%s.body" "${TMP_DIR}" "$1"
}

binary_path() {
  printf "%s/%s.bin" "${TMP_DIR}" "$1"
}

headers_path() {
  printf "%s/%s.headers" "${TMP_DIR}" "$1"
}

assert_json_keys() {
  local name="$1"; shift
  [[ "$#" -gt 0 ]] || return 0
  local file
  file="$(body_path "${name}")"
  "${PY_BIN}" - "$file" "$name" "$@" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
keys = sys.argv[3:]
data = json.loads(path.read_text())
missing = [key for key in keys if key not in data]
if missing:
    raise SystemExit(f"{name} missing keys: {', '.join(missing)}")
PY
}

assert_content_type() {
  local name="$1" needle="$2"
  local file
  file="$(headers_path "${name}")"
  "${PY_BIN}" - "$file" "$name" "$needle" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
needle = sys.argv[3].lower()
for line in path.read_text().splitlines():
    header = line.lower()
    if header.startswith("content-type") and needle in header:
        break
else:
    raise SystemExit(f"{sys.argv[2]} missing Content-Type containing '{needle}'")
PY
}

assert_file_min_size() {
  local name="$1" min_bytes="$2"
  local file
  file="$(binary_path "${name}")"
  if [[ ! -f "${file}" ]]; then
    die "${name} binary response not found at ${file}"
  fi
  local size
  size=$(wc -c <"${file}")
  if (( size < min_bytes )); then
    die "${name} response too small (${size} bytes < ${min_bytes})"
  fi
}

assert_no_error_field() {
  local name="$1"
  local file
  file="$(body_path "${name}")"
  "${PY_BIN}" - "$file" "$name" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
if isinstance(data, dict):
    for key in ("error", "detail"):
        if key in data:
            raise SystemExit(f"{sys.argv[2]} returned error payload: {data[key]!r}")
PY
}

warn() { echo "WARN: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

pretty_print_json() {
  local file="$1" limit="${2:-800}"
  "${PY_BIN}" - <<'PY' "${file}" "${limit}"
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
limit = int(sys.argv[2])
try:
    data = json.loads(path.read_text())
    text = json.dumps(data, indent=2)
except Exception:
    text = path.read_text()
if len(text) > limit:
    print(text[:limit] + "... (truncated)")
else:
    print(text)
PY
}

run_request() {
  local name="$1" method="$2" path="$3" body_file="${4:-}" output_type="${5:-json}"
  local url="${CHUTE_BASE_URL%/}${path}"
  local body_path
  if [[ "${output_type}" == "binary" ]]; then
    body_path="${TMP_DIR}/${name}.bin"
  else
    body_path="${TMP_DIR}/${name}.body"
  fi
  local headers_path="${TMP_DIR}/${name}.headers"

  echo "==> ${method} ${path}"

  local -a curl_args=(
    -sS
    -D "${headers_path}"
    -o "${body_path}"
    --connect-timeout "${CONNECT_TIMEOUT_SECONDS}"
    --max-time "${MAX_TIME_SECONDS}"
    -X "${method}"
    -H "Authorization: ${CHUTES_AUTHORIZATION}"
  )

  if [[ "${output_type}" == "binary" ]]; then
    curl_args+=( -H "Accept: */*" )
  else
    curl_args+=( -H "Accept: application/json" )
  fi

  if [[ -n "${body_file}" ]]; then
    curl_args+=( -H "Content-Type: application/json" --data-binary "@${body_file}" )
  fi

  curl_args+=( "${url}" )

  local attempt=1
  while true; do
    set +e
    local http_code
    http_code="$(curl "${curl_args[@]}" -w '%{http_code}')"
    local curl_rc=$?
    set -e

    if (( curl_rc != 0 )); then
      if (( attempt >= MAX_RETRIES )); then
        die "curl failed for ${path} (rc=${curl_rc})"
      fi
      warn "curl failed for ${path} (rc=${curl_rc}); retrying in ${RETRY_DELAY_SECONDS}s"
      sleep "${RETRY_DELAY_SECONDS}"
      attempt=$((attempt + 1))
      continue
    fi

    if [[ "${http_code}" =~ ^2 ]]; then
      echo "HTTP ${http_code}"
      if [[ "${output_type}" == "binary" ]]; then
        local size
        size=$(wc -c <"${body_path}")
        echo "    saved binary response to ${body_path} (${size} bytes)"
      else
        pretty_print_json "${body_path}" 600
      fi
      echo ""
      break
    fi

    echo "HTTP ${http_code}" >&2
    if [[ -s "${body_path}" ]]; then
      pretty_print_json "${body_path}" 400 >&2
    fi

    if (( attempt >= MAX_RETRIES )); then
      die "request ${method} ${path} failed"
    fi

    warn "retrying ${method} ${path} in ${RETRY_DELAY_SECONDS}s (${attempt}/${MAX_RETRIES})"
    sleep "${RETRY_DELAY_SECONDS}"
    attempt=$((attempt + 1))
  done
}

prepare_payloads() {
  local latents_file="${TMP_DIR}/create_and_store_latents.json"
  local tts_file="${TMP_DIR}/tts_to_audio.json"
  local whisper_file="${TMP_DIR}/whisper_inference.json"

  "${PY_BIN}" - <<'PY' "${latents_file}" "${tts_file}" "${whisper_file}" "${AUDIO_SAMPLE_PATH}" "${LANGUAGE}" "${SPEAKER_NAME}" "${TTS_TEXT}" "${WHISPER_RESPONSE_FORMAT}"
import base64, json, pathlib, sys
latents_file, tts_file, whisper_file, audio_path, language, speaker, text, whisper_format = sys.argv[1:9]
audio_bytes = pathlib.Path(audio_path).read_bytes()
audio_b64 = base64.b64encode(audio_bytes).decode('ascii')

payloads = {
    latents_file: {
        "language": language,
        "speaker_name": speaker,
        "wav_file_base64": audio_b64,
    },
    tts_file: {
        "text": text,
        "language": language,
        "speaker_name": speaker,
        "temperature": 0.7,
        "length_scale": 1.0,
        "enable_text_splitting": False,
    },
    whisper_file: {
        "file_base64": audio_b64,
        "temperature": 0.0,
        "response_format": whisper_format,
    },
}

for path, payload in payloads.items():
    pathlib.Path(path).write_text(json.dumps(payload))
PY

  echo "${latents_file}:${tts_file}:${whisper_file}"
}

warmup_chute() {
  if [[ "${SKIP_WARMUP:-0}" == "1" ]]; then
    return
  fi

  local chutes_cli="${CHUTES_CLI:-}"
  if [[ -z "${chutes_cli}" ]]; then
    if [[ -x "${REPO_ROOT}/.venv/bin/chutes" ]]; then
      chutes_cli="${REPO_ROOT}/.venv/bin/chutes"
    elif command -v chutes >/dev/null 2>&1; then
      chutes_cli="$(command -v chutes)"
    fi
  fi

  if [[ -z "${chutes_cli}" ]]; then
    warn "chutes CLI not found; skipping warmup"
    return
  fi

  local target="${CHUTE_WARMUP_TARGET:-${CHUTE_ID}}"
  if [[ -z "${target}" ]]; then
    target="deploy_xtts_whisper:chute"
  fi

  echo "==> warming chute: ${target}"
  if ! (cd "${REPO_ROOT}" && "${chutes_cli}" warmup "${target}"); then
    warn "warmup failed (continuing anyway)"
  fi
}

main() {
  echo "Running XTTS/Whisper smoke tests against ${CHUTE_BASE_URL}"
  echo "  language=${LANGUAGE} speaker=${SPEAKER_NAME}"
  echo "  audio_sample=${AUDIO_SAMPLE_PATH}"
  echo ""

  warmup_chute

  local latents_payload tts_payload whisper_payload
  IFS=":" read -r latents_payload tts_payload whisper_payload < <(prepare_payloads)

  # Discovery endpoints
  run_request "speakers_list" "GET" "/speakers_list"
  run_request "speakers" "GET" "/speakers"
  run_request "languages" "GET" "/languages"
  run_request "get_folders" "GET" "/get_folders"
  assert_json_keys "get_folders" "speaker_folder" "output_folder" "model_folder"
  run_request "get_models_list" "GET" "/get_models_list"
  run_request "get_tts_settings" "GET" "/get_tts_settings"
  assert_json_keys "get_tts_settings" "temperature" "speed" "enable_text_splitting"

  # Derived payloads for stateful POST routes
  local folders_body models_body settings_body
  folders_body="$(body_path get_folders)"
  models_body="$(body_path get_models_list)"
  settings_body="$(body_path get_tts_settings)"

  local set_output_payload="${TMP_DIR}/set_output.json"
  "${PY_BIN}" - <<'PY' "${folders_body}" "${set_output_payload}"
import json, pathlib, sys
folders = json.loads(pathlib.Path(sys.argv[1]).read_text())
payload = {"output_folder": folders.get("output_folder") or "/app/output"}
pathlib.Path(sys.argv[2]).write_text(json.dumps(payload))
PY

  local set_speaker_payload="${TMP_DIR}/set_speaker_folder.json"
  "${PY_BIN}" - <<'PY' "${folders_body}" "${set_speaker_payload}"
import json, pathlib, sys
folders = json.loads(pathlib.Path(sys.argv[1]).read_text())
payload = {"speaker_folder": folders.get("speaker_folder") or "speakers/"}
pathlib.Path(sys.argv[2]).write_text(json.dumps(payload))
PY

  local switch_model_payload="${TMP_DIR}/switch_model.json"
  "${PY_BIN}" - <<'PY' "${models_body}" "${switch_model_payload}"
import json, pathlib, sys
models = json.loads(pathlib.Path(sys.argv[1]).read_text() or "[]")
model = "v2.0.2"
if isinstance(models, list) and models:
    model = models[0]
pathlib.Path(sys.argv[2]).write_text(json.dumps({"model_name": model}))
PY

  local set_tts_payload="${TMP_DIR}/set_tts_settings.json"
  "${PY_BIN}" - <<'PY' "${settings_body}" "${set_tts_payload}"
import json, pathlib, sys
settings = json.loads(pathlib.Path(sys.argv[1]).read_text())
temperature = settings.get("temperature", 0.7)
settings["temperature"] = round(min(temperature + 0.05, 1.5), 3)
pathlib.Path(sys.argv[2]).write_text(json.dumps(settings))
PY

  local whisper_model_name="${WHISPER_MODEL:-large-v3-turbo}"
  local whisper_load_payload="${TMP_DIR}/whisper_load.json"
  "${PY_BIN}" - <<'PY' "${whisper_load_payload}" "${whisper_model_name}"
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({"model": sys.argv[2]}))
PY

  run_request "set_output" "POST" "/set_output" "${set_output_payload}"
  assert_json_keys "set_output" "message"
  run_request "set_speaker_folder" "POST" "/set_speaker_folder" "${set_speaker_payload}"
  assert_json_keys "set_speaker_folder" "message"
  run_request "switch_model" "POST" "/switch_model" "${switch_model_payload}"
  run_request "set_tts_settings" "POST" "/set_tts_settings" "${set_tts_payload}"
  assert_json_keys "set_tts_settings" "message"

  # Latent management flows
  run_request "create_latents" "POST" "/create_latents" "${latents_payload}"
  assert_json_keys "create_latents" "latents"
  local store_latents_payload="${TMP_DIR}/store_latents.json"
  "${PY_BIN}" - <<'PY' "$(body_path create_latents)" "${store_latents_payload}" "${LANGUAGE}" "${SPEAKER_NAME}"
import json, pathlib, sys
body_path, dest_path, language, speaker = sys.argv[1:5]
data = json.loads(pathlib.Path(body_path).read_text())
latents = data.get("latents")
if latents is None:
    raise SystemExit("create_latents response missing 'latents'")
payload = {"language": language, "speaker_name": speaker, "latents": latents}
pathlib.Path(dest_path).write_text(json.dumps(payload))
PY
  run_request "store_latents" "POST" "/store_latents" "${store_latents_payload}"
  assert_json_keys "store_latents" "message"
  run_request "create_and_store_latents" "POST" "/create_and_store_latents" "${latents_payload}"
  assert_json_keys "create_and_store_latents" "file_path"

  # TTS endpoints
  run_request "tts_to_audio" "POST" "/tts_to_audio" "${tts_payload}" "binary"
  assert_content_type "tts_to_audio" "audio"
  assert_file_min_size "tts_to_audio" 200
  run_request "tts_to_file" "POST" "/tts_to_file" "${tts_payload}"
  assert_json_keys "tts_to_file" "file_path"

  # Whisper endpoints
  run_request "whisper_load_get" "GET" "/load"
  run_request "whisper_load_post" "POST" "/whisper_load" "${whisper_load_payload}"
  assert_no_error_field "whisper_load_post"
  run_request "whisper_inference" "POST" "/inference" "${whisper_payload}"
  assert_json_keys "whisper_inference" "text"

  echo "All XTTS/Whisper endpoints responded successfully."
  echo "Artifacts stored in ${TMP_DIR} (will be deleted when this script exits)."
}

main "$@"
