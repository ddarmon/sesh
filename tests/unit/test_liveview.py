from __future__ import annotations

import json
from urllib.request import urlopen

import pytest

from sesh.liveview import LiveViewError, LiveViewServer
from sesh.models import Provider
from tests.helpers import make_message, make_session


def _get(url: str) -> tuple[dict, object]:
    with urlopen(url, timeout=2) as response:
        headers = response.headers
        body = json.loads(response.read().decode("utf-8"))
    return body, headers


def test_live_server_serves_private_page_and_changed_revisions() -> None:
    messages = [make_message(content="first")]
    session = make_session(provider=Provider.PI)
    server = LiveViewServer(lambda: (session, list(messages), None), poll_ms=250)
    try:
        url = server.start()
        assert url.startswith("http://127.0.0.1:")
        assert server.running

        with urlopen(url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        assert "● LIVE" in page
        assert '"api": "./api/session"' in page
        assert "first" in page

        first, headers = _get(url + "api/session")
        assert headers["Cache-Control"] == "no-store"
        assert first["revision"] == 1
        assert first["error"] is None
        assert first["payload"]["provider"] == "pi"

        unchanged, _ = _get(url + "api/session?revision=1")
        assert unchanged["revision"] == 1
        assert unchanged["payload"] is None

        messages.append(make_message(role="assistant", content="second"))
        second, _ = _get(url + "api/session?revision=1")
        assert second["revision"] == 2
        assert [m["content"] for m in second["payload"]["messages"]] == [
            "first",
            "second",
        ]
    finally:
        server.stop()
    assert not server.running


def test_live_server_keeps_last_good_snapshot_after_refresh_error() -> None:
    state = {"fail": False}
    session = make_session(provider=Provider.GEMINI)

    def loader():
        if state["fail"]:
            raise ValueError("partial JSON")
        return session, [make_message(content="last good")], None

    server = LiveViewServer(loader)
    try:
        url = server.start()
        state["fail"] = True
        update, _ = _get(url + "api/session")
        assert update["revision"] == 1
        assert update["payload"]["messages"][0]["content"] == "last good"
        assert update["error"] == "partial JSON"
    finally:
        server.stop()


def test_live_server_requires_a_valid_initial_snapshot() -> None:
    server = LiveViewServer(lambda: (_ for _ in ()).throw(ValueError("broken")))
    with pytest.raises(LiveViewError, match="Could not load session: broken"):
        server.start()
    assert not server.running


@pytest.mark.parametrize("provider", list(Provider))
def test_live_server_payload_is_provider_agnostic(provider: Provider) -> None:
    session = make_session(provider=provider)
    server = LiveViewServer(lambda: (session, [make_message()], None))
    try:
        url = server.start()
        update, _ = _get(url + "api/session")
        assert update["payload"]["provider"] == provider.value
    finally:
        server.stop()
