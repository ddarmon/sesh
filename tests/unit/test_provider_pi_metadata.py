from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import pi
from tests.helpers import make_session, write_jsonl


def _session_header(session_id: str, cwd: str, ts: str) -> dict:
    return {"type": "session", "version": 3, "id": session_id, "timestamp": ts, "cwd": cwd}


def _user_message(text: str, ts: str, parent: str | None = None) -> dict:
    return {
        "type": "message",
        "id": f"u-{ts}",
        "parentId": parent,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_message(
    text: str, ts: str, *, model: str = "gpt-5", usage: dict | None = None
) -> dict:
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}], "model": model}
    if usage is not None:
        msg["usage"] = usage
    return {
        "type": "message",
        "id": f"a-{ts}",
        "parentId": None,
        "timestamp": ts,
        "message": msg,
    }


def test_encode_pi_path_wraps_with_double_dashes() -> None:
    assert pi.encode_pi_path("/Users/me/proj") == "--Users-me-proj--"
    assert pi.encode_pi_path("/Users/me/My Project") == "--Users-me-My-Project--"
    assert pi.encode_pi_path("/Users/daviddarmon") == "--Users-daviddarmon--"


def test_parse_timestamp_iso_z() -> None:
    assert pi._parse_timestamp("2026-04-03T11:10:54.342Z") == datetime(
        2026, 4, 3, 11, 10, 54, 342000, tzinfo=timezone.utc
    )


def test_parse_timestamp_epoch_millis() -> None:
    assert pi._parse_timestamp(1775214656903) == datetime.fromtimestamp(
        1775214656.903, tz=timezone.utc
    )


def test_extract_project_path_reads_first_session_header(tmp_path: Path) -> None:
    project_dir = tmp_path / "--repo--"
    write_jsonl(
        project_dir / "2026-01-01T00-00-00Z_aaa.jsonl",
        [
            _session_header("aaa", "/real/cwd", "2026-01-01T00:00:00Z"),
            _user_message("hi", "2026-01-01T00:00:01Z"),
        ],
    )
    assert pi._extract_project_path(project_dir) == "/real/cwd"


def test_discover_projects_skips_dirs_without_session_header(tmp_pi_dir) -> None:
    good_dir = tmp_pi_dir / pi.encode_pi_path("/repo/good")
    write_jsonl(
        good_dir / "2026-01-01T00-00-00Z_g.jsonl",
        [
            _session_header("g", "/repo/good", "2026-01-01T00:00:00Z"),
            _user_message("first", "2026-01-01T00:00:01Z"),
        ],
    )

    empty_dir = tmp_pi_dir / "--empty--"
    empty_dir.mkdir(parents=True)

    nojson_dir = tmp_pi_dir / "--nojson--"
    (nojson_dir).mkdir(parents=True)
    (nojson_dir / "noise.txt").write_text("nothing")

    provider = pi.PiProvider()
    projects = list(provider.discover_projects())
    assert projects == [("/repo/good", "good")]


def test_get_sessions_returns_one_per_file_sorted_newest_first(tmp_pi_dir) -> None:
    project_dir = tmp_pi_dir / pi.encode_pi_path("/repo/x")
    write_jsonl(
        project_dir / "2026-01-01T00-00-00Z_a.jsonl",
        [
            _session_header("a", "/repo/x", "2026-01-01T00:00:00Z"),
            _user_message("First session prompt", "2026-01-01T00:00:01Z"),
            _assistant_message("hi", "2026-01-01T00:00:02Z"),
        ],
    )
    write_jsonl(
        project_dir / "2026-02-01T00-00-00Z_b.jsonl",
        [
            _session_header("b", "/repo/x", "2026-02-01T00:00:00Z"),
            _user_message("Second session prompt", "2026-02-01T00:00:01Z"),
            _assistant_message("hi", "2026-02-01T00:00:02Z"),
        ],
    )

    provider = pi.PiProvider()
    list(provider.discover_projects())  # populate path-to-dir cache
    sessions = provider.get_sessions("/repo/x")

    assert [s.id for s in sessions] == ["b", "a"]
    assert all(s.provider is Provider.PI for s in sessions)
    assert sessions[0].summary == "Second session prompt"
    assert sessions[0].source_path.endswith("2026-02-01T00-00-00Z_b.jsonl")


def test_get_sessions_uses_cache_on_unchanged_files(tmp_pi_dir) -> None:
    project_dir = tmp_pi_dir / pi.encode_pi_path("/repo/c")
    file_path = project_dir / "2026-01-01T00-00-00Z_cached.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("cached", "/repo/c", "2026-01-01T00:00:00Z"),
            _user_message("hi", "2026-01-01T00:00:01Z"),
        ],
    )

    cached = make_session(
        id="cached-from-cache",
        project_path="/repo/c",
        provider=Provider.PI,
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
    provider = pi.PiProvider(cache=fake)
    list(provider.discover_projects())
    sessions = provider.get_sessions("/repo/c")
    assert [s.id for s in sessions] == ["cached-from-cache"]
    assert fake.put_calls == 0


def test_parse_session_file_extracts_token_usage(tmp_path: Path) -> None:
    file_path = tmp_path / "tokens.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("tok", "/repo", "2026-01-01T00:00:00Z"),
            _user_message("hi", "2026-01-01T00:00:01Z"),
            _assistant_message(
                "first reply",
                "2026-01-01T00:00:02Z",
                usage={
                    "input": 100,
                    "cacheRead": 50,
                    "cacheWrite": 25,
                    "output": 200,
                },
            ),
            _user_message("more", "2026-01-01T00:00:03Z"),
            _assistant_message(
                "second reply",
                "2026-01-01T00:00:04Z",
                usage={
                    "input": 300,
                    "cacheRead": 50,
                    "cacheWrite": 0,
                    "output": 150,
                },
            ),
        ],
    )

    data = pi.PiProvider()._parse_session_file(file_path, "/repo")
    assert data is not None
    # Last assistant turn's input + cache variants
    assert data["input_tokens"] == 350
    # Sum of all assistant outputs
    assert data["output_tokens"] == 350
    # Sum of all per-turn input + cache variants
    assert data["cumulative_input_tokens"] == 525


def test_parse_session_file_no_usage_returns_none_tokens(tmp_path: Path) -> None:
    file_path = tmp_path / "notokens.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("tok", "/repo", "2026-01-01T00:00:00Z"),
            _user_message("hi", "2026-01-01T00:00:01Z"),
            _assistant_message("no usage", "2026-01-01T00:00:02Z"),
        ],
    )
    data = pi.PiProvider()._parse_session_file(file_path, "/repo")
    assert data is not None
    assert data["input_tokens"] is None
    assert data["output_tokens"] is None
    assert data["cumulative_input_tokens"] is None


def test_parse_session_file_falls_back_to_filename_uuid(tmp_path: Path) -> None:
    """If the session header is malformed, the trailing UUID in the filename is used."""
    file_path = tmp_path / "2026-01-01T00-00-00Z_fallback-uuid.jsonl"
    file_path.write_text(
        # No session header line; only a message
        '{"type":"message","timestamp":"2026-01-01T00:00:01Z",'
        '"message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
    )
    data = pi.PiProvider()._parse_session_file(file_path, "/repo")
    assert data is not None
    assert data["id"] == "fallback-uuid"
    # cwd defaults to the project_path supplied by the caller
    assert data["cwd"] == "/repo"


def test_get_messages_fans_out_content_blocks(tmp_pi_dir) -> None:
    """pi: toolCall blocks in assistant + toolResult role messages (not Claude-style)."""
    project_dir = tmp_pi_dir / pi.encode_pi_path("/repo/m")
    file_path = project_dir / "2026-01-01T00-00-00Z_msg.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("msg", "/repo/m", "2026-01-01T00:00:00Z"),
            {
                "type": "message",
                "id": "u1",
                "parentId": None,
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Run a tool please"}],
                },
            },
            {
                "type": "message",
                "id": "a1",
                "parentId": "u1",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "let me think..."},
                        {
                            "type": "toolCall",
                            "id": "call_1",
                            "name": "shell",
                            "arguments": {"cmd": "ls"},
                        },
                    ],
                },
            },
            {
                "type": "message",
                "id": "tr1",
                "parentId": "a1",
                "timestamp": "2026-01-01T00:00:03Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call_1",
                    "toolName": "shell",
                    "content": [{"type": "text", "text": "file1\nfile2"}],
                },
            },
            {
                "type": "message",
                "id": "a2",
                "parentId": "tr1",
                "timestamp": "2026-01-01T00:00:04Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done."}],
                },
            },
        ],
    )

    session = make_session(
        id="msg",
        project_path="/repo/m",
        provider=Provider.PI,
        source_path=str(file_path),
    )
    msgs = pi.PiProvider().get_messages(session)
    types = [(m.role, m.content_type) for m in msgs]
    assert types == [
        ("user", "text"),
        ("assistant", "thinking"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
        ("assistant", "text"),
    ]
    tool_result = msgs[3]
    assert tool_result.tool_name == "shell"
    assert "file1" in tool_result.tool_output
    tool_use = msgs[2]
    assert '"cmd"' in tool_use.tool_input


def test_get_messages_flags_system_user_text(tmp_pi_dir) -> None:
    project_dir = tmp_pi_dir / pi.encode_pi_path("/repo/s")
    file_path = project_dir / "2026-01-01T00-00-00Z_s.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("s", "/repo/s", "2026-01-01T00:00:00Z"),
            _user_message("<system-reminder>do thing</system-reminder>", "2026-01-01T00:00:01Z"),
            _user_message("real prompt", "2026-01-01T00:00:02Z"),
        ],
    )
    session = make_session(provider=Provider.PI, source_path=str(file_path))
    msgs = pi.PiProvider().get_messages(session)
    assert len(msgs) == 2
    assert msgs[0].is_system is True
    assert msgs[1].is_system is False
