from __future__ import annotations

from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError

from nonebot_plugin_weather_fighter import matcher as matcher_mod
from nonebot_plugin_weather_fighter.matcher import _send_chart


class FakeMatcher:
    def __init__(self, errors: list[BaseException] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls = 0
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.calls += 1
        self.sent.append(message)
        if self.errors:
            raise self.errors.pop(0)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matcher_mod, "CHART_SEND_RETRY_DELAY", 0)


async def test_chart_sent_on_first_try() -> None:
    fake = FakeMatcher()
    assert await _send_chart(fake, b"png") is True  # type: ignore[arg-type]
    assert fake.calls == 1
    # 必须发 Message 而不是裸 MessageSegment：第三方插件会 for seg in message
    assert isinstance(fake.sent[0], Message)
    assert fake.sent[0][0].type == "image"


async def test_network_error_retries_once_then_succeeds() -> None:
    fake = FakeMatcher([NetworkError("ws timeout")])
    assert await _send_chart(fake, b"png") is True  # type: ignore[arg-type]
    assert fake.calls == 2


async def test_api_unavailable_counts_as_connection_error() -> None:
    fake = FakeMatcher([ApiNotAvailable()])
    assert await _send_chart(fake, b"png") is True  # type: ignore[arg-type]
    assert fake.calls == 2


async def test_action_failed_is_not_retried() -> None:
    fake = FakeMatcher([ActionFailed(retcode=100, msg="file too large")])
    assert await _send_chart(fake, b"png") is False  # type: ignore[arg-type]
    assert fake.calls == 1


async def test_gives_up_after_all_attempts() -> None:
    fake = FakeMatcher([NetworkError("a"), NetworkError("b")])
    assert await _send_chart(fake, b"png") is False  # type: ignore[arg-type]
    assert fake.calls == 2


async def test_empty_png_is_skipped() -> None:
    fake = FakeMatcher()
    assert await _send_chart(fake, b"") is False  # type: ignore[arg-type]
    assert fake.calls == 0


async def test_type_error_from_hooked_send_falls_back_without_retry() -> None:
    # nonebot-plugin-emotionimg 等插件魔改 Bot.send 后可能抛 TypeError
    fake = FakeMatcher([TypeError("'MessageSegment' object is not iterable")])
    assert await _send_chart(fake, b"png") is False  # type: ignore[arg-type]
    assert fake.calls == 1
