from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from sesh import prompt_history
from sesh.prompt_history import (
    HistoryWriteError,
    remove_json_array_entries,
    remove_jsonl_entries,
)


def _drop_s1(entry: dict) -> bool:
    return entry.get("sid") == "s1"


def test_jsonl_removes_matching_and_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"sid": "s1", "text": "a"}\n'
        '{"sid": "s2", "text": "b"}\n'
        "not json\n"
        '["a list"]\n'
        "\n"
        '{"sid": "s1", "text": "c"}\n'
        '{"sid": "s2", "text": "d"}'
    )
    assert remove_jsonl_entries(path, _drop_s1) == 2
    assert path.read_text() == (
        '{"sid": "s2", "text": "b"}\n'
        "not json\n"
        '["a list"]\n'
        "\n"
        '{"sid": "s2", "text": "d"}'
    )


def test_jsonl_dry_run_reports_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    original = '{"sid": "s1"}\n{"sid": "s2"}\n'
    path.write_text(original)
    assert remove_jsonl_entries(path, _drop_s1, dry_run=True) == 1
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]


def test_jsonl_missing_file_is_zero(tmp_path: Path) -> None:
    assert remove_jsonl_entries(tmp_path / "absent.jsonl", _drop_s1) == 0


def test_jsonl_no_match_leaves_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"sid": "s2"}\n')
    before = path.stat().st_mtime_ns
    assert remove_jsonl_entries(path, _drop_s1) == 0
    assert path.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [path]


def test_jsonl_preserves_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"sid": "s1"}\n{"sid": "s2"}\n')
    os.chmod(path, 0o600)
    remove_jsonl_entries(path, _drop_s1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_jsonl_preserves_non_utf8_bytes(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"sid": "s1"}\n{"sid": "s2", "t": "\xff"}\r\n')
    assert remove_jsonl_entries(path, _drop_s1) == 1
    assert path.read_bytes() == b'{"sid": "s2", "t": "\xff"}\r\n'


def test_jsonl_retries_when_appended_mid_rewrite(tmp_path: Path) -> None:
    """A line appended during the rewrite survives: the pass starts over."""
    path = tmp_path / "history.jsonl"
    path.write_text('{"sid": "s1"}\n{"sid": "s2"}\n')
    passes = []

    def drop(entry: dict) -> bool:
        if entry.get("sid") == "s1" and not passes:
            passes.append(1)
            with open(path, "a") as f:
                f.write('{"sid": "s3", "late": true}\n')
        return entry.get("sid") == "s1"

    assert remove_jsonl_entries(path, drop) == 1
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{"sid": "s2"}, {"sid": "s3", "late": True}]
    assert list(tmp_path.iterdir()) == [path]


def test_jsonl_gives_up_when_file_never_settles(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "history.jsonl"
    original = '{"sid": "s1"}\n'
    path.write_text(original)
    monkeypatch.setattr(prompt_history, "ATTEMPTS", 2)

    def drop(entry: dict) -> bool:
        with open(path, "a") as f:
            f.write("\n")
        return True

    with pytest.raises(HistoryWriteError):
        remove_jsonl_entries(path, drop)
    assert path.read_text().startswith(original)
    assert list(tmp_path.iterdir()) == [path]


def test_json_array_removes_matching(tmp_path: Path) -> None:
    path = tmp_path / "logs.json"
    path.write_text(json.dumps([{"sid": "s1"}, {"sid": "s2", "m": "é"}, "x"]))
    assert remove_json_array_entries(path, _drop_s1) == 1
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == [{"sid": "s2", "m": "é"}, "x"]
    assert "é" in text
    assert text.startswith("[\n  {")


def test_json_array_non_array_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "logs.json"
    path.write_text('{"sid": "s1"}')
    assert remove_json_array_entries(path, _drop_s1) == 0
    assert path.read_text() == '{"sid": "s1"}'


def test_json_array_invalid_json_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "logs.json"
    path.write_text("[{")
    assert remove_json_array_entries(path, _drop_s1) == 0
    assert path.read_text() == "[{"
