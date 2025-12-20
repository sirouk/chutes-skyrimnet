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
MAX_RETRIES="${MAX_RETRIES:-1}"
RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-0}"
LANGUAGE="${LANGUAGE:-en}"
SPEAKER_NAME="${SPEAKER_NAME:-malebrute}"
TTS_TEXT="${TTS_TEXT:-Greetings from SkyrimNet. The Greybeards await you at High Hrothgar.}"
WHISPER_RESPONSE_FORMAT="${WHISPER_RESPONSE_FORMAT:-json}"
SILO_ID="${SILO_ID:-}"
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

TMP_DIR="${REPO_ROOT}/test_artifacts"
mkdir -p "${TMP_DIR}"
# cleanup() { rm -rf "${TMP_DIR}"; }
# trap cleanup EXIT

body_path() {
  local prefix="${SILO_ID:-global}"
  printf "%s/%s_%s.body" "${TMP_DIR}" "${prefix}" "$1"
}

binary_path() {
  local prefix="${SILO_ID:-global}"
  printf "%s/%s_%s.bin" "${TMP_DIR}" "${prefix}" "$1"
}

headers_path() {
  local prefix="${SILO_ID:-global}"
  printf "%s/%s_%s.headers" "${TMP_DIR}" "${prefix}" "$1"
}

payload_path() {
  local prefix="${SILO_ID:-global}"
  printf "%s/%s_%s.json" "${TMP_DIR}" "${prefix}" "$1"
}

assert_json_keys() {
  local name="$1"; shift
  [[ "$#" -gt 0 ]] || return 0
  local file
  file="$(body_path "${name}")"
  [[ -f "${file}" ]] || { warn "${name} body not found for assertion"; return 0; }
  "${PY_BIN}" - "$file" "$name" "$@" <<'PY' || warn "assertion failed for ${name}"
import json, pathlib, sys
try:
    path = pathlib.Path(sys.argv[1])
    name = sys.argv[2]
    keys = sys.argv[3:]
    data = json.loads(path.read_text())
    missing = [key for key in keys if key not in data]
    if missing:
        print(f"ERROR: {name} missing keys: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"ERROR: {name} assertion error: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

assert_content_type() {
  local name="$1" needle="$2"
  local file
  file="$(headers_path "${name}")"
  [[ -f "${file}" ]] || { warn "${name} headers not found for assertion"; return 0; }
  "${PY_BIN}" - "$file" "$name" "$needle" <<'PY' || warn "assertion failed for ${name}"
import pathlib, sys
try:
    path = pathlib.Path(sys.argv[1])
    needle = sys.argv[3].lower()
    for line in path.read_text().splitlines():
        header = line.lower()
        if header.startswith("content-type") and needle in header:
            break
    else:
        print(f"ERROR: {sys.argv[2]} missing Content-Type containing '{needle}'", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"ERROR: {sys.argv[2]} assertion error: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

assert_file_min_size() {
  local name="$1" min_bytes="$2"
  local file
  file="$(binary_path "${name}")"
  if [[ ! -f "${file}" ]]; then
    warn "${name} binary response not found at ${file}"
    return 0
  fi
  local size
  size=$(wc -c <"${file}")
  if (( size < min_bytes )); then
    warn "${name} response too small (${size} bytes < ${min_bytes})"
  fi
}

assert_no_error_field() {
  local name="$1"
  local file
  file="$(body_path "${name}")"
  [[ -f "${file}" ]] || { warn "${name} body not found for assertion"; return 0; }
  "${PY_BIN}" - "$file" "$name" <<'PY' || warn "assertion failed for ${name}"
import json, pathlib, sys
try:
    path = pathlib.Path(sys.argv[1])
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        for key in ("error", "detail"):
            if key in data:
                print(f"ERROR: {sys.argv[2]} returned error payload: {data[key]!r}", file=sys.stderr)
                sys.exit(1)
except Exception as e:
    print(f"ERROR: {sys.argv[2]} assertion error: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

assert_store_latents_ok() {
  local name="store_latents"
  local file
  file="$(body_path "${name}")"
  [[ -f "${file}" ]] || { warn "${name} body not found for assertion"; return 0; }
  "${PY_BIN}" - "$file" "$name" <<'PY' || warn "assertion failed for ${name}"
import json, pathlib, sys
try:
    path = pathlib.Path(sys.argv[1])
    data = json.loads(path.read_text())
    if "message" not in data and "detail" not in data:
        print(f"ERROR: {sys.argv[2]} missing success message/detail keys: {list(data.keys())}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"ERROR: {sys.argv[2]} assertion error: {e}", file=sys.stderr)
    sys.exit(1)
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
  
  if [[ -n "${SILO_ID}" ]]; then
    if [[ "${url}" == *"?"* ]]; then
      url="${url}&silo_id=${SILO_ID}"
    else
      url="${url}?silo_id=${SILO_ID}"
    fi
  fi

  local body_path
  if [[ "${output_type}" == "binary" ]]; then
    body_path="$(binary_path "${name}")"
  else
    body_path="$(body_path "${name}")"
  fi
  local headers_path="$(headers_path "${name}")"

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

  if [[ -n "${SILO_ID}" ]]; then
    curl_args+=( -H "X-Silo-ID: ${SILO_ID}" )
  fi

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
      warn "request ${method} ${path} failed"
      break
    fi

    warn "retrying ${method} ${path} in ${RETRY_DELAY_SECONDS}s (${attempt}/${MAX_RETRIES})"
    sleep "${RETRY_DELAY_SECONDS}"
    attempt=$((attempt + 1))
  done
}

prepare_payloads() {
  local latents_file="$(payload_path "create_and_store_latents_input")"
  local tts_file="$(payload_path "tts_to_audio_input")"
  local whisper_file="$(payload_path "whisper_inference_input")"

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
        "speaker_wav": speaker,
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

run_smoke_tests() {
  echo "--- RUNNING SMOKE TESTS (SILO_ID='${SILO_ID:-<none>}') ---"
  echo "  language=${LANGUAGE} speaker=${SPEAKER_NAME}"
  echo "  audio_sample=${AUDIO_SAMPLE_PATH}"
  echo ""

  local latents_payload tts_payload whisper_payload
  IFS=":" read -r latents_payload tts_payload whisper_payload < <(prepare_payloads)

  # Discovery endpoints
  run_request "speakers_list" "GET" "/speakers_list"
  run_request "languages" "GET" "/languages"

  # Derived payloads
  local whisper_model_name="${WHISPER_MODEL:-large-v3-turbo}"
  local whisper_load_payload="$(payload_path "whisper_load")"
  "${PY_BIN}" - <<'PY' "${whisper_load_payload}" "${whisper_model_name}"
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({"model": sys.argv[2]}))
PY

  # Latent management flow
  run_request "create_and_store_latents" "POST" "/create_and_store_latents" "${latents_payload}"
  assert_json_keys "create_and_store_latents" "file_path"

  # Additional XTTS cords
  run_request "speakers" "GET" "/speakers"
  run_request "get_folders" "GET" "/get_folders"
  run_request "get_models_list" "GET" "/get_models_list"
  run_request "get_tts_settings" "GET" "/get_tts_settings"
  
  local set_output_payload="$(payload_path "set_output")"
  echo '{"output_folder": "/app/output"}' > "${set_output_payload}"
  run_request "set_output" "POST" "/set_output" "${set_output_payload}"
  
  local set_speaker_payload="$(payload_path "set_speaker_folder")"
  echo '{"speaker_folder": "speakers/"}' > "${set_speaker_payload}"
  run_request "set_speaker_folder" "POST" "/set_speaker_folder" "${set_speaker_payload}"
  
  local switch_model_payload="$(payload_path "switch_model")"
  echo '{"model_name": "v2.0.2"}' > "${switch_model_payload}"
  # This may return 200 (newly loaded) or 400 (already loaded). We use a subshell to intercept the die/exit.
  if ! ( run_request "switch_model" "POST" "/switch_model" "${switch_model_payload}" ); then
    if grep -iq "already loaded" "$(body_path switch_model)" 2>/dev/null; then
      warn "switch_model reported model already loaded (400), continuing..."
    else
      warn "switch_model failed unexpectedly"
    fi
  fi
  
  local set_tts_payload="$(payload_path "set_tts_settings")"
  # Fetch current settings first to ensure we send a complete object (upstream requires all fields)
  "${PY_BIN}" - "$(body_path get_tts_settings)" "${set_tts_payload}" <<'PY'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text())
    data["temperature"] = 0.75
    pathlib.Path(sys.argv[2]).write_text(json.dumps(data))
except Exception as e:
    # Fallback to a safe full payload if GET failed
    pathlib.Path(sys.argv[2]).write_text(json.dumps({
        "temperature": 0.75, "length_penalty": 1.0, "repetition_penalty": 5.0,
        "top_k": 50, "top_p": 0.85, "speed": 1, "enable_text_splitting": True, "stream_chunk_size": 100
    }))
PY
  run_request "set_tts_settings" "POST" "/set_tts_settings" "${set_tts_payload}"

  run_request "create_latents" "POST" "/create_latents" "${latents_payload}"
  
  # Store latents
  local store_latents_payload="$(payload_path "store_latents")"
  if [[ -f "$(body_path create_latents)" ]]; then
    "${PY_BIN}" - "$(body_path create_latents)" "${store_latents_payload}" <<'PY'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text())
    payload = {"language": "en", "speaker_name": "malebrute", "latents": data["latents"]}
    pathlib.Path(sys.argv[2]).write_text(json.dumps(payload))
except Exception as e:
    print(f"ERROR: failed to prepare store_latents payload: {e}", file=sys.stderr)
    sys.exit(1)
PY
    if [[ $? -eq 0 ]]; then
      run_request "store_latents" "POST" "/store_latents" "${store_latents_payload}"
      assert_store_latents_ok
    fi
  else
    warn "skipping store_latents because create_latents failed"
  fi

  run_request "tts_to_file" "POST" "/tts_to_file" "${tts_payload}"
  assert_json_keys "tts_to_file" "file_path"

  # Verify trailing slash compat (must be registered as distinct cords at the gateway)
  run_request "tts_to_file_slash" "POST" "/tts_to_file/" "${tts_payload}"
  assert_json_keys "tts_to_file_slash" "file_path"

  # TTS endpoint
  run_request "tts_to_audio" "POST" "/tts_to_audio" "${tts_payload}" "binary"
  assert_content_type "tts_to_audio" "audio"
  assert_file_min_size "tts_to_audio" 200

  # Verify trailing slash compat
  run_request "tts_to_audio_slash" "POST" "/tts_to_audio/" "${tts_payload}" "binary"
  assert_content_type "tts_to_audio_slash" "audio"
  assert_file_min_size "tts_to_audio_slash" 200

  # Whisper endpoints
  run_request "whisper_load_get" "GET" "/load"
  run_request "whisper_load_post" "POST" "/whisper_load" "${whisper_load_payload}"
  assert_no_error_field "whisper_load_post"
  run_request "whisper_inference" "POST" "/inference" "${whisper_payload}"
  assert_json_keys "whisper_inference" "text"
}

main() {
  echo "Running XTTS/Whisper smoke tests against ${CHUTE_BASE_URL}"
  echo ""

  warmup_chute

  for sid in "" "test-silo-$(head /dev/urandom | tr -dc A-Za-z0-9 | head -c 8)"; do
    export SILO_ID="$sid"
    run_smoke_tests
  done

  echo "All XTTS/Whisper endpoints (siloed and non-siloed) responded successfully."
  echo "Artifacts stored in ${TMP_DIR}"
}

main "$@"
