from __future__ import annotations

import json

from sesh.models import Provider
from sesh.providers import codex
from tests.helpers import write_jsonl


def test_rewrite_codex_jsonl_new_format_updates_session_meta(tmp_path) -> None:
    """New-format Codex JSONL: payload.cwd is updated to the new path."""
    file_path = tmp_path / "new.jsonl"
    write_jsonl(
        file_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "s1", "cwd": "/old"},
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
        ],
    )

    changed = codex._rewrite_codex_jsonl(file_path, "/old", "/new")
    assert changed is True
    first = json.loads(file_path.read_text().splitlines()[0])
    assert first["payload"]["cwd"] == "/new"


def test_rewrite_codex_jsonl_legacy_replaces_cwd_tags(tmp_path) -> None:
    """Legacy Codex JSONL: <cwd>old</cwd> tags are replaced with <cwd>new</cwd>."""
    file_path = tmp_path / "legacy.jsonl"
    file_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "payload": {
                            "content": [{"text": "prefix <cwd>/old</cwd> suffix"}]
                        }
                    }
                ),
                "raw line <cwd>/old</cwd>",
            ]
        )
        + "\n"
    )

    changed = codex._rewrite_codex_jsonl(file_path, "/old", "/new")
    assert changed is True
    text = file_path.read_text()
    assert "<cwd>/new</cwd>" in text
    assert "<cwd>/old</cwd>" not in text


def test_rewrite_codex_jsonl_no_change_returns_false(tmp_path) -> None:
    """When no cwd references match, the file is untouched and False is returned."""
    file_path = tmp_path / "noop.jsonl"
    write_jsonl(file_path, [{"type": "event_msg", "payload": {"type": "user_message"}}])
    assert codex._rewrite_codex_jsonl(file_path, "/old", "/new") is False


def test_move_project_counts_modified_files_and_clears_index(tmp_codex_dir) -> None:
    """Move counts only JSONL files that were actually modified and clears the in-memory index."""
    needs_change = tmp_codex_dir / "a.jsonl"
    no_change = tmp_codex_dir / "b.jsonl"
    write_jsonl(
        needs_change,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "s1", "cwd": "/old"},
            }
        ],
    )
    write_jsonl(no_change, [{"type": "event_msg", "payload": {"type": "user_message"}}])

    provider = codex.CodexProvider()
    provider._index = {"stale": []}
    report = provider.move_project("/old", "/new")

    assert report.provider is Provider.CODEX
    assert report.success is True
    assert report.files_modified == 1
    assert provider._index is None
    first = json.loads(needs_change.read_text().splitlines()[0])
    assert first["payload"]["cwd"] == "/new"


def test_move_project_missing_dir_is_success(tmp_codex_dir) -> None:
    """When the Codex sessions dir doesn't exist, move succeeds with zero changes."""
    report = codex.CodexProvider().move_project("/old", "/new")
    assert report.success is True
    assert report.provider is Provider.CODEX
    assert report.files_modified == 0
