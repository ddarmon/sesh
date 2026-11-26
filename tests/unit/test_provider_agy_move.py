"""Project move for the Antigravity CLI (agy) provider."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sesh.providers.agy import AgyProvider, pb_fields, uri_to_path
from tests.helpers import agy_trajectory_blob, write_agy_conversation, write_jsonl

CONV = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
OTHER = "44444444-4444-4444-8444-444444444444"

OLD = "/Users/me/old repo"
NEW = "/Users/me/new place/repo é"


def _blob(data_dir: Path, conversation_id: str) -> bytes:
    conn = sqlite3.connect(data_dir / "conversations" / f"{conversation_id}.db")
    try:
        return conn.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
    finally:
        conn.close()


def _index_row(data_dir: Path, conversation_id: str) -> tuple[str, bytes]:
    conn = sqlite3.connect(data_dir / "conversation_summaries.db")
    try:
        return conn.execute(
            "SELECT workspace_uris, raw_summary FROM conversation_summaries"
            " WHERE conversation_id = ?", (conversation_id,),
        ).fetchone()
    finally:
        conn.close()


def _uris(buf: bytes) -> list[str]:
    """Every ``file://`` leaf in a nested message, decoded to a path."""
    out: list[str] = []
    try:
        fields = pb_fields(buf)
    except ValueError:
        return out
    for _f, wire_type, _s, _e, value in fields:
        if wire_type != 2:
            continue
        if value.startswith(b"file://"):
            out.append(uri_to_path(value.decode()))
        else:
            out.extend(_uris(value))
    return out


def test_move_rewrites_every_workspace_record(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace=OLD)
    write_agy_conversation(tmp_agy_dir, conversation_id=CHILD, parent_id=CONV, workspace=OLD)
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER, workspace="/Users/me/other")
    other_blob = _blob(tmp_agy_dir, OTHER)
    cache = tmp_agy_dir / "cache"
    cache.mkdir()
    (cache / "last_conversations.json").write_text(json.dumps(
        {OLD: CONV, "/Users/me/other": OTHER}
    ))
    (cache / "conversation_metadata.json").write_text(json.dumps({"conversations": {
        CONV: {"summary": {"ID": CONV, "WorkspaceURIs": ["file:///Users/me/old%20repo"]}},
        OTHER: {"summary": {"ID": OTHER, "WorkspaceURIs": ["file:///Users/me/other"]}},
    }}))
    history = tmp_agy_dir / "history.jsonl"
    write_jsonl(history, [
        {"display": "héllo", "timestamp": 1, "workspace": OLD, "conversationId": CONV},
        {"display": "x", "timestamp": 2, "workspace": "/Users/me/other"},
    ])

    provider = AgyProvider()
    assert provider.move_candidates(OLD) == 2
    report = provider.move_project(OLD, NEW)

    assert report.success and report.error is None
    # 2 conversation dbs + index + jetbox + 1 sidecar + 2 cache files + history
    assert report.files_modified == 8

    for conversation_id in (CONV, CHILD):
        assert _uris(_blob(tmp_agy_dir, conversation_id)) == [NEW, NEW]
        text, raw = _index_row(tmp_agy_dir, conversation_id)
        assert [uri_to_path(u) for u in json.loads(text)] == [NEW]
        assert _uris(raw) == [NEW, NEW, NEW]
    # Embedded paths outside the workspace fields are deliberately left alone.
    assert f"read_file({OLD})".encode() in _blob(tmp_agy_dir, CONV)

    assert _blob(tmp_agy_dir, OTHER) == other_blob
    assert json.loads(_index_row(tmp_agy_dir, OTHER)[0]) == ["file:///Users/me/other"]

    jetbox = (tmp_agy_dir / "jetbox_summaries_proto.pb").read_bytes()
    assert sorted(set(_uris(jetbox))) == sorted({NEW, "/Users/me/other"})

    sidecar = json.loads((
        tmp_agy_dir / "brain" / CONV / ".system_generated" / "subagents" / f"{CHILD}.json"
    ).read_text())
    assert [uri_to_path(u) for u in sidecar["workspaceUris"]] == [NEW]
    assert sidecar["conversationId"] == CHILD

    assert json.loads((cache / "last_conversations.json").read_text()) == {
        NEW: CONV, "/Users/me/other": OTHER,
    }
    metadata = json.loads((cache / "conversation_metadata.json").read_text())
    assert [uri_to_path(u) for u in metadata["conversations"][CONV]["summary"]["WorkspaceURIs"]] == [NEW]
    assert metadata["conversations"][OTHER]["summary"]["WorkspaceURIs"] == ["file:///Users/me/other"]

    lines = [json.loads(line) for line in history.read_text().splitlines()]
    assert [line["workspace"] for line in lines] == [NEW, "/Users/me/other"]
    assert lines[0]["display"] == "héllo" and lines[0]["conversationId"] == CONV

    # Discovery now reports the new location.
    assert list(AgyProvider().discover_projects()) == [
        (NEW, "repo é"), ("/Users/me/other", "other"),
    ]


def test_move_keeps_the_blob_a_well_formed_message(tmp_agy_dir) -> None:
    """Lengths of every enclosing message are re-encoded around the new URI."""
    write_agy_conversation(tmp_agy_dir, workspace=OLD)

    AgyProvider().move_project(OLD, "/x" * 200)

    # Byte-identical to a blob agy would have written for the new workspace:
    # only the URIs differ, the free text still quotes the old path.
    assert _blob(tmp_agy_dir, CONV) == agy_trajectory_blob(
        CONV, "/x" * 200, embedded=OLD
    )


def test_move_rewrites_subdirectory_workspaces(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace=OLD + "/packages/app")

    AgyProvider().move_project(OLD, NEW)

    assert list(AgyProvider().discover_projects()) == [(NEW + "/packages/app", "app")]


def test_move_ignores_prefix_siblings_and_case_variants(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace="/Users/me/old repo2")
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER, workspace="/Users/me/Old Repo")
    blobs = [_blob(tmp_agy_dir, CONV), _blob(tmp_agy_dir, OTHER)]

    provider = AgyProvider()
    assert provider.move_candidates(OLD) == 0
    report = provider.move_project(OLD, NEW)

    assert report.success and report.files_modified == 0
    assert [_blob(tmp_agy_dir, CONV), _blob(tmp_agy_dir, OTHER)] == blobs


def test_move_does_not_displace_a_conversation_already_at_the_new_path(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace=OLD)
    cache = tmp_agy_dir / "cache"
    cache.mkdir()
    (cache / "last_conversations.json").write_text(json.dumps({OLD: CONV, NEW: OTHER}))

    AgyProvider().move_project(OLD, NEW)

    assert json.loads((cache / "last_conversations.json").read_text()) == {NEW: OTHER}


def test_move_with_no_store_is_a_successful_no_op(tmp_agy_dir) -> None:
    report = AgyProvider().move_project(OLD, NEW)
    assert report.success and report.files_modified == 0


def test_move_reports_a_corrupt_index_as_a_failure(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace=OLD, with_index_row=False)
    (tmp_agy_dir / "conversation_summaries.db").write_text("this is not sqlite")

    report = AgyProvider().move_project(OLD, NEW)

    assert report.success is False
    assert "agy metadata" in report.error
    assert report.files_modified == 1  # the conversation db was already done
