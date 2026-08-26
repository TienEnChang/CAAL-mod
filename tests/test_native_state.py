from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "native_state.py"
SPEC = importlib.util.spec_from_file_location("native_state", MODULE_PATH)
assert SPEC and SPEC.loader
native_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native_state)


def test_encrypted_payload_round_trip() -> None:
    payload = b"settings, credentials, and conversation state"
    password = b"correct horse battery staple"

    encrypted = native_state.encrypt_payload(payload, password)

    assert payload not in encrypted
    assert native_state.decrypt_payload(encrypted, password) == payload


def test_wrong_password_is_rejected() -> None:
    encrypted = native_state.encrypt_payload(b"secret", b"long correct password")

    with pytest.raises(native_state.StateError, match="Incorrect passphrase"):
        native_state.decrypt_payload(encrypted, b"long incorrect password")


def test_password_uses_keychain_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = b"keychain-generated-password"
    monkeypatch.setattr(
        native_state,
        "_keychain_password",
        lambda *, create: expected if create else b"existing-keychain-password",
    )

    assert native_state._password(
        None, confirm=True, prompt=False, create_keychain=True
    ) == expected
    assert native_state._password(
        None, confirm=False, prompt=False, create_keychain=False
    ) == b"existing-keychain-password"


def test_archive_round_trip_and_manifest() -> None:
    files = {
        "native/config/settings.json": (
            b'{"n8n_enabled": false, "n8n_url": "", '
            b'"n8n_token": "", "n8n_api_key": ""}'
        ),
    }
    archive = native_state._make_archive(files, Path("/does/not/exist"))
    manifest, restored = native_state._read_archive(archive)

    assert manifest["format"] == native_state.FORMAT_VERSION
    assert restored == files
