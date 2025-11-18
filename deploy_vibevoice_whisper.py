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
    script: Optional[str] = Field(default=None, description="Optional multi-speaker script.")
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
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

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
VIBEVOICE_MODEL_ID = os.getenv("VIBEVOICE_MODEL_ID", "microsoft/VibeVoice-1.5B")
WHISPER_MODEL_ID = os.getenv("VIBEVOICE_WHISPER_MODEL", os.getenv("WHISPER_MODEL", "large-v3-turbo"))
DEFAULT_VOICE_PATH = "/app/assets/vibevoice_default.wav"
LOCAL_VOICE_SAMPLE = "assets/default_voice.wav"


image = (
    Image(
        username=USERNAME,
        name="vibevoice-whisper",
        tag="0.0.1",
        readme="VibeVoice long-form TTS with Whisper transcription.",
    )
    .from_base("parachutes/base-python:3.12.9")
    .set_user("root")
    .run_command("apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*")
    .set_user("chutes")
    .add(LOCAL_VOICE_SAMPLE, DEFAULT_VOICE_PATH)
    .run_command(
        "pip install --no-cache-dir "
        "vibevoice==0.0.1 "
        "torch==2.5.1 "
        "torchaudio==2.5.1 "
        "soundfile==0.12.1 "
        "librosa==0.10.2.post1 "
        "faster-whisper==1.0.3"
    )
)

chute = Chute(
    username=USERNAME,
    name="vibevoice-whisper",
    tagline="VibeVoice conversational speech generation for SkyrimNet.",
    readme="""
### VibeVoice + Whisper Private Chute

- **Model**: `microsoft/VibeVoice-1.5B`
- **Use cases**: long-form podcasts, story narration, in-game banter
- **Features**:
  - Optional dialogue scripts (multi-speaker) via `script` field
  - Voice cloning using base64 reference audio
  - Whisper transcription / translation endpoint

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


class VibeVoiceEngine:
    def __init__(
        self,
        model_name: str,
        default_voice_path: str,
        sample_rate: int = 24000,
        cfg_scale: float = 1.3,
        ddpm_steps: int = 6,
    ):
        import torch  # type: ignore[import-not-found]
        from vibevoice.modular.modeling_vibevoice_inference import (  # type: ignore[import-not-found]
            VibeVoiceForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor  # type: ignore[import-not-found]

        self._torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.bfloat16 if self.device.type == "cuda" else torch.float32

        self.processor = VibeVoiceProcessor.from_pretrained(model_name)
        self.model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            model_name,
            torch_dtype=dtype,
            device_map="cuda" if self.device.type == "cuda" else {"": self.device},
            attn_implementation="sdpa",
        )
        if self.device.type != "cuda":
            self.model.to(self.device)
        self.model.eval()
        self.model.model.noise_scheduler = self.model.model.noise_scheduler.from_config(
            self.model.model.noise_scheduler.config,
            algorithm_type="sde-dpmsolver++",
            beta_schedule="squaredcos_cap_v2",
        )
        self.model.set_ddpm_inference_steps(num_steps=ddpm_steps)

        self.sample_rate = sample_rate
        self.cfg_scale_default = cfg_scale
        self.default_voice_path = default_voice_path
        self._lock = asyncio.Lock()

    def _read_voice(self, path: str) -> np.ndarray:
        import librosa  # type: ignore[import-not-found]

        audio, _ = librosa.load(path, sr=self.sample_rate)
        max_samples = self.sample_rate * 45  # cap at ~45 seconds
        if len(audio) > max_samples:
            audio = audio[:max_samples]
        return audio.astype(np.float32)

    def _format_script(self, request: SpeakRequest) -> str:
        if request.script:
            return request.script.strip()
        text = (request.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Text or script content is required.")
        speakers = request.num_speakers or 1
        if speakers <= 1:
            return f"Speaker 0: {text}"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            lines = [text]
        formatted: List[str] = []
        for idx, line in enumerate(lines):
            formatted.append(f"Speaker {idx % speakers}: {line}")
        return "\n".join(formatted)

    def _generate(self, script: str, voice_samples: List[np.ndarray], cfg_scale: float, max_new_tokens: Optional[int]):
        inputs = self.processor(
            text=[script],
            voice_samples=[voice_samples],
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        for key, value in inputs.items():
            if hasattr(value, "to"):
                inputs[key] = value.to(self.device)

        outputs = self.model.generate(
            **inputs,
            tokenizer=self.processor.tokenizer,
            cfg_scale=cfg_scale,
            max_new_tokens=max_new_tokens,
            show_progress_bar=False,
            verbose=False,
            refresh_negative=True,
        )
        speech_outputs = outputs.speech_outputs or []
        if not speech_outputs or speech_outputs[0] is None:
            raise RuntimeError("VibeVoice did not return any speech output.")
        audio = speech_outputs[0].detach().cpu().numpy().astype(np.float32)
        if audio.ndim > 1:
            audio = audio.squeeze()
        return audio, self.sample_rate

    async def synthesize(self, request: SpeakRequest) -> Tuple[np.ndarray, int]:
        script = self._format_script(request)
        cfg_scale = request.cfg_scale or self.cfg_scale_default
        max_new_tokens = request.max_new_tokens

        voice_arrays: List[np.ndarray] = []
        with temporary_audio_file(request.voice_sample_b64) as ref_audio:
            if ref_audio:
                voice_arrays.append(self._read_voice(ref_audio))
            else:
                voice_arrays.append(self._read_voice(self.default_voice_path))

        loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                return await loop.run_in_executor(
                    None,
                    self._generate,
                    script,
                    voice_arrays,
                    cfg_scale,
                    max_new_tokens,
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"VibeVoice synthesis failed: {exc}") from exc


@chute.on_startup()
async def initialize(self):
    self.vibevoice = VibeVoiceEngine(VIBEVOICE_MODEL_ID, DEFAULT_VOICE_PATH)
    self.transcriber = WhisperTranscriber(WHISPER_MODEL_ID)


@chute.cord(public_api_path="/speak", public_api_method="POST", stream=False)
async def speak(self, payload: SpeakRequest):
    audio, sr = await self.vibevoice.synthesize(payload)
    meta = {
        "engine": "vibevoice",
        "model": VIBEVOICE_MODEL_ID,
        "num_speakers": payload.num_speakers or 1,
        "cfg_scale": payload.cfg_scale or self.vibevoice.cfg_scale_default,
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

