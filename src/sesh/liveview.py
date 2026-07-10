"""Private loopback server for live-updating browser session views."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypeAlias
from urllib.parse import parse_qs, urlsplit

from sesh.export import format_session_html, session_html_payload
from sesh.models import Message, SessionMeta, SubagentMeta

LiveSnapshot: TypeAlias = tuple[
    SessionMeta,
    list[Message],
    list[tuple[SubagentMeta, list[Message]]] | None,
]
LiveLoader: TypeAlias = Callable[[], LiveSnapshot]


class LiveViewError(RuntimeError):
    """Raised when a live browser view cannot be started."""


class LiveViewServer:
    """Serve one provider-normalized session on a private loopback URL.

    The loader is provider-agnostic: it returns the same ``SessionMeta`` and
    ``Message`` objects used by static export. A failed refresh retains the
    last good payload so a transient partial JSON write or SQLite lock never
    blanks the browser view.
    """

    def __init__(self, loader: LiveLoader, *, poll_ms: int = 1500) -> None:
        self._loader = loader
        self.poll_ms = max(250, poll_ms)
        self._token = secrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._refresh_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._snapshot: LiveSnapshot | None = None
        self._payload: dict | None = None
        self._digest: str | None = None
        self._revision = 0
        self._error: str | None = None
        self._updated_at: str | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise LiveViewError("Live view server is not running")
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/{self._token}/"

    def start(self) -> str:
        """Load the first snapshot, start the server, and return its URL."""
        if self.running:
            return self.url
        try:
            self._refresh()
        except Exception as exc:
            raise LiveViewError(f"Could not load session: {exc}") from exc

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                path = self.path.split("?", 1)[0]
                root = f"/{owner._token}/"
                if path == root:
                    owner._serve_page(self)
                elif path == f"{root}api/session":
                    owner._serve_api(self)
                else:
                    self.send_error(404)

            def log_message(self, _format: str, *args: object) -> None:
                return

        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            raise LiveViewError(f"Could not bind loopback server: {exc}") from exc
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="sesh-live-view",
            daemon=True,
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        """Stop the loopback server. Safe to call more than once."""
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _refresh(self) -> None:
        """Load and publish a snapshot, retaining old state on later errors."""
        with self._refresh_lock:
            try:
                snapshot = self._loader()
                session, messages, subagents = snapshot
                payload = session_html_payload(session, messages, subagents)
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                digest = hashlib.sha256(encoded).hexdigest()
            except Exception as exc:
                with self._state_lock:
                    self._error = str(exc) or exc.__class__.__name__
                raise

            with self._state_lock:
                self._snapshot = snapshot
                self._payload = payload
                if digest != self._digest:
                    self._revision += 1
                    self._digest = digest
                self._error = None
                self._updated_at = datetime.now(tz=timezone.utc).isoformat()

    def _response_headers(
        self,
        handler: BaseHTTPRequestHandler,
        content_type: str,
        length: int,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(length))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        if content_type.startswith("text/html"):
            handler.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; font-src data:; "
                "img-src data: http: https:; connect-src 'self'",
            )
        handler.end_headers()

    def _serve_page(self, handler: BaseHTTPRequestHandler) -> None:
        with self._state_lock:
            snapshot = self._snapshot
            revision = self._revision
        if snapshot is None:
            handler.send_error(503)
            return
        session, messages, subagents = snapshot
        page = format_session_html(
            session,
            messages,
            subagents,
            live_api="./api/session",
            live_revision=revision,
            live_poll_ms=self.poll_ms,
        ).encode("utf-8")
        self._response_headers(handler, "text/html; charset=utf-8", len(page))
        handler.wfile.write(page)

    def _serve_api(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            self._refresh()
        except Exception:
            # The last good state and a short error are returned below.
            pass
        query = parse_qs(urlsplit(handler.path).query)
        try:
            client_revision = int(query.get("revision", ["-1"])[0])
        except (TypeError, ValueError):
            client_revision = -1
        with self._state_lock:
            response = {
                "revision": self._revision,
                "updated_at": self._updated_at,
                "payload": (
                    None if client_revision == self._revision else self._payload
                ),
                "error": self._error,
            }
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self._response_headers(handler, "application/json; charset=utf-8", len(body))
        handler.wfile.write(body)
