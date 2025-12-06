#!/bin/bash
# Test script for xtts-whisper chute
# Usage: ./test_xtts_whisper.sh
#
# NOTE: Uses hostname-based invocation: https://{slug}.chutes.ai/{endpoint}
# The slug is: sirouk-dev-xtts-whisper
# Methods must match public_api_method (GET or POST depending on route)

set -e

API_KEY="${CHUTES_API_KEY:-cpk_2df4bcbadbc3463fa08abc07001ce952.38fdbc589ca35e34a7e94df5507cbebf.rPNOrv1tl03H7yvRr9ESe7Ce2Y6ouem9}"
BASE_URL="https://sirouk-dev-xtts-whisper.chutes.ai"
API_URL="https://api.chutes.ai"
CHUTE_NAME="xtts-whisper"

echo "=== Testing xtts-whisper chute ==="
echo "Base URL: $BASE_URL"
echo

# Warmup first
echo "0. Warming up chute (may take a few minutes for cold start)..."
timeout 120 curl -s "$API_URL/chutes/warmup/$CHUTE_NAME" \
  -H "Authorization: Bearer $API_KEY" 2>&1 | grep -o '"status":"[^"]*"' | tail -1 || echo "warmup timeout"
echo
echo

# Test 1: List speakers (GET)
echo "1. GET /speakers_list"
curl -s -X GET "$BASE_URL/speakers_list" \
  -H "Authorization: Bearer $API_KEY" \
  -w "\nHTTP %{http_code}\n" | head -20
echo

# Test 2: List languages (GET)  
echo "2. GET /languages"
curl -s -X GET "$BASE_URL/languages" \
  -H "Authorization: Bearer $API_KEY" \
  -w "\nHTTP %{http_code}\n" | head -20
echo

# Test 3: Get TTS settings (GET)
echo "3. GET /get_tts_settings"
curl -s -X GET "$BASE_URL/get_tts_settings" \
  -H "Authorization: Bearer $API_KEY" \
  -w "\nHTTP %{http_code}\n" | head -20
echo

# Test 4: TTS - Generate audio (POST, returns audio bytes)
echo "4. POST /tts_to_audio/ (text to speech)"
curl -s -X POST "$BASE_URL/tts_to_audio/" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello, this is a test of the XTTS text to speech system.",
    "speaker_wav": "default",
    "language": "en"
  }' \
  --output /tmp/xtts_test.wav \
  -w "HTTP %{http_code} - saved to /tmp/xtts_test.wav (%{size_download} bytes)\n"
echo

# Test 5: Whisper - Check model load status (GET)
echo "5. GET /load (whisper model status)"
curl -s -X GET "$BASE_URL/load" \
  -H "Authorization: Bearer $API_KEY" \
  -w "\nHTTP %{http_code}\n" | head -20
echo

# Test 6: Whisper - Transcribe audio (POST with multipart form)
if [[ -f /tmp/xtts_test.wav ]] && [[ $(stat -c%s /tmp/xtts_test.wav 2>/dev/null || stat -f%z /tmp/xtts_test.wav) -gt 100 ]]; then
  echo "6. POST /inference (whisper transcription)"
  curl -s -X POST "$BASE_URL/inference" \
    -H "Authorization: Bearer $API_KEY" \
    -F "file=@/tmp/xtts_test.wav" \
    -w "\nHTTP %{http_code}\n" | head -20
else
  echo "6. Skipped - no valid audio file from TTS"
fi

echo
echo "=== Tests complete ==="
