"""Claude Code ``/branch`` sessions and the shared-root copy rule."""

from __future__ import annotations

from datetime import datetime, timezone

from sesh.providers import claude
from tests.helpers import write_jsonl

PROJECT = "/Users/me/repo"


def _project_dir(tmp_claude_dir):
    d = tmp_claude_dir / "projects" / claude.encode_claude_path(PROJECT)
    d.mkdir(parents=True)
    return d


def _user(sid, uuid, parent, ts, text, **extra):
    return {
        "type": "user",
        "sessionId": sid,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "cwd": PROJECT,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(sid, uuid, parent, ts, text, *, inp, out, **extra):
    return {
        "type": "assistant",
        "sessionId": sid,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "cwd": PROJECT,
        "message": {
            "role": "assistant",
            "model": "claude-opus",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": inp, "output_tokens": out},
        },
        **extra,
    }


def _original(sid="orig"):
    """Two turns: 100+10 then 200+20 tokens."""
    return [
        _user(sid, "u1", None, "2026-10-17T10:00:00Z", "First prompt"),
        _assistant(sid, "a1", "u1", "2026-10-17T10:00:05Z", "one", inp=100, out=10),
        _user(sid, "u2", "a1", "2026-10-17T10:01:00Z", "Second prompt"),
        _assistant(sid, "a2", "u2", "2026-10-17T10:01:05Z", "two", inp=200, out=20),
    ]


def _branch(source, sid="branch"):
    """Copy ``source`` the way ``/branch`` does, then add one own turn."""
    copied = []
    for rec in source:
        rec = dict(rec, sessionId=sid)
        rec["forkedFrom"] = {"sessionId": source[0]["sessionId"], "messageUuid": rec["uuid"]}
        copied.append(rec)
    return copied + [
        {"type": "custom-title", "customTitle": "First prompt (Branch)", "sessionId": sid},
        _user(sid, "u3", "a2", "2026-10-17T11:00:00Z", "Branch prompt"),
        _assistant(sid, "a3", "u3", "2026-10-17T11:00:05Z", "three", inp=300, out=30),
    ]


def _by_id(sessions):
    return {s.id: s for s in sessions}


def test_branch_and_original_are_both_listed(tmp_claude_dir) -> None:
    """A /branch copy shares the original's root uuid but neither is hidden."""
    d = _project_dir(tmp_claude_dir)
    original = _original()
    write_jsonl(d / "orig.jsonl", original)
    write_jsonl(d / "branch.jsonl", _branch(original))

    sessions = _by_id(claude.ClaudeProvider().get_sessions(PROJECT))

    assert set(sessions) == {"orig", "branch"}
    branch = sessions["branch"]
    assert branch.forked_from == "orig"
    assert branch.forked_at == datetime(2026, 10, 17, 10, 1, 5, tzinfo=timezone.utc)
    assert sessions["orig"].forked_from is None
    assert sessions["orig"].forked_at is None


def test_branch_usage_excludes_copied_turns(tmp_claude_dir) -> None:
    """Copied turns were paid for in the original; only the branch's own count."""
    d = _project_dir(tmp_claude_dir)
    original = _original()
    write_jsonl(d / "orig.jsonl", original)
    write_jsonl(d / "branch.jsonl", _branch(original))

    sessions = _by_id(claude.ClaudeProvider().get_sessions(PROJECT))

    assert sessions["orig"].cumulative_input_tokens == 300
    assert sessions["orig"].output_tokens == 30
    branch = sessions["branch"]
    assert branch.cumulative_input_tokens == 300
    assert branch.output_tokens == 30
    # Context size and transcript length still describe the whole branch.
    assert branch.input_tokens == 300
    assert branch.message_count == 6


def test_branch_listed_when_original_continues(tmp_claude_dir) -> None:
    """Both sides of a diverged branch keep their own titles and are listed."""
    d = _project_dir(tmp_claude_dir)
    original = _original()
    branch = _branch(original)
    original += [
        _user("orig", "u9", "a2", "2026-10-17T12:00:00Z", "Original goes on"),
        _assistant("orig", "a9", "u9", "2026-10-17T12:00:05Z", "nine", inp=1, out=1),
    ]
    write_jsonl(d / "orig.jsonl", original)
    write_jsonl(d / "branch.jsonl", branch)

    sessions = _by_id(claude.ClaudeProvider().get_sessions(PROJECT))

    assert set(sessions) == {"orig", "branch"}
    assert sessions["orig"].summary == "Original goes on"
    assert sessions["branch"].summary == "First prompt (Branch)"


def test_unmarked_subset_copy_is_merged_into_larger(tmp_claude_dir) -> None:
    """Without forkedFrom, a session wholly contained in another is hidden,
    even when it is the newer one."""
    d = _project_dir(tmp_claude_dir)
    full = _original("full")
    partial = [dict(r, sessionId="partial", timestamp="2026-10-18T00:00:00Z") for r in full[:2]]
    write_jsonl(d / "full.jsonl", full)
    write_jsonl(d / "partial.jsonl", partial)

    ids = {s.id for s in claude.ClaudeProvider().get_sessions(PROJECT)}

    assert ids == {"full"}


def test_unmarked_overlap_with_unique_records_lists_both(tmp_claude_dir) -> None:
    """Without forkedFrom, sessions sharing a root but each holding records the
    other lacks are both listed: nothing unique is ever hidden."""
    d = _project_dir(tmp_claude_dir)
    base = _original("a")
    other = [dict(r, sessionId="b") for r in base[:2]] + [
        _user("b", "ub", "a1", "2026-10-18T00:00:00Z", "Only in b"),
    ]
    write_jsonl(d / "a.jsonl", base)
    write_jsonl(d / "b.jsonl", other)

    ids = {s.id for s in claude.ClaudeProvider().get_sessions(PROJECT)}

    assert ids == {"a", "b"}


def test_title_precedence_custom_then_summary_then_ai(tmp_claude_dir) -> None:
    """custom-title beats a summary record, which beats ai-title, which beats
    the last prompt; the latest record of each kind wins."""
    d = _project_dir(tmp_claude_dir)

    def session(sid, *titles):
        return [
            _user(sid, f"{sid}-u", None, "2026-10-17T10:00:00Z", f"{sid} prompt"),
            *({**t, "sessionId": sid} for t in titles),
        ]

    write_jsonl(d / "s.jsonl", [
        *session(
            "custom",
            {"type": "ai-title", "aiTitle": "AI"},
            {"type": "summary", "summary": "Summary"},
            {"type": "custom-title", "customTitle": "Old name"},
            {"type": "custom-title", "customTitle": "New name"},
        ),
        *session(
            "summary",
            {"type": "ai-title", "aiTitle": "AI"},
            {"type": "summary", "summary": "Summary"},
        ),
        *session(
            "ai",
            {"type": "ai-title", "aiTitle": "First AI"},
            {"type": "ai-title", "aiTitle": "Latest AI"},
            {"type": "ai-title", "aiTitle": "   "},
        ),
        *session("plain"),
    ])

    titles = {s.id: s.summary for s in claude.ClaudeProvider().get_sessions(PROJECT)}

    assert titles == {
        "custom": "New name",
        "summary": "Summary",
        "ai": "Latest AI",
        "plain": "plain prompt",
    }


def test_deleting_original_leaves_branch_intact(tmp_claude_dir) -> None:
    """A branch holds full copies, so deleting its original removes nothing
    from it; the branch still names the (now missing) parent."""
    d = _project_dir(tmp_claude_dir)
    original = _original()
    write_jsonl(d / "orig.jsonl", original)
    write_jsonl(d / "branch.jsonl", _branch(original))
    provider = claude.ClaudeProvider()
    sessions = _by_id(provider.get_sessions(PROJECT))

    provider.delete_session(sessions["orig"])

    after = _by_id(provider.get_sessions(PROJECT))
    assert set(after) == {"branch"}
    assert after["branch"].forked_from == "orig"
    messages = provider.get_messages(after["branch"])
    assert [m.content for m in messages if m.role == "user"] == [
        "First prompt", "Second prompt", "Branch prompt",
    ]
