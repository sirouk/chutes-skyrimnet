import asyncio
import base64
import contextlib
import io
import os
import tempfile
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
    script: Optional[str] = Field(default=None)
    language: Optional[str] = Field(default=None)
    voice_sample_b64: Optional[str] = Field(default=None)
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
os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1")

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
]

USERNAME = os.getenv("CHUTES_USERNAME", "your_chutes_username")
ZONOS_MODEL_ID = os.getenv("ZONOS_MODEL_ID", "Zyphra/Zonos-v0.1-hybrid")
WHISPER_MODEL_ID = os.getenv("ZONOS_WHISPER_MODEL", os.getenv("WHISPER_MODEL", "large-v3-turbo"))
DEFAULT_VOICE_PATH = "/app/assets/zonos_default.wav"
LOCAL_VOICE_SAMPLE = "assets/default_voice.wav"


image = (
    Image(
        username=USERNAME,
        name="zonos-whisper",
        tag="0.0.1",
        readme="Zonos v0.1 hybrid TTS with Whisper transcription.",
    )
    .from_base("parachutes/base-python:3.12.9")
    .set_user("root")
    .run_command(
        "apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng libespeak-ng1 "
        "&& rm -rf /var/lib/apt/lists/*"
    )
    .set_user("chutes")
    .add(LOCAL_VOICE_SAMPLE, DEFAULT_VOICE_PATH)
    .run_command(
        "pip install --no-cache-dir "
        "zonos==0.1.0.dev0 "
        "torch==2.5.1 "
        "torchaudio==2.5.1 "
        "soundfile==0.12.1 "
        "librosa==0.10.2.post1 "
        "faster-whisper==1.0.3"
    )
)

chute = Chute(
    username=USERNAME,
    name="zonos-whisper",
    tagline="Zonos expressive multilingual speech for SkyrimNet.",
    readme="""
### Zonos v0.1 Hybrid Chute

- **Model**: `Zyphra/Zonos-v0.1-hybrid`
- **Voice cloning**: reference audio via `voice_sample_b64`
- **Controls**: `language`, `cfg_scale`, `max_new_tokens`

Endpoints:
1. `POST /speak`
2. `POST /transcribe`
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=24, include=["h100", "h100_sxm", "h200"]),
    concurrency=1,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


class ZonosEngine:
    def __init__(
        self,
        model_name: str,
        default_voice_path: str,
        cfg_scale: float = 2.0,
        max_new_tokens: int = 86 * 25,
    ):
        import torch  # type: ignore[import-not-found]
        import torchaudio  # type: ignore[import-not-found]
        from zonos.model import Zonos  # type: ignore[import-not-found]
        from zonos.conditioning import make_cond_dict  # type: ignore[import-not-found]

        self.torch = torch
        self.torchaudio = torchaudio
        self.make_cond_dict = make_cond_dict

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = Zonos.from_pretrained(model_name, device=device)
        self.device = device
        self.sample_rate = int(self.model.autoencoder.sampling_rate)
        self.cfg_scale = cfg_scale
        self.max_new_tokens = max_new_tokens
        self.default_voice_path = default_voice_path
        self._lock = asyncio.Lock()

    def _load_voice(self, path: str) -> Tuple[Any, int]:
        wav, sr = self.torchaudio.load(path)
        if sr != self.sample_rate:
            wav = self.torchaudio.functional.resample(wav, sr, self.sample_rate)
            sr = self.sample_rate
        return wav, sr

    def _generate(
        self,
        text: str,
        speaker_audio: Any,
        speaker_sr: int,
        language: str,
        cfg_scale: float,
        max_new_tokens: int,
    ):
        if speaker_sr != self.sample_rate:
            speaker_audio = self.torchaudio.functional.resample(speaker_audio, speaker_sr, self.sample_rate)
        speaker = self.model.make_speaker_embedding(speaker_audio, self.sample_rate)
        cond = self.make_cond_dict(
            text=text,
            speaker=speaker,
            language=language,
        )
        conditioning = self.model.prepare_conditioning(cond)
        codes = self.model.generate(
            conditioning,
            cfg_scale=cfg_scale,
            max_new_tokens=max_new_tokens,
            progress_bar=False,
        )
        wavs = self.model.autoencoder.decode(codes).cpu()
        audio = wavs[0].numpy()
        if audio.ndim > 1:
            audio = audio[0]
        return audio.astype(np.float32), self.sample_rate

    async def synthesize(self, request: SpeakRequest) -> Tuple[np.ndarray, int]:
        text = (request.script or request.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Text or script content is required.")
        language = request.language or "en-us"
        cfg_scale = request.cfg_scale or self.cfg_scale
        max_new_tokens = request.max_new_tokens or self.max_new_tokens

        loop = asyncio.get_running_loop()
        async with self._lock:
            with temporary_audio_file(request.voice_sample_b64) as custom_voice:
                voice_path = custom_voice or self.default_voice_path
                speaker_audio, sr = self._load_voice(voice_path)
                try:
                    return await loop.run_in_executor(
                        None,
                        self._generate,
                        text,
                        speaker_audio,
                        sr,
                        language,
                        cfg_scale,
                        max_new_tokens,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=500, detail=f"Zonos synthesis failed: {exc}") from exc


@chute.on_startup()
async def initialize(self):
    self.zonos = ZonosEngine(ZONOS_MODEL_ID, DEFAULT_VOICE_PATH)
    self.transcriber = WhisperTranscriber(WHISPER_MODEL_ID)


@chute.cord(public_api_path="/speak", public_api_method="POST", stream=False)
async def speak(self, payload: SpeakRequest):
    audio, sr = await self.zonos.synthesize(payload)
    meta = {
        "engine": "zonos",
        "model": ZONOS_MODEL_ID,
        "language": payload.language or "en-us",
        "cfg_scale": payload.cfg_scale or self.zonos.cfg_scale,
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

