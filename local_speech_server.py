"""OpenAI-compatible MLX Whisper and Kokoro server for Apple Silicon.

The bridge exposes only the endpoints CAAL uses: ``/v1/audio/transcriptions``,
``/v1/audio/speech``, and ``/v1/models``. Both neural models run through MLX;
audio decoding and resampling remain ordinary CPU-side preprocessing.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
from typing import Annotated

import mlx.core as mx
import mlx_whisper
import numpy as np
import soundfile as sf
import soxr
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from mlx_audio.tts.utils import load as load_tts_model
from mlx_whisper.transcribe import ModelHolder
from pydantic import BaseModel

WHISPER_MODEL = os.getenv(
    "CAAL_WHISPER_MODEL", "mlx-community/distil-whisper-medium.en"
)
KOKORO_MODEL = os.getenv("CAAL_KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")
KOKORO_LANG = os.getenv("CAAL_KOKORO_LANG", "a")
DEFAULT_VOICE = os.getenv("CAAL_KOKORO_VOICE", "af_heart")
SAMPLE_RATE = 24_000

app = FastAPI(title="CAAL local MLX speech bridge", version="1.0")

_whisper = None
_kokoro = None
_whisper_lock = threading.Lock()
_kokoro_lock = threading.Lock()


def _get_whisper():
    global _whisper
    if _whisper is None:
        with _whisper_lock:
            if _whisper is None:
                _whisper = ModelHolder.get_model(WHISPER_MODEL, dtype=mx.float16)
    return _whisper


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        with _kokoro_lock:
            if _kokoro is None:
                _kokoro = load_tts_model(KOKORO_MODEL)
    return _kokoro


def _transcribe(audio_bytes: bytes, language: str | None = None) -> str:
    data, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sample_rate != 16_000:
        data = soxr.resample(data, sample_rate, 16_000)
    _get_whisper()
    result = mlx_whisper.transcribe(
        data.flatten(),
        path_or_hf_repo=WHISPER_MODEL,
        verbose=None,
        language=language,
        fp16=True,
    )
    return (result.get("text") or "").strip()


def _synthesize(text: str, voice: str, speed: float) -> bytes:
    chunks: list[np.ndarray] = []
    pipe = _get_kokoro()
    for item in pipe.generate(
        text,
        voice=voice or DEFAULT_VOICE,
        speed=speed,
        lang_code=KOKORO_LANG,
    ):
        audio = item.audio
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
    del model
    try:
        text = await asyncio.to_thread(_transcribe, await file.read(), language)
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

    uvicorn.run(
        app,
        host=os.getenv("SPEECH_HOST", "127.0.0.1"),
        port=int(os.getenv("SPEECH_PORT", "8001")),
        log_level="info",
    )
