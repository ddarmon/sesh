from __future__ import annotations

import json
from pathlib import Path

from sesh.models import Provider
from sesh.providers.codex import CodexProvider
from tests.helpers import make_session, write_jsonl


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_delete_session_always_cleans_session_index(tmp_codex_dir: Path) -> None:
    codex_home = tmp_codex_dir.parent
    rollout = tmp_codex_dir / "2026" / "10" / "25" / "rollout-s1.jsonl"
    write_jsonl(rollout, [{"type": "session_meta", "payload": {"id": "s1"}}])
    index = codex_home / "session_index.jsonl"
    write_jsonl(index, [
        {"id": "s1", "thread_name": "doomed", "updated_at": "2026-10-25T00:00:00Z"},
        {"id": "s2", "thread_name": "kept", "updated_at": "2026-10-25T00:00:00Z"},
    ])
    history = codex_home / "history.jsonl"
    write_jsonl(history, [{"session_id": "s1", "ts": 1, "text": "hi"}])

    CodexProvider().delete_session(
        make_session(id="s1", provider=Provider.CODEX, source_path=str(rollout))
    )

    assert not rollout.exists()
    assert [e["id"] for e in _read(index)] == ["s2"]
    # Prompt history is only touched by delete_history.
    assert len(_read(history)) == 1


def test_delete_history_removes_session_prompts(tmp_codex_dir: Path) -> None:
    history = tmp_codex_dir.parent / "history.jsonl"
    write_jsonl(history, [
        {"session_id": "s1", "ts": 1, "text": "a"},
        {"session_id": "s2", "ts": 2, "text": "b"},
        {"session_id": "s1", "ts": 3, "text": "c"},
    ])
    session = make_session(id="s1", provider=Provider.CODEX)

    dry = CodexProvider().delete_history(session, dry_run=True)
    assert dry.removed == {str(history): 2}
    assert len(_read(history)) == 3

    report = CodexProvider().delete_history(session)
    assert report.supported
    assert report.removed == {str(history): 2}
    assert [e["text"] for e in _read(history)] == ["b"]


def test_delete_history_without_files(tmp_codex_dir: Path) -> None:
    report = CodexProvider().delete_history(make_session(id="s1", provider=Provider.CODEX))
    assert report.supported
    assert report.removed == {}
