from __future__ import annotations

import json
from pathlib import Path

import pytest

from sesh import snapshots
from sesh.models import Provider
from sesh.snapshots import core as snapshots_core
from sesh.snapshots.backend import CapturedTab
from tests.helpers import make_snapshot, make_snapshot_resume, make_snapshot_tab


def test_to_dict_roundtrip() -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(
                window=1,
                tab=1,
                cwd="/tmp/proj",
                scrollback_tail="hello\nworld",
                resume=make_snapshot_resume(
                    session_id="abc",
                    cmd_args=["claude", "--resume", "abc"],
                ),
            ),
        ],
    )
    d = snap.to_dict()
    again = snapshots.Snapshot.from_dict(d)
    assert again.id == snap.id
    assert again.tabs[0].resume.session_id == "abc"
    assert again.tabs[0].resume.cmd_args == ["claude", "--resume", "abc"]


def test_save_and_load_roundtrip(tmp_snapshots_dir: Path) -> None:
    snap = make_snapshot(
        id="snapshot-20260424-153000",
        tabs=[make_snapshot_tab(cwd="/tmp/proj")],
    )
    path = snapshots.save(snap)
    assert path.is_file()

    loaded = snapshots.load(snap.id)
    assert loaded.id == snap.id
    assert len(loaded.tabs) == 1


def test_load_missing_raises(tmp_snapshots_dir: Path) -> None:
    with pytest.raises(snapshots.SnapshotsNotFoundError):
        snapshots.load("snapshot-doesnt-exist")


def test_load_unsupported_schema(tmp_snapshots_dir: Path) -> None:
    target = tmp_snapshots_dir / "snapshot-bad.json"
    target.write_text(json.dumps({"schema_version": 99, "id": "snapshot-bad", "created_at": "", "host": "", "tabs": []}))
    with pytest.raises(snapshots.SnapshotsSchemaError):
        snapshots.load("snapshot-bad")


def test_list_snapshots_newest_first(tmp_snapshots_dir: Path) -> None:
    older = make_snapshot(id="snapshot-20260424-100000", created_at="2026-04-24T10:00:00-04:00")
    newer = make_snapshot(id="snapshot-20260424-153000", created_at="2026-04-24T15:30:00-04:00")
    snapshots.save(older)
    snapshots.save(newer)

    summaries = snapshots.list_snapshots()
    assert [s.id for s in summaries] == [newer.id, older.id]


def test_list_snapshots_skips_bad_files(tmp_snapshots_dir: Path) -> None:
    snap = make_snapshot()
    snapshots.save(snap)
    (tmp_snapshots_dir / "snapshot-broken.json").write_text("{not valid json")
    summaries = snapshots.list_snapshots()
    assert [s.id for s in summaries] == [snap.id]


def test_delete(tmp_snapshots_dir: Path) -> None:
    snap = make_snapshot()
    snapshots.save(snap)
    snapshots.delete(snap.id)
    assert not (tmp_snapshots_dir / f"{snap.id}.json").exists()


def test_delete_missing_raises(tmp_snapshots_dir: Path) -> None:
    with pytest.raises(snapshots.SnapshotsNotFoundError):
        snapshots.delete("snapshot-nope")


def test_capture_unsupported_when_no_backend(monkeypatch, tmp_snapshots_dir: Path) -> None:
    monkeypatch.setattr(snapshots_core, "get_backend", lambda: None)
    with pytest.raises(snapshots.SnapshotsUnsupportedError):
        snapshots.capture()


def test_capture_uses_backend_and_resolves_explicit_resume(
    fake_backend, tmp_snapshots_dir: Path, monkeypatch
) -> None:
    fake_backend.captured_tabs = [
        CapturedTab(
            window=1,
            tab=1,
            tty="/dev/ttys001",
            cwd="/tmp/proj",
            scrollback_tail="$ claude --resume abc-123\nsome output",
        ),
    ]
    monkeypatch.setattr(snapshots_core, "_index_mtime_lookup", lambda: None)

    snap = snapshots.capture()

    assert len(snap.tabs) == 1
    tab = snap.tabs[0]
    assert tab.resume is not None
    assert tab.resume.provider == Provider.CLAUDE
    assert tab.resume.session_id == "abc-123"
    assert tab.resume.source == "explicit"
    assert tab.resume.cmd_args == ["claude", "--resume", "abc-123"]


def test_build_restore_plan_skips_plain_shell_by_default(tmp_snapshots_dir: Path, monkeypatch) -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(
                window=1,
                tab=1,
                cwd="/tmp/proj",
                resume=make_snapshot_resume(),
            ),
            make_snapshot_tab(window=1, tab=2, cwd="/tmp/proj", resume=None),
        ],
    )
    # Make CWDs and binaries appear to exist.
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    plan = snapshots.build_restore_plan(snap)

    assert len(plan.items) == 2
    assert plan.items[0].reason_skipped is None
    assert plan.items[1].reason_skipped == "plain shell"


def test_build_restore_plan_all_includes_shells(tmp_snapshots_dir: Path, monkeypatch) -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(window=1, tab=1, cwd="/tmp/proj", resume=None),
        ],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

    plan = snapshots.build_restore_plan(snap, include_shells=True)
    assert plan.items[0].reason_skipped is None
    assert plan.items[0].cmd_args is None  # plain shell


def test_build_restore_plan_marks_missing_cwd(tmp_snapshots_dir: Path, monkeypatch) -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(window=1, tab=1, cwd="/nonexistent/path", resume=make_snapshot_resume()),
        ],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    plan = snapshots.build_restore_plan(snap)
    assert plan.items[0].reason_skipped == "cwd missing: /nonexistent/path"


def test_build_restore_plan_marks_missing_binary(tmp_snapshots_dir: Path, monkeypatch) -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(window=1, tab=1, cwd="/tmp/proj", resume=make_snapshot_resume()),
        ],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: None)

    plan = snapshots.build_restore_plan(snap)
    assert plan.items[0].reason_skipped == "claude not on PATH"


def test_build_restore_plan_preserves_window_tab_order(tmp_snapshots_dir: Path, monkeypatch) -> None:
    snap = make_snapshot(
        tabs=[
            make_snapshot_tab(window=2, tab=1, resume=make_snapshot_resume()),
            make_snapshot_tab(window=1, tab=3, resume=make_snapshot_resume()),
            make_snapshot_tab(window=1, tab=1, resume=make_snapshot_resume()),
        ],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    plan = snapshots.build_restore_plan(snap)
    pairs = [(it.window, it.tab) for it in plan.items]
    assert pairs == [(2, 1), (1, 3), (1, 1)]


def test_restore_dry_run_returns_plan_without_calling_backend(
    fake_backend, tmp_snapshots_dir: Path, monkeypatch
) -> None:
    snap = make_snapshot(
        host="some-other-host",
        tabs=[make_snapshot_tab(window=1, tab=1, cwd="/tmp/proj", resume=make_snapshot_resume())],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    report = snapshots.restore(snap, dry_run=True)

    assert report.launched == 0
    assert report.fellback is False
    assert "dry-run" in (report.note or "")
    # Host mismatch should be true since "some-other-host" != real host.
    assert report.host_mismatch is True
    # Plan still includes the runnable tab.
    assert len(report.plan.items) == 1
    # Backend should not have been called.
    assert fake_backend.restore_calls == []


def test_restore_calls_backend_for_runnable_tabs(
    fake_backend, tmp_snapshots_dir: Path, monkeypatch
) -> None:
    snap = make_snapshot(
        host="",  # disable host_mismatch comparison for this test
        tabs=[
            make_snapshot_tab(window=1, tab=1, cwd="/tmp/proj", resume=make_snapshot_resume()),
            make_snapshot_tab(window=1, tab=2, cwd="/tmp/proj", resume=None),
        ],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    monkeypatch.setattr("sesh.snapshots.core.shutil.which", lambda _: "/usr/bin/claude")

    report = snapshots.restore(snap)
    # Only one runnable item — the plain shell is skipped by default.
    assert len(fake_backend.restore_calls) == 1
    assert len(fake_backend.restore_calls[0]) == 1
    assert report.launched == 1


def test_restore_returns_no_runnable_when_all_skipped(
    fake_backend, tmp_snapshots_dir: Path, monkeypatch
) -> None:
    snap = make_snapshot(
        tabs=[make_snapshot_tab(window=1, tab=1, cwd="/tmp/proj", resume=None)],
    )
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

    report = snapshots.restore(snap)  # plain shell skipped by default
    assert report.launched == 0
    assert fake_backend.restore_calls == []
