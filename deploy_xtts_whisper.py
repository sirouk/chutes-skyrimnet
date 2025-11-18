import asyncio
import base64
import contextlib
import io
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf
from fastapi import HTTPException
from pydantic import BaseModel, Field

from chutes.chute import Chute, NodeSelector
from chutes.image import Image


def decode_audio_from_base64(audio_b64: str, target_sr: Optional[int] = None) -> Tuple[np.ndarray, int]:
    audio_bytes = base64.b64decode(audio_b64)
    audio, sr = sf.read(io.BytesIO(audio_bytes))
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    if target_sr and sr != target_sr:
        import librosa  # type: ignore[import-not-found]

        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr).astype(np.float32)
        sr = target_sr
    return audio, sr


def encode_audio_to_base64(audio: np.ndarray, sample_rate: int) -> str:
    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="wav")
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")


@contextlib.contextmanager
def temporary_audio_file(audio_b64: Optional[str], suffix: str = ".wav"):
    if not audio_b64:
        yield None
        return
    path = None
    try:
        audio_bytes = base64.b64decode(audio_b64)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            path = tmp.name
        yield path
    finally:
        if path and os.path.exists(path):
            os.remove(path)


class SpeakRequest(BaseModel):
    text: Optional[str] = Field(default=None, description="Primary text input for the TTS engine.")
    script: Optional[str] = Field(default=None, description="Optional multi-speaker script input.")
    language: Optional[str] = Field(default=None, description="Language hint for TTS/STT components.")
    voice_sample_b64: Optional[str] = Field(default=None, description="Base64 encoded reference audio.")
    temperature: Optional[float] = Field(default=None)
    top_p: Optional[float] = Field(default=None)
    max_new_tokens: Optional[int] = Field(default=None)
    cfg_scale: Optional[float] = Field(default=None)
    seed: Optional[int] = Field(default=None)
    num_speakers: Optional[int] = Field(default=1, ge=1, le=4)


class TranscribeRequest(BaseModel):
    audio_b64: str = Field(..., description="Base64 encoded WAV/MP3 payload to transcribe.")
    translate_to_english: bool = Field(default=False)
    language: Optional[str] = Field(default=None)


class WhisperTranscriber:
    def __init__(self, model_name: str, device: Optional[str] = None, compute_type: Optional[str] = None):
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]

        if device is None:
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        if compute_type is None:
            if device == "cuda":
                compute_type = "float16"
            elif device == "mps":
                compute_type = "float32"
            else:
                compute_type = "int8"
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self._device = device

    async def transcribe(self, request: TranscribeRequest) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()

        def _run(audio_path: str) -> Dict[str, Any]:
            segments, info = self._model.transcribe(
                audio_path,
                task="translate" if request.translate_to_english else "transcribe",
                language=request.language,
                vad_filter=True,
                beam_size=5,
            )
            segment_payload: List[Dict[str, Any]] = []
            for seg in segments:
                segment_payload.append(
                    {
                        "id": seg.id,
                        "text": seg.text.strip(),
                        "start": float(seg.start),
                        "end": float(seg.end),
                        "temperature": seg.temperature,
                        "avg_log_prob": seg.avg_log_prob,
                        "compression_ratio": seg.compression_ratio,
                        "no_speech_prob": seg.no_speech_prob,
                    }
                )
            transcript = " ".join(seg["text"] for seg in segment_payload).strip()
            return {
                "text": transcript,
                "segments": segment_payload,
                "detected_language": info.language,
                "duration": info.duration,
            }

        audio, sr = decode_audio_from_base64(request.audio_b64)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, audio, sr, format="wav")
            temp_path = tmp.name
        try:
            return await loop.run_in_executor(None, _run, temp_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


def audio_duration_seconds(audio: np.ndarray, sample_rate: int) -> float:
    return float(len(audio) / sample_rate) if sample_rate > 0 else 0.0


def response_with_audio(audio: np.ndarray, sample_rate: int, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "audio_b64": encode_audio_to_base64(audio, sample_rate),
        "sample_rate": sample_rate,
        "duration_seconds": audio_duration_seconds(audio, sample_rate),
    }
    if meta:
        payload["meta"] = meta
    return payload


os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")

GPU_PREFERENCE = [
    "RTX 4090",
    "RTX 4080",
    "RTX 4080S",
    "RTX 4070 Ti",
    "RTX 4070S",
    "RTX 4070S Ti",
    "RTX 4070",
    "RTX 4060 Ti",
    "RTX 4060",
    "RTX 3090 Ti",
    "RTX 3090",
    "RTX 3080 Ti",
    "RTX 3080",
    "RTX 3070 Ti",
    "RTX 3070",
    "RTX 3060 Ti",
    "RTX 3060",
]

USERNAME = os.getenv("CHUTES_USERNAME", "your_chutes_username")
DEFAULT_VOICE_PATH = "/app/assets/xtts_default.wav"
LOCAL_VOICE_SAMPLE = "assets/default_voice.wav"
XTTS_MODEL_ID = os.getenv("XTTS_MODEL_ID", "tts_models/multilingual/multi-dataset/xtts_v2")
WHISPER_MODEL_ID = os.getenv("XTTS_WHISPER_MODEL", os.getenv("WHISPER_MODEL", "large-v3-turbo"))


image = (
    Image(
        username=USERNAME,
        name="xtts-whisper",
        tag="0.0.1",
        readme="Private XTTS + Whisper chute for SkyrimNet voice services.",
    )
    .from_base("parachutes/base-python:3.11.9")
    .set_user("root")
    .run_command("apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng && rm -rf /var/lib/apt/lists/*")
    .set_user("chutes")
    .add(LOCAL_VOICE_SAMPLE, DEFAULT_VOICE_PATH)
    .run_command(
        "pip install --no-cache-dir "
        "TTS==0.22.0 "
        "faster-whisper==1.0.3 "
        "soundfile==0.12.1 "
        "librosa==0.10.2.post1 "
        "torch==2.5.1 "
        "torchaudio==2.5.1"
    )
)

chute = Chute(
    username=USERNAME,
    name="xtts-whisper",
    tagline="XTTS voice cloning with Whisper transcription fallback.",
    readme="""
### XTTS + Whisper Private Chute

- **Model**: `coqui/XTTS-v2`
- **Voice cloning**: optional base64 audio sample per request
- **Transcription**: `faster-whisper` (default `large-v3-turbo`)
- **Best for**: single speaker conversational TTS workloads

Endpoints:
1. `POST /speak` &mdash; synthesize speech (returns base64 WAV)
2. `POST /transcribe` &mdash; transcribe / translate arbitrary audio
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=16, include=["h100", "h200", "b200"]),
    concurrency=2,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


class XTTSGenerator:
    def __init__(self, model_name: str, default_voice_path: str, default_language: str = "en"):
        from TTS.api import TTS as CoquiTTS  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]

        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._tts = CoquiTTS(model_name=model_name, progress_bar=False, gpu=self._device == "cuda")
        self._default_voice = default_voice_path
        self._default_language = default_language
        self._lock = asyncio.Lock()

    def _synthesize(self, text: str, language: str, speaker_path: str) -> Tuple[np.ndarray, int]:
        tmp_path = f"/tmp/xtts_{uuid.uuid4().hex}.wav"
        try:
            self._tts.tts_to_file(
                text=text,
                speaker_wav=speaker_path,
                language=language,
                file_path=tmp_path,
            )
            audio, sr = sf.read(tmp_path)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            return audio.astype(np.float32), sr
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def synthesize(self, request: SpeakRequest) -> Tuple[np.ndarray, int]:
        text = (request.script or request.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Text or script content is required.")
        language = request.language or self._default_language

        loop = asyncio.get_running_loop()
        async with self._lock:
            with temporary_audio_file(request.voice_sample_b64) as custom_voice:
                speaker_path = custom_voice or self._default_voice
                try:
                    return await loop.run_in_executor(
                        None,
                        self._synthesize,
                        text,
                        language,
                        speaker_path,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=500, detail=f"XTTS synthesis failed: {exc}") from exc


@chute.on_startup()
async def initialize(self):
    self.xtts = XTTSGenerator(XTTS_MODEL_ID, DEFAULT_VOICE_PATH)
    self.transcriber = WhisperTranscriber(WHISPER_MODEL_ID)


@chute.cord(public_api_path="/speak", public_api_method="POST", stream=False)
async def speak(self, payload: SpeakRequest):
    audio, sr = await self.xtts.synthesize(payload)
    meta = {
        "engine": "xtts",
        "language": payload.language or "auto",
        "model": XTTS_MODEL_ID,
    }
    return response_with_audio(audio, sr, meta=meta)


@chute.cord(public_api_path="/transcribe", public_api_method="POST", stream=False)
async def transcribe(self, payload: TranscribeRequest):
    try:
        return await self.transcriber.transcribe(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc

