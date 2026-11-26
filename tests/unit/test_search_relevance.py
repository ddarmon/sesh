"""Text-only matching, the shared matcher, and title/ranking for search."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sesh import search
from sesh.models import Provider, SearchResult
from tests.helpers import create_opencode_db, make_session, write_jsonl

# A long base64-looking run that happens to contain the query "wiz".
BLOB = "gAAAAABpD2iHCox57CKV64orXrGHWSUQzvXZv2NlJVS7TftDKwiZ9xcSlTwqV" * 4


# ---------------------------------------------------------------------------
# _Matcher
# ---------------------------------------------------------------------------


def test_matcher_literal_is_case_insensitive_and_not_regex() -> None:
    m = search._Matcher("Wiz")
    assert m.literal and m.verified
    assert m.matches("a setup wizard")
    assert not m.matches("nothing here")
    # No metacharacters, so a dot elsewhere is never a wildcard.
    assert not search._Matcher("a b").matches("axb")


def test_matcher_regex_word_boundary() -> None:
    m = search._Matcher(r"\bwiz\b")
    assert not m.literal and m.verified
    assert m.matches("We use Wiz in production")
    assert not m.matches("a setup wizard")


def test_matcher_snippet_centers_on_regex_match() -> None:
    m = search._Matcher(r"\bwiz\b")
    text = ("x" * 400) + " we use Wiz here " + ("y" * 400)
    snippet = m.snippet(text, max_len=60)
    assert "Wiz" in snippet
    assert snippet.startswith("...") and snippet.endswith("...")
    assert len(snippet) == 60


def test_matcher_unverifiable_regex_accepts_text_instead_of_nothing() -> None:
    """ripgrep takes \\p{L}; Python's re does not. Degrade, don't go silent."""
    m = search._Matcher(r"\p{L}+needle")
    assert not m.verified
    assert m.matches("anything")
    assert not m.matches("")


def test_matcher_best_returns_empty_when_nothing_matches() -> None:
    m = search._Matcher("needle")
    assert m.best(["hay", "more hay"]) == ""
    assert m.best(["hay", "a needle", "the long needle here"]) == "the long needle here"


def test_like_filter_only_narrows_literal_queries() -> None:
    assert search._like_filter("data", search._Matcher("50%")) == (
        "data LIKE ? ESCAPE '!'", ("%50!%%",),
    )
    assert search._like_filter("data", search._Matcher(r"\bwiz\b")) == ("1", ())


# ---------------------------------------------------------------------------
# _entry_texts: only what the transcript renders
# ---------------------------------------------------------------------------


def test_entry_texts_ignores_claude_signature_and_image_payloads() -> None:
    entry = {
        "type": "assistant",
        "cwd": "/Users/me/wiz-project",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "plan the work", "signature": BLOB},
                {"type": "text", "text": "done"},
                {"type": "image", "source": {"type": "base64", "data": BLOB}},
            ],
        },
    }
    texts = search._entry_texts(entry)
    assert texts == ["plan the work", "done"]
    assert search._Matcher("wiz").best(texts) == ""


def test_entry_texts_ignores_claude_api_request_blob() -> None:
    """An api-request-blob holds the raw request (system prompt, skill lists)."""
    entry = {
        "type": "api-request-blob",
        "message": {"role": "user", "content": "skill: make Anki cards"},
    }
    assert search._entry_texts(entry) == []


def test_entry_texts_pi_tool_call_arguments() -> None:
    entry = {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": "c1", "name": "bash",
                 "arguments": {"command": "rg needle"}},
                {"type": "thinking", "thinking": "hm", "thinkingSignature": BLOB},
            ],
        },
    }
    texts = search._entry_texts(entry)
    assert json.dumps({"command": "rg needle"}) in texts
    assert BLOB not in texts


def test_entry_texts_codex_custom_tool_call_and_output() -> None:
    call = {"type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch",
                        "input": "*** needle patch"}}
    out = {"type": "response_item",
           "payload": {"type": "custom_tool_call_output", "output": "needle ok"}}
    assert search._entry_texts(call) == ["*** needle patch"]
    assert "needle ok" in search._entry_texts(out)


def test_entry_texts_codex_skips_model_input_and_encrypted_reasoning() -> None:
    developer = {"type": "response_item",
                 "payload": {"type": "message", "role": "developer",
                             "content": [{"type": "input_text", "text": "AGENTS.md rules"}]}}
    reasoning = {"type": "response_item",
                 "payload": {"type": "reasoning", "summary": [],
                             "encrypted_content": BLOB}}
    assistant = {"type": "response_item",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "reply"}]}}
    assert search._entry_texts(developer) == []
    assert search._entry_texts(reasoning) == []
    assert search._entry_texts(assistant) == ["reply"]


def test_first_text_match_skips_blob_lines(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    write_jsonl(f, [
        {"type": "assistant", "sessionId": "s",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "x", "signature": BLOB}]}},
        {"type": "user", "sessionId": "s",
         "message": {"role": "user", "content": "tell me about Wiz"}},
    ])
    m = search._Matcher("wiz")
    entry, text = search._first_text_match(str(f), m)
    assert text == "tell me about Wiz"
    assert entry["type"] == "user"
    # Nothing past the real hit, and nothing in a blob-only file.
    assert search._first_text_match(str(f), m, after_line=2) is None


def test_search_opencode_db_regex_query(tmp_search_dirs) -> None:
    """A regex query cannot use LIKE; it still matches on extracted text."""
    data_dir = tmp_search_dirs["opencode_data"]
    create_opencode_db(
        data_dir / "opencode.db",
        sessions=[{"id": "ses_db", "directory": "/Users/me/repo", "title": "t",
                   "time_created": 1, "time_updated": 2}],
        messages=[{"id": "msg_1", "session_id": "ses_db", "data": {"role": "user"}}],
        parts=[
            {"id": "prt_1", "message_id": "msg_1", "session_id": "ses_db",
             "data": {"type": "text", "text": "a setup wizard"}},
            {"id": "prt_2", "message_id": "msg_1", "session_id": "ses_db",
             "data": {"type": "text", "text": "we use Wiz daily"}},
        ],
    )
    results = search._search_opencode_db(r"\bwiz\b", data_dir, None)
    assert [r.session_id for r in results] == ["ses_db"]
    assert results[0].matched_line == "we use Wiz daily"


# ---------------------------------------------------------------------------
# search_sessions / rank_results
# ---------------------------------------------------------------------------


def _hit(session_id: str, provider: Provider = Provider.CLAUDE, **kw) -> SearchResult:
    data = dict(session_id=session_id, project_path="/repo", provider=provider,
                matched_line="... needle ...", file_path=f"/tmp/{session_id}.jsonl")
    data.update(kw)
    return SearchResult(**data)


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def test_search_sessions_joins_titles_and_ranks(monkeypatch) -> None:
    sessions = [
        make_session(id="old-content", summary="unrelated", timestamp=_ts(1)),
        make_session(id="new-content", summary="unrelated", timestamp=_ts(9)),
        make_session(id="both", summary="Needle hunt", timestamp=_ts(3)),
        make_session(id="title-only", summary="the NEEDLE plan", timestamp=_ts(5),
                     source_path="/tmp/title-only.jsonl"),
        make_session(id="no-match", summary="unrelated", timestamp=_ts(8)),
    ]
    monkeypatch.setattr(
        search, "ripgrep_search",
        lambda q, **_kw: [_hit("old-content"), _hit("both"), _hit("new-content")],
    )

    results = search.search_sessions("needle", sessions)

    # Title matches first (newest first), then content-only (newest first).
    assert [r.session_id for r in results] == [
        "title-only", "both", "new-content", "old-content",
    ]
    by_id = {r.session_id: r for r in results}
    assert by_id["title-only"].matched_in == ("title",)
    assert by_id["title-only"].matched_line == ""
    assert by_id["title-only"].file_path == "/tmp/title-only.jsonl"
    assert by_id["both"].matched_in == ("title", "content")
    assert by_id["both"].title == "Needle hunt"
    assert by_id["new-content"].matched_in == ("content",)
    assert by_id["new-content"].timestamp == _ts(9)


def test_search_sessions_titles_only_skips_ripgrep(monkeypatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("ripgrep_search must not run with titles_only")

    monkeypatch.setattr(search, "ripgrep_search", _boom)
    sessions = [
        make_session(id="a", summary="Wiz IDAM flashcards"),
        make_session(id="b", summary="setup wizard"),
    ]
    results = search.search_sessions(r"\bwiz\b", sessions, titles_only=True)
    assert [r.session_id for r in results] == ["a"]


def test_search_sessions_keys_on_provider_and_host(monkeypatch) -> None:
    """The same id under another provider or host is a different session."""
    sessions = [
        make_session(id="s", provider=Provider.CODEX, summary="needle codex"),
        make_session(id="s", provider=Provider.CLAUDE, summary="needle laptop",
                     host="laptop"),
    ]
    monkeypatch.setattr(
        search, "ripgrep_search",
        lambda q, **_kw: [_hit("s", Provider.CLAUDE, host="desktop")],
    )
    results = search.search_sessions("needle", sessions)
    assert sorted((r.provider.value, r.host, r.matched_in) for r in results) == sorted([
        ("claude", "desktop", ("content",)),
        ("codex", None, ("title",)),
        ("claude", "laptop", ("title",)),
    ])


def test_search_sessions_unindexed_hit_uses_file_mtime(monkeypatch, tmp_path) -> None:
    f = tmp_path / "loose.jsonl"
    f.write_text("{}\n")
    os.utime(f, (1_800_000_000, 1_800_000_000))
    monkeypatch.setattr(
        search, "ripgrep_search",
        lambda q, **_kw: [_hit("loose", file_path=str(f)), _hit("gone")],
    )
    results = search.search_sessions("needle", [])
    assert [r.session_id for r in results] == ["loose", "gone"]
    assert results[0].title is None
    assert results[0].timestamp == datetime.fromtimestamp(1_800_000_000, tz=timezone.utc)
    assert results[1].timestamp is None


def test_search_sessions_unverifiable_regex_matches_no_titles(monkeypatch) -> None:
    monkeypatch.setattr(search, "ripgrep_search", lambda q, **_kw: [])
    sessions = [make_session(id="a", summary="anything at all")]
    assert search.search_sessions(r"\p{L}+needle", sessions) == []
