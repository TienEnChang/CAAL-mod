"""Model switching is an idle-only operation that leaves the model warm."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from caal import webhooks


class _Rooms:
    def __init__(self, counts):
        self.rooms = [SimpleNamespace(num_participants=n) for n in counts]


def _api(counts=(), error=None):
    room = SimpleNamespace(
        list_rooms=AsyncMock(side_effect=error) if error else AsyncMock(return_value=_Rooms(counts))
    )
    return SimpleNamespace(room=room, aclose=AsyncMock())


@pytest.mark.asyncio
async def test_a_room_with_participants_counts_as_an_active_call():
    with patch.object(webhooks, "get_livekit_api", lambda: _api(counts=(1,))):
        assert await webhooks.call_is_active() is True


@pytest.mark.asyncio
async def test_an_empty_room_is_not_an_active_call():
    with patch.object(webhooks, "get_livekit_api", lambda: _api(counts=(0,))):
        assert await webhooks.call_is_active() is False


@pytest.mark.asyncio
async def test_unreachable_livekit_does_not_block_switching():
    """Refusing every switch while LiveKit is down would strand the setting."""
    with patch.object(webhooks, "get_livekit_api", lambda: _api(error=OSError("down"))):
        assert await webhooks.call_is_active() is False


@pytest.mark.asyncio
async def test_switching_during_a_call_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        webhooks.settings_module, "load_settings", lambda: {"openai_model": "old/model"}
    )
    with patch.object(webhooks, "call_is_active", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as raised:
            await webhooks.update_settings(
                webhooks.SettingsUpdateRequest(settings={"openai_model": "new/model"})
            )
    assert raised.value.status_code == 409
    assert "End the call" in raised.value.detail


@pytest.mark.asyncio
async def test_an_unchanged_model_is_never_treated_as_a_switch(monkeypatch):
    """Saving unrelated settings during a call must still work."""
    monkeypatch.setattr(
        webhooks.settings_module, "load_settings", lambda: {"openai_model": "same/model"}
    )
    active = AsyncMock(return_value=True)
    with patch.object(webhooks, "call_is_active", active):
        try:
            await webhooks.update_settings(
                webhooks.SettingsUpdateRequest(settings={"openai_model": "same/model"})
            )
        except HTTPException:
            pytest.fail("An unchanged model must not be refused")
        except Exception:
            pass  # save/reload touch real files; only the guard matters here
    active.assert_not_awaited()
