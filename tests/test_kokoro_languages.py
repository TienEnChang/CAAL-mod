"""Kokoro multilingual voice selection."""

import json
import re
from pathlib import Path

from caal.settings import KOKORO_LANGUAGES, KOKORO_VOICE_MAP, PIPER_VOICE_MAP, kokoro_supports

# Language codes Kokoro-82M ships trained voices for. The first letter of a
# voice name is its language code.
KOKORO_CODES = set("abefhijpz")

PANEL = Path(__file__).parent.parent / "frontend/components/settings/settings-panel.tsx"


def test_supported_languages():
    for language in ("en", "fr", "it", "pt"):
        assert kokoro_supports(language), language


def test_languages_without_a_kokoro_voice_fall_back():
    """Danish and Romanian have no Kokoro voice and must use Piper."""
    for language in ("da", "ro"):
        assert not kokoro_supports(language)
        assert language in PIPER_VOICE_MAP


def test_every_default_voice_encodes_its_language():
    """A voice whose prefix is not a Kokoro language code would be phonemized
    with the fallback language, which is the bug this map exists to prevent."""
    for language, voice in KOKORO_VOICE_MAP.items():
        assert voice[0] in KOKORO_CODES, f"{language}: {voice}"


def test_non_english_defaults_are_not_english_voices():
    for language, voice in KOKORO_VOICE_MAP.items():
        if language == "en":
            continue
        assert voice[0] not in {"a", "b"}, f"{language} defaults to English voice {voice}"


def test_every_kokoro_language_has_a_piper_fallback():
    """Piper must still cover these, since Kokoro can be switched off."""
    for language in KOKORO_LANGUAGES:
        assert language in PIPER_VOICE_MAP


def test_frontend_voice_map_matches_backend():
    """The picker duplicates this map; drift would show voices the agent then
    overrides, so the two must stay identical."""
    source = PANEL.read_text()
    block = re.search(
        r"const KOKORO_VOICES: Record<string, string> = \{(.*?)\};", source, re.S
    )
    assert block, "KOKORO_VOICES not found in settings-panel.tsx"
    frontend = dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))
    assert frontend == KOKORO_VOICE_MAP, json.dumps(
        {"frontend": frontend, "backend": KOKORO_VOICE_MAP}, indent=2
    )
