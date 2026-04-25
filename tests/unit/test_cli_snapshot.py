from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from sesh import cli, snapshots
from tests.helpers import make_snapshot, make_snapshot_resume, make_snapshot_tab


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def test_snapshot_save_unsupported_exits(
    tmp_snapshots_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from sesh.snapshots import core as snapshots_core

    monkeypatch.setattr(snapshots_core, "get_backend", lambda: None)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_snapshot_save(_ns())
    assert exc.value.code == 1
    assert "macOS-only" in capsys.readouterr().err


def test_snapshot_save_writes_file_and_prints_summary(
    tmp_snapshots_dir: Path, fake_backend, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from sesh.snapshots.backend import CapturedTab
    from sesh.snapshots import core as snapshots_core

    fake_backend.captured_tabs = [
        CapturedTab(
            window=1,
            tab=1,
            tty="/dev/ttys001",
            cwd="/tmp/proj",
            scrollback_tail="$ claude --resume abc",
        ),
    ]
    monkeypatch.setattr(snapshots_core, "_index_mtime_lookup", lambda: None)

    cli.cmd_snapshot_save(_ns())

    out = json.loads(capsys.readouterr().out)
    assert out["tab_count"] == 1
    assert out["resumable"] == 1
    assert out["id"].startswith("snapshot-")
    assert Path(out["path"]).is_file()


def test_snapshot_list_outputs_summaries(tmp_snapshots_dir: Path, capsys) -> None:
    snap = make_snapshot(tabs=[make_snapshot_tab(resume=make_snapshot_resume())])
    snapshots.save(snap)

    cli.cmd_snapshot_list(_ns())
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert out[0]["id"] == snap.id
    assert out[0]["resumable_count"] == 1


def test_snapshot_show_outputs_full_snapshot(tmp_snapshots_dir: Path, capsys) -> None:
    snap = make_snapshot(tabs=[make_snapshot_tab(resume=make_snapshot_resume())])
    snapshots.save(snap)

    cli.cmd_snapshot_show(_ns(snapshot_id=snap.id))
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == snap.id
    assert out["tabs"][0]["resume"]["session_id"] == "abc-123"


def test_snapshot_show_missing_exits(tmp_snapshots_dir: Path, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.cmd_snapshot_show(_ns(snapshot_id="snapshot-doesnt-exist"))
    assert "not found" in capsys.readouterr().err


def test_snapshot_reopen_dry_run_outputs_plan(
    tmp_snapshots_dir: Path, fake_backend, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    snap = make_snapshot(
        tabs=[make_snapshot_tab(cwd="/tmp/proj", resume=make_snapshot_resume())],
    )
    snapshots.save(snap)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    cli.cmd_snapshot_reopen(_ns(snapshot_id=snap.id, all=False, dry_run=True))

    out = json.loads(capsys.readouterr().out)
    assert out["launched"] == 0
    assert out["snapshot_id"] == snap.id
    assert "dry-run" in (out["note"] or "")
    assert fake_backend.restore_calls == []


def test_snapshot_reopen_calls_backend(
    tmp_snapshots_dir: Path, fake_backend, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    snap = make_snapshot(
        host="",
        tabs=[make_snapshot_tab(cwd="/tmp/proj", resume=make_snapshot_resume())],
    )
    snapshots.save(snap)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    cli.cmd_snapshot_reopen(_ns(snapshot_id=snap.id, all=False, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["launched"] == 1
    assert len(fake_backend.restore_calls) == 1


def test_snapshot_delete_dry_run(tmp_snapshots_dir: Path, capsys) -> None:
    snap = make_snapshot()
    snapshots.save(snap)

    cli.cmd_snapshot_delete(_ns(snapshot_id=snap.id, force=False, dry_run=True))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["would_delete"]["id"] == snap.id
    # File still exists
    assert (tmp_snapshots_dir / f"{snap.id}.json").is_file()


def test_snapshot_delete_force(tmp_snapshots_dir: Path, capsys) -> None:
    snap = make_snapshot()
    snapshots.save(snap)

    cli.cmd_snapshot_delete(_ns(snapshot_id=snap.id, force=True, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["id"] == snap.id
    assert not (tmp_snapshots_dir / f"{snap.id}.json").exists()


def test_snapshot_delete_missing_exits(tmp_snapshots_dir: Path, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.cmd_snapshot_delete(_ns(snapshot_id="snapshot-nope", force=True, dry_run=False))
    assert "not found" in capsys.readouterr().err
