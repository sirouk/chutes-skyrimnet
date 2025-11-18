import asyncio
import base64
import contextlib
import io
import os
import tempfile
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf
from fastapi import HTTPException
from pydantic import BaseModel, Field

from chutes.chute import Chute, NodeSelector
from chutes.image import Image

if TYPE_CHECKING:
    from boson_multimodal.data_types import ChatMLSample  # type: ignore[import-not-found]


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
    script: Optional[str] = Field(default=None, description="Optional extra context.")
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
HIGGS_MODEL_ID = os.getenv("HIGGS_MODEL_ID", "bosonai/higgs-audio-v2-generation-3B-base")
HIGGS_TOKENIZER_ID = os.getenv("HIGGS_AUDIO_TOKENIZER", "bosonai/higgs-audio-v2-tokenizer")
WHISPER_MODEL_ID = os.getenv("HIGGS_WHISPER_MODEL", os.getenv("WHISPER_MODEL", "large-v3-turbo"))
HIGGS_COMMIT = os.getenv("HIGGS_AUDIO_COMMIT", "f644b62b855ba2b938896436221e01efadcc76ca")


image = (
    Image(
        username=USERNAME,
        name="higgs-whisper",
        tag="0.0.1",
        readme="Boson Higgs Audio v2 with Whisper transcription.",
    )
    .from_base("parachutes/base-python:3.12.9")
    .set_user("root")
    .run_command("apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*")
    .set_user("chutes")
    .run_command(
        "pip install --no-cache-dir "
        f"git+https://github.com/boson-ai/higgs-audio.git@{HIGGS_COMMIT} "
        "torch==2.5.1 "
        "torchaudio==2.5.1 "
        "soundfile==0.12.1 "
        "librosa==0.10.2.post1 "
        "faster-whisper==1.0.3"
    )
)

chute = Chute(
    username=USERNAME,
    name="higgs-whisper",
    tagline="Higgs Audio v2 speech generation for SkyrimNet.",
    readme="""
### Higgs Audio v2 Chute

- **Model**: `bosonai/higgs-audio-v2-generation-3B-base`
- **Tokenizer**: `bosonai/higgs-audio-v2-tokenizer`
- **Strengths**: emotive storytelling, expressive multilingual delivery

Endpoints:
1. `POST /speak`
2. `POST /transcribe`
""",
    image=image,
    node_selector=NodeSelector(gpu_count=1, min_vram_gb_per_gpu=32, include=["h100_sxm", "h100_nvl", "mi300x"]),
    concurrency=1,
    allow_external_egress=True,
    shutdown_after_seconds=3600,
)


class HiggsAudioEngine:
    def __init__(
        self,
        model_id: str,
        tokenizer_id: str,
        max_new_tokens: int = 2048,
        temperature: float = 0.2,
        top_p: float = 0.9,
    ):
        import torch  # type: ignore[import-not-found]
        from boson_multimodal.serve.serve_engine import HiggsAudioServeEngine  # type: ignore[import-not-found]

        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = "bfloat16" if device == "cuda" else "float32"
        self.engine = HiggsAudioServeEngine(
            model_name_or_path=model_id,
            audio_tokenizer_name_or_path=tokenizer_id,
            device=device,
            torch_dtype=dtype,
        )
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.top_p = top_p
        self._lock = asyncio.Lock()

    def _build_sample(self, request: SpeakRequest):
        from boson_multimodal.data_types import ChatMLSample, Message  # type: ignore[import-not-found]

        user_text = (request.script or request.text or "").strip()
        if not user_text:
            raise HTTPException(status_code=400, detail="Text or script content is required.")
        system_prompt = request.language or "You narrate vivid scenes with cinematic pacing."
        return ChatMLSample(
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_text),
            ]
        )

    def _generate(self, sample: "ChatMLSample", temperature: float, top_p: float) -> Tuple[np.ndarray, int]:
        response = self.engine.generate(
            chat_ml_sample=sample,
            max_new_tokens=self.max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            force_audio_gen=True,
            ras_win_len=7,
        )
        if response.audio is None:
            raise RuntimeError("Higgs audio generation returned empty output.")
        audio = response.audio.astype(np.float32)
        return audio, int(response.sampling_rate or 24000)

    async def synthesize(self, request: SpeakRequest) -> Tuple[np.ndarray, int]:
        sample = self._build_sample(request)
        temperature = request.temperature if request.temperature is not None else self.temperature
        top_p = request.top_p if request.top_p is not None else self.top_p
        loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                return await loop.run_in_executor(None, self._generate, sample, temperature, top_p)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Higgs synthesis failed: {exc}") from exc


@chute.on_startup()
async def initialize(self):
    self.higgs = HiggsAudioEngine(HIGGS_MODEL_ID, HIGGS_TOKENIZER_ID)
    self.transcriber = WhisperTranscriber(WHISPER_MODEL_ID)


@chute.cord(public_api_path="/speak", public_api_method="POST", stream=False)
async def speak(self, payload: SpeakRequest):
    audio, sr = await self.higgs.synthesize(payload)
    meta = {
        "engine": "higgs-audio-v2",
        "model": HIGGS_MODEL_ID,
        "tokenizer": HIGGS_TOKENIZER_ID,
        "temperature": payload.temperature if payload.temperature is not None else self.higgs.temperature,
        "top_p": payload.top_p if payload.top_p is not None else self.higgs.top_p,
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

