"""OpenAI-compatible MLX Whisper and Kokoro server for Apple Silicon.

The bridge exposes only the endpoints CAAL uses: ``/v1/audio/transcriptions``,
``/v1/audio/speech``, ``/v1/audio/voices``, and ``/v1/models``. Both neural
models run through MLX; audio decoding and resampling remain ordinary CPU-side
preprocessing.
"""

from __future__ import annotations

import asyncio
import io
import os
import pathlib
import re
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
    "CAAL_WHISPER_MODEL", "mlx-community/distil-whisper-large-v3"
)
KOKORO_MODEL = os.getenv("CAAL_KOKORO_MODEL", "mlx-community/Kokoro-82M-bf16")
KOKORO_LANG = os.getenv("CAAL_KOKORO_LANG", "a")
DEFAULT_VOICE = os.getenv("CAAL_KOKORO_VOICE", "af_heart")
SAMPLE_RATE = 24_000

# Kokoro encodes the language in the first letter of the voice name and selects
# its grapheme-to-phoneme backend from lang_code. Pinning lang_code to one value
# would phonemize every language as that one, so it is derived per request and
# CAAL_KOKORO_LANG only supplies the fallback for unrecognised voices.
KOKORO_LANGUAGE_CODES = frozenset("abefhijpz")


def _lang_code_for_voice(voice: str) -> str:
    """Derive Kokoro's lang_code from a voice name (``ff_siwis`` -> ``f``)."""
    if voice and voice[0] in KOKORO_LANGUAGE_CODES:
        return voice[0]
    return KOKORO_LANG

app = FastAPI(title="CAAL local MLX speech bridge", version="1.0")

_whisper = None
_kokoro = None
_whisper_lock = threading.Lock()
_kokoro_lock = threading.Lock()
# STT, TTS and allocator cleanup all share MLX's process-global allocator.
# Serializing them prevents a call-end cleanup from racing work that is still
# materializing audio on a worker thread.
_mlx_inference_lock = threading.Lock()


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


# A Kokoro voice phonemizes with one language's G2P. Handed Han characters,
# the English backend emits the literal words "Chinese letter" per character,
# so a reply that mixes scripts - an English sentence quoting a Chinese
# calendar entry - is unspeakable by any single voice. Such text is split into
# runs and each run is voiced by a matching voice, then concatenated.
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Companion Han voice, matched to the requested voice's gender so the reply
# does not change speaker mid-sentence more than it has to.
CJK_VOICE_FEMALE = os.getenv("CAAL_KOKORO_CJK_VOICE_FEMALE", "zf_xiaoxiao")
CJK_VOICE_MALE = os.getenv("CAAL_KOKORO_CJK_VOICE_MALE", "zm_yunjian")

# Neither script's G2P should be handed the other's punctuation.
_NEUTRAL = set(" \t\n\r0123456789.,!?;:'\"()[]-—–…、，。！？；：（）「」《》")


def _han_voice_for(voice: str) -> str:
    """Pick the Han voice matching the requested voice's gender."""
    gender = voice[1:2].lower()
    return CJK_VOICE_MALE if gender == "m" else CJK_VOICE_FEMALE


def _split_by_script(text: str) -> list[tuple[str, bool]]:
    """Split text into (run, is_han) segments, keeping neutral characters with
    the run in progress so punctuation never becomes its own utterance."""
    runs: list[tuple[str, bool]] = []
    current: list[str] = []
    current_han: bool | None = None

    for char in text:
        if char in _NEUTRAL:
            current.append(char)
            continue
        is_han = bool(_HAN_RE.match(char))
        if current_han is None:
            current_han = is_han
        elif is_han != current_han:
            runs.append(("".join(current), current_han))
            current = []
            current_han = is_han
        current.append(char)

    if current and current_han is not None:
        runs.append(("".join(current), current_han))
    return runs


def _generate(pipe, text: str, voice: str, speed: float) -> list[np.ndarray]:
    chunks: list[np.ndarray] = []
    for item in pipe.generate(
        text,
        voice=voice,
        speed=speed,
        lang_code=_lang_code_for_voice(voice),
    ):
        audio = item.audio
        if audio is not None:
            chunks.append(np.asarray(audio, dtype=np.float32))
    return chunks


def _synthesize(text: str, voice: str, speed: float) -> bytes:
    chunks: list[np.ndarray] = []
    pipe = _get_kokoro()
    selected_voice = voice or DEFAULT_VOICE

    voice_is_han = _lang_code_for_voice(selected_voice) == "z"
    segments = (
        [] if voice_is_han or not _HAN_RE.search(text) else _split_by_script(text)
    )

    if len(segments) > 1:
        han_voice = _han_voice_for(selected_voice)
        for run, is_han in segments:
            if not run.strip():
                continue
            chunks.extend(
                _generate(pipe, run, han_voice if is_han else selected_voice, speed)
            )
    else:
        chunks = _generate(pipe, text, selected_voice, speed)

    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    output = io.BytesIO()
    sf.write(output, np.concatenate(chunks), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return output.getvalue()


def _run_transcription(audio_bytes: bytes, language: str | None) -> str:
    with _mlx_inference_lock:
        return _transcribe(audio_bytes, language)


def _run_synthesis(text: str, voice: str, speed: float) -> bytes:
    with _mlx_inference_lock:
        return _synthesize(text, voice, speed)


def _clear_mlx_cache() -> None:
    with _mlx_inference_lock:
        mx.synchronize()
        mx.clear_cache()


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


@app.post("/v1/cache/clear")
async def clear_cache() -> dict[str, str]:
    """Release reusable MLX buffers while retaining both loaded models."""
    await asyncio.to_thread(_clear_mlx_cache)
    return {"status": "ok"}


def _available_voices() -> list[str]:
    """List the voice names bundled with the downloaded Kokoro model."""
    from huggingface_hub import snapshot_download

    try:
        root = pathlib.Path(
            snapshot_download(KOKORO_MODEL, allow_patterns=["voices/*"])
        )
    except Exception as exc:  # offline, or model not fetched yet
        raise HTTPException(
            status_code=503, detail=f"Kokoro voices unavailable: {exc}"
        ) from exc

    # Each voice ships as both .pt and .safetensors, so dedupe by stem.
    voices = sorted({p.stem for p in (root / "voices").glob("*") if p.is_file()})
    if not voices:
        raise HTTPException(status_code=503, detail="Kokoro shipped no voices")
    return voices


@app.get("/v1/audio/voices")
def voices() -> dict:
    """Kokoro voice list, in the shape kokoro-fastapi returns."""
    return {"voices": _available_voices()}


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
        text = await asyncio.to_thread(
            _run_transcription, await file.read(), language
        )
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
            _run_synthesis,
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
