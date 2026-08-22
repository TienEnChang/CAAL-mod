"""OpenAI-compatible Whisper and Kokoro server for CAAL on Apple Silicon.

This intentionally reuses the Python environment and Hugging Face cache from
the sibling protoVoice installation.  It exposes only the endpoints CAAL uses:
``/v1/audio/transcriptions``, ``/v1/audio/speech``, and ``/v1/models``.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
from typing import Annotated

import numpy as np
import soundfile as sf
import soxr
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from transformers import pipeline as hf_pipeline

WHISPER_MODEL = os.getenv("CAAL_WHISPER_MODEL", "distil-whisper/distil-medium.en")
KOKORO_MODEL = os.getenv("CAAL_KOKORO_MODEL", "hexgrad/Kokoro-82M")
KOKORO_LANG = os.getenv("CAAL_KOKORO_LANG", "a")
DEFAULT_VOICE = os.getenv("CAAL_KOKORO_VOICE", "af_heart")
SAMPLE_RATE = 24_000

app = FastAPI(title="CAAL local MLX speech bridge", version="1.0")

_whisper = None
_kokoro = None
_whisper_lock = threading.Lock()
_kokoro_lock = threading.Lock()


def _device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _get_whisper():
    global _whisper
    if _whisper is None:
        with _whisper_lock:
            if _whisper is None:
                device = _device()
                use_fp16 = device == "mps" and os.getenv("CAAL_STT_FP16", "1") == "1"
                _whisper = hf_pipeline(
                    "automatic-speech-recognition",
                    model=WHISPER_MODEL,
                    torch_dtype=torch.float16 if use_fp16 else torch.float32,
                    device=device,
                    model_kwargs={"attn_implementation": "sdpa"} if device == "mps" else {},
                )
    return _whisper


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        with _kokoro_lock:
            if _kokoro is None:
                from kokoro import KPipeline

                _kokoro = KPipeline(lang_code=KOKORO_LANG, repo_id=KOKORO_MODEL)
    return _kokoro


def _transcribe(audio_bytes: bytes) -> str:
    data, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sample_rate != 16_000:
        data = soxr.resample(data, sample_rate, 16_000)
    result = _get_whisper()({"raw": data.flatten(), "sampling_rate": 16_000})
    return (result.get("text") or "").strip()


def _synthesize(text: str, voice: str, speed: float) -> bytes:
    chunks: list[np.ndarray] = []
    pipe = _get_kokoro()
    for item in pipe(text, voice=voice or DEFAULT_VOICE, speed=speed):
        audio = item[2] if hasattr(item, "__len__") and len(item) >= 3 else item
        if audio is not None:
            chunks.append(np.asarray(audio, dtype=np.float32))
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    output = io.BytesIO()
    sf.write(output, np.concatenate(chunks), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return output.getvalue()


class SpeechRequest(BaseModel):
    input: str
    model: str = "kokoro"
    voice: str = DEFAULT_VOICE
    speed: float = 1.0
    response_format: str = "wav"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": WHISPER_MODEL, "object": "model"},
            {"id": KOKORO_MODEL, "object": "model"},
        ],
    }


async def _load_requested_model(model_name: str) -> dict[str, str]:
    normalized = model_name.lower()
    if "whisper" in normalized:
        await asyncio.to_thread(_get_whisper)
    elif "kokoro" in normalized:
        await asyncio.to_thread(_get_kokoro)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown local model: {model_name}")
    return {"status": "loaded", "model": model_name}


@app.post("/v1/models")
async def load_model(model_name: str):
    return await _load_requested_model(model_name)


@app.post("/v1/models/{model_name:path}")
async def load_model_path(model_name: str):
    return await _load_requested_model(model_name)


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()] = WHISPER_MODEL,
    response_format: Annotated[str, Form()] = "json",
    language: Annotated[str | None, Form()] = None,
):
    del model, language
    try:
        text = await asyncio.to_thread(_transcribe, await file.read())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Whisper failed: {exc}") from exc
    if response_format == "text":
        return PlainTextResponse(text)
    return JSONResponse({"text": text})


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest):
    if request.response_format not in {"wav", "wave"}:
        raise HTTPException(status_code=400, detail="This local bridge supports WAV output")
    if not request.input.strip():
        raise HTTPException(status_code=400, detail="input must not be empty")
    try:
        audio = await asyncio.to_thread(
            _synthesize,
            request.input,
            request.voice,
            request.speed,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Kokoro failed: {exc}") from exc
    return Response(audio, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
