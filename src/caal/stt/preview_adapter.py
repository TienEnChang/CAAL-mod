"""Add periodic interim transcripts to a fast, non-streaming STT backend."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from livekit import rtc
from livekit.agents import stt, utils, vad
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)
from livekit.agents.vad import VADEventType


class PreviewStreamAdapter(stt.STT):
    """Wrap batch STT with low-rate preview recognition during speech.

    Final recognition still runs over the VAD's authoritative full utterance.
    Preview requests are deliberately infrequent to avoid delaying the final
    transcript or monopolizing the local Metal model.
    """

    def __init__(
        self,
        *,
        inner_stt: stt.STT,
        vad_instance: vad.VAD,
        preview_interval: float = 1.0,
        minimum_audio: float = 0.9,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
            )
        )
        self._inner = inner_stt
        self._vad = vad_instance
        self._preview_interval = preview_interval
        self._minimum_audio = minimum_audio
        self._inner.on("metrics_collected", self._on_metrics_collected)

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        return await self._inner.recognize(
            buffer=buffer,
            language=language,
            conn_options=conn_options,
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _PreviewRecognizeStream(
            adapter=self,
            vad_instance=self._vad,
            language=language,
            conn_options=conn_options,
        )

    def _on_metrics_collected(self, *args: Any, **kwargs: Any) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    async def aclose(self) -> None:
        self._inner.off("metrics_collected", self._on_metrics_collected)
        await self._inner.aclose()


class _PreviewRecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        adapter: PreviewStreamAdapter,
        vad_instance: vad.VAD,
        language: NotGivenOr[str],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=adapter, conn_options=conn_options)
        self._adapter = adapter
        self._vad = vad_instance
        self._language = language
        self._inner_conn_options = conn_options

    async def _run(self) -> None:
        vad_stream = self._vad.stream()
        speech_frames: list[rtc.AudioFrame] = []
        speech_active = False
        last_preview_at = 0.0
        last_preview_text = ""
        preview_task: asyncio.Task[None] | None = None
        utterance_generation = 0

        async def recognize_preview(
            frames: list[rtc.AudioFrame], generation: int
        ) -> None:
            nonlocal last_preview_text
            event = await self._adapter._inner.recognize(
                buffer=frames,
                language=self._language,
                conn_options=self._inner_conn_options,
            )
            if generation != utterance_generation or not event.alternatives:
                return
            alternative = event.alternatives[0]
            text = alternative.text.strip()
            if not text or text == last_preview_text:
                return
            last_preview_text = text
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[alternative],
                )
            )

        async def forward_input() -> None:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    vad_stream.flush()
                else:
                    vad_stream.push_frame(item)
            vad_stream.end_input()

        async def recognize() -> None:
            nonlocal speech_active, speech_frames, last_preview_at
            nonlocal preview_task, utterance_generation, last_preview_text

            async for event in vad_stream:
                if event.type == VADEventType.START_OF_SPEECH:
                    utterance_generation += 1
                    speech_active = True
                    speech_frames = list(event.frames)
                    last_preview_text = ""
                    last_preview_at = time.monotonic()
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                    )
                    continue

                if event.type == VADEventType.INFERENCE_DONE and speech_active:
                    speech_frames.extend(event.frames)
                    duration = sum(frame.duration for frame in speech_frames)
                    now = time.monotonic()
                    if (
                        duration >= self._adapter._minimum_audio
                        and now - last_preview_at >= self._adapter._preview_interval
                        and (preview_task is None or preview_task.done())
                    ):
                        last_preview_at = now
                        preview_task = asyncio.create_task(
                            recognize_preview(
                                list(speech_frames), utterance_generation
                            ),
                            name="stt_preview",
                        )
                    continue

                if event.type != VADEventType.END_OF_SPEECH:
                    continue

                speech_active = False
                utterance_generation += 1
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                )
                if preview_task and not preview_task.done():
                    try:
                        await preview_task
                    except Exception:
                        pass
                final_event = await self._adapter._inner.recognize(
                    buffer=utils.merge_frames(event.frames),
                    language=self._language,
                    conn_options=self._inner_conn_options,
                )
                if final_event.alternatives and final_event.alternatives[0].text.strip():
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                            alternatives=[final_event.alternatives[0]],
                        )
                    )
                speech_frames = []
                preview_task = None

        tasks = [
            asyncio.create_task(forward_input(), name="preview_stt_input"),
            asyncio.create_task(recognize(), name="preview_stt_recognition"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            if preview_task:
                preview_task.cancel()
            await utils.aio.cancel_and_wait(*tasks)
            await vad_stream.aclose()
