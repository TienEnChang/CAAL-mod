"""Mixed-script TTS segmentation in the native speech bridge.

A Kokoro voice phonemizes with one language's G2P, so an English voice handed
Han characters speaks the literal words "Chinese letter" once per character.
The bridge splits mixed text by script and voices each run separately; these
tests cover that split.

local_speech_server imports MLX packages that live only in the speech venv, so
they are stubbed - none of the code under test touches them.
"""

import sys
import types
from pathlib import Path

import pytest


def _load_module():
    for name in ("mlx", "mlx.core", "mlx_whisper", "mlx_whisper.transcribe",
                 "mlx_audio", "mlx_audio.tts", "mlx_audio.tts.utils", "soxr", "soundfile"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["mlx_whisper"].transcribe = lambda *a, **k: {}
    sys.modules["mlx_whisper.transcribe"].ModelHolder = object
    sys.modules["mlx_audio.tts.utils"].load = lambda *a, **k: None

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import local_speech_server

    return local_speech_server


speech = _load_module()


@pytest.mark.parametrize(
    "text,expected",
    [
        # The reported failure: an English sentence quoting a calendar entry.
        (
            "You have one event: 第三次请求撤销，全天。",
            [("You have one event: ", False), ("第三次请求撤销，全天。", True)],
        ),
        # Single-script text stays a single run.
        ("Hello, how are you today?", [("Hello, how are you today?", False)]),
        ("第三次请求撤销。", [("第三次请求撤销。", True)]),
    ],
)
def test_split_by_script(text, expected):
    assert speech._split_by_script(text) == expected


def test_interleaved_scripts_alternate_runs():
    runs = speech._split_by_script("会议 at 3pm 明天")
    assert [is_han for _, is_han in runs] == [True, False, True]


def test_digits_and_punctuation_never_become_their_own_run():
    """A run of only punctuation would be an empty utterance to synthesize."""
    for text in ["会议 at 3pm 明天", "You have one event: 第三次，全天。"]:
        for run, _ in speech._split_by_script(text):
            assert run.strip(), f"empty run in {text!r}"


def test_han_voice_matches_requested_gender():
    assert speech._han_voice_for("am_puck") == "zm_yunjian"
    assert speech._han_voice_for("af_heart") == "zf_xiaoxiao"
    assert speech._han_voice_for("bm_daniel") == "zm_yunjian"


def test_lang_code_derives_from_voice_prefix():
    assert speech._lang_code_for_voice("ff_siwis") == "f"
    assert speech._lang_code_for_voice("zf_xiaoxiao") == "z"
    assert speech._lang_code_for_voice("im_nicola") == "i"
    # Unknown prefixes fall back rather than passing an invalid lang_code.
    assert speech._lang_code_for_voice("qq_nobody") == speech.KOKORO_LANG
