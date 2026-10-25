from __future__ import annotations

import json
from pathlib import Path

from sesh.models import Provider
from sesh.providers.claude import ClaudeProvider
from tests.helpers import make_session, write_jsonl


def _entry(session_id: str | None, text: str, hashes: tuple[str, ...] = ()) -> dict:
    entry = {
        "display": text,
        "pastedContents": {
            str(i + 1): {"id": i + 1, "type": "text", "contentHash": h}
            for i, h in enumerate(hashes)
        },
        "timestamp": 1792949776773,
        "project": "/repo",
    }
    if session_id is not None:
        entry["sessionId"] = session_id
    return entry


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_removes_only_the_sessions_entries(tmp_claude_dir: Path) -> None:
    history = tmp_claude_dir / "history.jsonl"
    write_jsonl(history, [
        _entry("s1", "one"),
        _entry("s2", "two"),
        _entry(None, "legacy, no sessionId"),
        _entry("s1", "three"),
    ])
    report = ClaudeProvider().delete_history(make_session(id="s1", provider=Provider.CLAUDE))

    assert report.supported
    assert report.removed == {str(history): 2}
    assert [e["display"] for e in _read(history)] == ["two", "legacy, no sessionId"]


def test_unreferenced_paste_files_removed_shared_kept(tmp_claude_dir: Path) -> None:
    history = tmp_claude_dir / "history.jsonl"
    paste_dir = tmp_claude_dir / "paste-cache"
    paste_dir.mkdir(parents=True)
    for h in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"):
        (paste_dir / f"{h}.txt").write_text(h)
    write_jsonl(history, [
        _entry("s1", "own paste", ("aaaaaaaaaaaaaaaa",)),
        _entry("s1", "shared paste", ("bbbbbbbbbbbbbbbb",)),
        _entry("s2", "other session", ("bbbbbbbbbbbbbbbb", "cccccccccccccccc")),
        # Hash already pruned by Claude Code: nothing to remove, no error.
        _entry("s1", "pruned paste", ("dddddddddddddddd",)),
    ])
    report = ClaudeProvider().delete_history(make_session(id="s1", provider=Provider.CLAUDE))

    assert report.files_removed == [str(paste_dir / "aaaaaaaaaaaaaaaa.txt")]
    assert sorted(p.name for p in paste_dir.iterdir()) == [
        "bbbbbbbbbbbbbbbb.txt",
        "cccccccccccccccc.txt",
    ]


def test_unsafe_paste_hash_is_ignored(tmp_claude_dir: Path) -> None:
    history = tmp_claude_dir / "history.jsonl"
    victim = tmp_claude_dir / "victim.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("keep me")
    write_jsonl(history, [_entry("s1", "x", ("../victim",))])

    report = ClaudeProvider().delete_history(make_session(id="s1", provider=Provider.CLAUDE))

    assert report.removed == {str(history): 1}
    assert report.files_removed == []
    assert victim.exists()


def test_dry_run_changes_nothing(tmp_claude_dir: Path) -> None:
    history = tmp_claude_dir / "history.jsonl"
    paste_dir = tmp_claude_dir / "paste-cache"
    paste_dir.mkdir(parents=True)
    paste = paste_dir / "aaaaaaaaaaaaaaaa.txt"
    paste.write_text("p")
    write_jsonl(history, [_entry("s1", "x", ("aaaaaaaaaaaaaaaa",)), _entry("s2", "y")])
    original = history.read_text()

    report = ClaudeProvider().delete_history(
        make_session(id="s1", provider=Provider.CLAUDE), dry_run=True
    )

    assert report.removed == {str(history): 1}
    assert report.files_removed == [str(paste)]
    assert history.read_text() == original
    assert paste.exists()


def test_no_history_file(tmp_claude_dir: Path) -> None:
    report = ClaudeProvider().delete_history(make_session(id="s1", provider=Provider.CLAUDE))
    assert report.supported
    assert report.removed == {}


def test_aggregation_base_dir(tmp_path: Path) -> None:
    host_home = tmp_path / "laptop"
    history = host_home / ".claude" / "history.jsonl"
    write_jsonl(history, [_entry("s1", "x"), _entry("s2", "y")])
    report = ClaudeProvider(base_dir=host_home, host="laptop").delete_history(
        make_session(id="s1", provider=Provider.CLAUDE), dry_run=True
    )
    assert report.removed == {str(history): 1}
