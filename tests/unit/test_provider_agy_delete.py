"""Deletion for the Antigravity CLI (agy) provider."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sesh.models import Provider
from sesh.providers.agy import AgyProvider, pb_fields
from tests.helpers import make_session, write_agy_conversation, write_jsonl

CONV = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
GRANDCHILD = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"


def _session(conversation_id: str = CONV):
    return make_session(id=conversation_id, provider=Provider.AGY)


def _index_ids(data_dir: Path, table: str = "conversation_summaries") -> set[str]:
    conn = sqlite3.connect(data_dir / "conversation_summaries.db")
    try:
        return {row[0] for row in conn.execute(f"SELECT conversation_id FROM {table}")}
    finally:
        conn.close()


def _jetbox_ids(data_dir: Path) -> list[str]:
    data = (data_dir / "jetbox_summaries_proto.pb").read_bytes()
    return [
        pb_fields(value)[0][4].decode()
        for _f, _w, _s, _e, value in pb_fields(data)
    ]


def _write_side_files(data_dir: Path, ids: list[str]) -> None:
    for sub, suffix in (("presence", ".lock"), ("annotations", ".pbtxt")):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
        for conversation_id in ids:
            (data_dir / sub / f"{conversation_id}{suffix}").write_text("")
    for conversation_id in ids:
        db = data_dir / "conversations" / f"{conversation_id}.db"
        Path(f"{db}-wal").write_text("")
        Path(f"{db}-shm").write_text("")
    cache = data_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "last_conversations.json").write_text(json.dumps(
        {f"/Users/me/{i}": conversation_id for i, conversation_id in enumerate(ids)}
    ))
    (cache / "conversation_metadata.json").write_text(json.dumps(
        {"conversations": {c: {"summary": {"ID": c}} for c in ids}}
    ))


def test_delete_removes_every_trace_of_the_conversation(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER, workspace="/Users/me/other")
    _write_side_files(tmp_agy_dir, [CONV, OTHER])

    AgyProvider().delete_session(_session())

    assert not (tmp_agy_dir / "brain" / CONV).exists()
    assert (tmp_agy_dir / "brain" / OTHER).is_dir()
    assert sorted(p.name for p in (tmp_agy_dir / "conversations").iterdir()) == [
        f"{OTHER}.db", f"{OTHER}.db-shm", f"{OTHER}.db-wal",
    ]
    assert [p.name for p in (tmp_agy_dir / "presence").iterdir()] == [f"{OTHER}.lock"]
    assert [p.name for p in (tmp_agy_dir / "annotations").iterdir()] == [f"{OTHER}.pbtxt"]
    assert _index_ids(tmp_agy_dir) == {OTHER}
    assert _index_ids(tmp_agy_dir, "conversations_fts") == {OTHER}
    assert _jetbox_ids(tmp_agy_dir) == [OTHER]
    assert json.loads((tmp_agy_dir / "cache" / "last_conversations.json").read_text()) == {
        "/Users/me/1": OTHER,
    }
    metadata = json.loads((tmp_agy_dir / "cache" / "conversation_metadata.json").read_text())
    assert list(metadata["conversations"]) == [OTHER]


def test_delete_takes_nested_subagents_with_the_root(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)
    write_agy_conversation(tmp_agy_dir, conversation_id=CHILD, parent_id=CONV)
    write_agy_conversation(tmp_agy_dir, conversation_id=GRANDCHILD, parent_id=CHILD)
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER)

    AgyProvider().delete_session(_session())

    assert [p.name for p in (tmp_agy_dir / "brain").iterdir()] == [OTHER]
    assert [p.name for p in (tmp_agy_dir / "conversations").iterdir()] == [f"{OTHER}.db"]
    assert _index_ids(tmp_agy_dir) == {OTHER}
    assert _jetbox_ids(tmp_agy_dir) == [OTHER]


def test_delete_leaves_other_jetbox_bytes_untouched(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER)
    before = (tmp_agy_dir / "jetbox_summaries_proto.pb").read_bytes()
    write_agy_conversation(tmp_agy_dir)

    AgyProvider().delete_session(_session())

    assert (tmp_agy_dir / "jetbox_summaries_proto.pb").read_bytes() == before


def test_delete_works_without_index_jetbox_or_caches(tmp_agy_dir) -> None:
    write_agy_conversation(
        tmp_agy_dir, with_index_row=False, with_jetbox_entry=False,
    )

    AgyProvider().delete_session(_session())

    assert not (tmp_agy_dir / "brain" / CONV).exists()
    assert not (tmp_agy_dir / "conversations" / f"{CONV}.db").exists()


def test_delete_tolerates_an_index_without_a_search_table(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)
    conn = sqlite3.connect(tmp_agy_dir / "conversation_summaries.db")
    conn.execute("DROP TABLE conversations_fts")
    conn.commit()
    conn.close()

    AgyProvider().delete_session(_session())

    assert _index_ids(tmp_agy_dir) == set()


def test_delete_rejects_an_unsafe_id(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)

    with pytest.raises(ValueError):
        AgyProvider().delete_session(_session("../../etc"))

    assert (tmp_agy_dir / "brain" / CONV).is_dir()


def test_failed_file_removal_leaves_the_index_alone(tmp_agy_dir, monkeypatch) -> None:
    """A partial delete must not edit agy's index."""
    from sesh.providers import agy

    write_agy_conversation(tmp_agy_dir)

    def boom(_path):
        raise OSError("busy")

    monkeypatch.setattr(agy.shutil, "rmtree", boom)

    with pytest.raises(OSError):
        AgyProvider().delete_session(_session())

    assert _index_ids(tmp_agy_dir) == {CONV}
    assert _jetbox_ids(tmp_agy_dir) == [CONV]


def test_unusable_index_raises_after_the_files_are_gone(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, with_index_row=False)
    (tmp_agy_dir / "conversation_summaries.db").write_text("this is not sqlite")

    with pytest.raises(RuntimeError, match="agy's index"):
        AgyProvider().delete_session(_session())

    assert not (tmp_agy_dir / "brain" / CONV).exists()


def test_deleted_session_is_no_longer_discovered(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)
    provider = AgyProvider()
    assert provider.get_sessions("/Users/me/repo")

    provider.delete_session(_session())

    assert list(provider.discover_projects()) == []


def test_delete_history_removes_only_attributed_entries(tmp_agy_dir) -> None:
    history = tmp_agy_dir / "history.jsonl"
    write_jsonl(history, [
        {"display": "first prompt", "timestamp": 1, "workspace": "/Users/me/repo"},
        {"display": "follow up", "timestamp": 2, "workspace": "/Users/me/repo",
         "conversationId": CONV},
        {"display": "/resume", "timestamp": 3, "workspace": "/Users/me/repo",
         "conversationId": CONV, "type": "slash_command"},
        {"display": "elsewhere", "timestamp": 4, "workspace": "/Users/me/other",
         "conversationId": OTHER},
    ])
    provider = AgyProvider()

    preview = provider.delete_history(_session(), dry_run=True)
    assert preview.supported and preview.removed == {str(history): 2}
    assert len(history.read_text().splitlines()) == 4

    report = provider.delete_history(_session())
    assert report.removed == {str(history): 2}
    kept = [json.loads(line)["display"] for line in history.read_text().splitlines()]
    assert kept == ["first prompt", "elsewhere"]


def test_delete_history_without_a_history_file(tmp_agy_dir) -> None:
    report = AgyProvider().delete_history(_session())
    assert report.supported and report.removed == {}
