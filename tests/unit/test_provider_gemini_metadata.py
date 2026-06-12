from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import gemini
from tests.helpers import make_session, write_gemini_projects, write_gemini_session


def _user(text: str, ts: str = "2026-01-01T00:00:01.000Z") -> dict:
    return {"id": f"u-{ts}", "timestamp": ts, "type": "user", "content": [{"text": text}]}


def _gemini_msg(
    text: str,
    ts: str = "2026-01-01T00:00:02.000Z",
    *,
    model: str = "gemini-3-pro-preview",
    tokens: dict | None = None,
    thoughts: list | None = None,
    tool_calls: list | None = None,
) -> dict:
    msg: dict = {"id": f"g-{ts}", "timestamp": ts, "type": "gemini", "content": text, "model": model}
    if tokens is not None:
        msg["tokens"] = tokens
    if thoughts is not None:
        msg["thoughts"] = thoughts
    if tool_calls is not None:
        msg["toolCalls"] = tool_calls
    return msg


def test_hash_gemini_path_is_sha256_hex() -> None:
    assert gemini.hash_gemini_path("/Users/me/proj") == (
        "42a9f445a46de17a9dae6b76137b85d7a365145a8211ef576ef3b5ba313415d1"
    )
    assert len(gemini.hash_gemini_path("/x")) == 64


def test_parse_timestamp_iso_z() -> None:
    assert gemini._parse_timestamp("2026-02-22T17:02:37.729Z") == datetime(
        2026, 2, 22, 17, 2, 37, 729000, tzinfo=timezone.utc
    )


def test_discover_projects_resolves_named_dir_via_projects_json(tmp_gemini_dir: Path) -> None:
    write_gemini_projects(tmp_gemini_dir, {"/Users/me/proj": "proj"})
    write_gemini_session(
        tmp_gemini_dir / "tmp" / "proj" / "chats" / "session-2026-01-01T00-00-aaaa.json",
        messages=[_user("hello")],
    )

    provider = gemini.GeminiProvider()
    projects = list(provider.discover_projects())
    assert projects == [("/Users/me/proj", "proj")]


def test_discover_projects_resolves_hash_dir_via_projects_json(tmp_gemini_dir: Path) -> None:
    """A hash-named dir resolves when its name is sha256 of a known path."""
    path = "/Users/me/other"
    write_gemini_projects(tmp_gemini_dir, {path: "other"})
    hashed = gemini.hash_gemini_path(path)
    write_gemini_session(
        tmp_gemini_dir / "tmp" / hashed / "chats" / "session-2026-01-01T00-00-bbbb.json",
        messages=[_user("hi")],
    )

    provider = gemini.GeminiProvider()
    projects = list(provider.discover_projects())
    assert projects == [(path, "other")]


def test_discover_projects_unresolved_hash_falls_back_to_tmp_dir(tmp_gemini_dir: Path) -> None:
    """SHA-256 of the cwd is not invertible; unknown hashes keep the tmp dir."""
    hashed = "ab" * 32
    write_gemini_session(
        tmp_gemini_dir / "tmp" / hashed / "chats" / "session-2026-01-01T00-00-cccc.json",
        messages=[_user("hi")],
    )

    provider = gemini.GeminiProvider()
    projects = list(provider.discover_projects())
    assert len(projects) == 1
    project_path, display_name = projects[0]
    assert project_path == str(tmp_gemini_dir / "tmp" / hashed)
    assert display_name == f"gemini:{hashed[:8]}"


def test_discover_projects_skips_dirs_without_session_files(tmp_gemini_dir: Path) -> None:
    # logs.json only (older Gemini CLI usage) — no chats/ session files.
    logs_only = tmp_gemini_dir / "tmp" / ("cd" * 32)
    logs_only.mkdir(parents=True)
    (logs_only / "logs.json").write_text("[]")

    # Empty chats dir.
    empty_chats = tmp_gemini_dir / "tmp" / ("ef" * 32) / "chats"
    empty_chats.mkdir(parents=True)

    # Non-directory entry (e.g. the bundled rg binary dir is a dir, but
    # a stray file must not break discovery either).
    (tmp_gemini_dir / "tmp" / "stray.txt").write_text("noise")

    provider = gemini.GeminiProvider()
    assert list(provider.discover_projects()) == []


def test_get_sessions_one_per_file_sorted_newest_first(tmp_gemini_dir: Path) -> None:
    write_gemini_projects(tmp_gemini_dir, {"/repo/x": "x"})
    chats = tmp_gemini_dir / "tmp" / "x" / "chats"
    write_gemini_session(
        chats / "session-2026-01-01T00-00-aaaa.json",
        session_id="aaa",
        start_time="2026-01-01T00:00:00.000Z",
        last_updated="2026-01-01T00:30:00.000Z",
        messages=[_user("First session prompt"), _gemini_msg("hi")],
    )
    write_gemini_session(
        chats / "session-2026-02-01T00-00-bbbb.json",
        session_id="bbb",
        start_time="2026-02-01T00:00:00.000Z",
        last_updated="2026-02-01T00:30:00.000Z",
        messages=[_user("Second session prompt"), _gemini_msg("hi")],
    )

    provider = gemini.GeminiProvider()
    list(provider.discover_projects())
    sessions = provider.get_sessions("/repo/x")

    assert [s.id for s in sessions] == ["bbb", "aaa"]
    assert all(s.provider is Provider.GEMINI for s in sessions)
    assert sessions[0].summary == "Second session prompt"
    assert sessions[0].message_count == 2
    assert sessions[0].model == "gemini-3-pro-preview"
    assert sessions[0].source_path.endswith("session-2026-02-01T00-00-bbbb.json")
    assert sessions[0].start_timestamp == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert sessions[0].timestamp == datetime(2026, 2, 1, 0, 30, tzinfo=timezone.utc)


def test_get_sessions_merges_hash_and_named_dirs_for_same_path(tmp_gemini_dir: Path) -> None:
    """A project named after old hash-dir sessions exist keeps both dirs."""
    path = "/repo/dual"
    write_gemini_projects(tmp_gemini_dir, {path: "dual"})
    write_gemini_session(
        tmp_gemini_dir / "tmp" / "dual" / "chats" / "session-2026-02-01T00-00-new1.json",
        session_id="new-session",
        last_updated="2026-02-01T00:00:00.000Z",
        messages=[_user("new")],
    )
    write_gemini_session(
        tmp_gemini_dir
        / "tmp"
        / gemini.hash_gemini_path(path)
        / "chats"
        / "session-2026-01-01T00-00-old1.json",
        session_id="old-session",
        last_updated="2026-01-01T00:00:00.000Z",
        messages=[_user("old")],
    )

    provider = gemini.GeminiProvider()
    projects = list(provider.discover_projects())
    assert projects == [(path, "dual")]

    sessions = provider.get_sessions(path)
    assert [s.id for s in sessions] == ["new-session", "old-session"]


def test_get_sessions_uses_cache_on_unchanged_files(tmp_gemini_dir: Path) -> None:
    write_gemini_projects(tmp_gemini_dir, {"/repo/c": "c"})
    file_path = (
        tmp_gemini_dir / "tmp" / "c" / "chats" / "session-2026-01-01T00-00-cach.json"
    )
    write_gemini_session(file_path, session_id="cached", messages=[_user("hi")])

    cached = make_session(
        id="cached-from-cache",
        project_path="/repo/c",
        provider=Provider.GEMINI,
        source_path=str(file_path),
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    class FakeCache:
        def __init__(self) -> None:
            self.put_calls = 0

        def get_sessions(self, path: str):
            assert path == str(file_path)
            return [cached]

        def put_sessions(self, path: str, sessions) -> None:
            self.put_calls += 1

    fake = FakeCache()
    provider = gemini.GeminiProvider(cache=fake)
    list(provider.discover_projects())
    sessions = provider.get_sessions("/repo/c")
    assert [s.id for s in sessions] == ["cached-from-cache"]
    assert fake.put_calls == 0


def test_parse_session_file_extracts_token_usage(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-tok1.json"
    write_gemini_session(
        file_path,
        messages=[
            _user("hi"),
            _gemini_msg(
                "first",
                tokens={"input": 5761, "output": 105, "cached": 2775, "thoughts": 390, "tool": 0, "total": 6256},
            ),
            _user("more"),
            _gemini_msg(
                "second",
                tokens={"input": 29650, "output": 126, "cached": 7379, "thoughts": 399, "tool": 0, "total": 30175},
            ),
        ],
    )

    data = gemini.GeminiProvider()._parse_session_file(file_path)
    assert data is not None
    # Last gemini turn's input (already includes cached tokens)
    assert data["input_tokens"] == 29650
    # Sum of output + thoughts across turns
    assert data["output_tokens"] == 105 + 390 + 126 + 399
    # Sum of all per-turn inputs
    assert data["cumulative_input_tokens"] == 5761 + 29650


def test_parse_session_file_no_tokens_returns_none_tokens(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-not1.json"
    write_gemini_session(file_path, messages=[_user("hi"), _gemini_msg("no tokens")])

    data = gemini.GeminiProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["input_tokens"] is None
    assert data["output_tokens"] is None
    assert data["cumulative_input_tokens"] is None


def test_parse_session_file_prefers_stored_summary(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-sum1.json"
    write_gemini_session(
        file_path,
        summary="A stored summary.",
        messages=[_user("Some long first prompt")],
    )
    data = gemini.GeminiProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["summary"] == "A stored summary."


def test_parse_session_file_summary_skips_slash_commands(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-sum2.json"
    write_gemini_session(
        file_path,
        messages=[_user("/model"), _user("Real first prompt")],
    )
    data = gemini.GeminiProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["summary"] == "Real first prompt"


def test_parse_session_file_falls_back_to_filename_stem(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-fall.json"
    file_path.write_text('{"messages": []}')
    data = gemini.GeminiProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["id"] == "session-2026-01-01T00-00-fall"


def test_parse_session_file_handles_corrupt_json(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-bad1.json"
    file_path.write_text("{not json")
    assert gemini.GeminiProvider()._parse_session_file(file_path) is None


def test_read_session_id_streams_file_head(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-head.json"
    write_gemini_session(file_path, session_id="head-id", messages=[_user("hi")])
    assert gemini.read_session_id(file_path) == "head-id"


def test_resolve_chats_project_path_shared_with_search(tmp_gemini_dir: Path) -> None:
    write_gemini_projects(tmp_gemini_dir, {"/Users/me/proj": "proj"})
    tmp_dir = tmp_gemini_dir / "tmp"

    named = tmp_dir / "proj"
    assert gemini.resolve_chats_project_path(named, tmp_gemini_dir) == "/Users/me/proj"

    hashed = tmp_dir / gemini.hash_gemini_path("/Users/me/proj")
    assert gemini.resolve_chats_project_path(hashed, tmp_gemini_dir) == "/Users/me/proj"

    unknown = tmp_dir / ("99" * 32)
    assert gemini.resolve_chats_project_path(unknown, tmp_gemini_dir) == str(unknown)
