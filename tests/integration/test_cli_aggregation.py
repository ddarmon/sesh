"""End-to-end CLI tests for aggregation mode.

These exercise `sesh --aggregation-root <tree> ...` against a synthetic
two-host aggregation tree built from real on-disk JSONL fixtures.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from sesh import cli
from sesh.models import encode_claude_path
from tests.helpers import write_jsonl


def _seed_claude_session(host_root: Path, project_path: str, session_id: str) -> None:
    encoded = encode_claude_path(project_path)
    project_dir = host_root / ".claude" / "projects" / encoded
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [
            {
                "sessionId": session_id,
                "cwd": project_path,
                "timestamp": "2025-01-01T00:00:00Z",
                "type": "summary",
                "summary": f"Session {session_id}",
            },
            {
                "sessionId": session_id,
                "cwd": project_path,
                "timestamp": "2025-01-01T00:00:01Z",
                "uuid": "u1",
                "parentUuid": None,
                "message": {"role": "user", "content": "hello"},
            },
        ],
    )


@pytest.fixture()
def tmp_aggregation_root(tmp_path: Path) -> Path:
    """Build a two-host aggregation tree under tmp_path/agg."""
    agg = tmp_path / "agg"
    _seed_claude_session(agg / "laptop", "/Users/me/proj-a", "sess-laptop")
    _seed_claude_session(agg / "desktop", "/Users/me/proj-b", "sess-desktop")
    return agg


def _ns(**kwargs) -> "object":
    """Build an argparse-like Namespace with the given attrs and aggregation_root."""
    import argparse

    defaults = {"aggregation_root": None, "provider": None, "project": None}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_sessions_includes_host_field(
    monkeypatch, capsys, tmp_aggregation_root: Path
) -> None:
    """`sesh --aggregation-root X sessions` returns entries tagged with host."""
    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    cli.cmd_sessions(_ns(aggregation_root=str(tmp_aggregation_root)))

    out = capsys.readouterr().out
    data = json.loads(out)
    hosts = sorted({s["host"] for s in data})
    assert hosts == ["desktop", "laptop"]


def test_projects_lists_per_host_entries(
    monkeypatch, capsys, tmp_aggregation_root: Path
) -> None:
    """`sesh --aggregation-root X projects` lists each host's projects separately."""
    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    cli.cmd_projects(_ns(aggregation_root=str(tmp_aggregation_root)))

    out = capsys.readouterr().out
    data = json.loads(out)
    hosts = sorted(p["host"] for p in data)
    assert hosts == ["desktop", "laptop"]


def test_env_var_sets_aggregation_root(
    monkeypatch, capsys, tmp_aggregation_root: Path
) -> None:
    """SESH_AGGREGATION_ROOT env var is honored when --aggregation-root is unset."""
    monkeypatch.setenv("SESH_AGGREGATION_ROOT", str(tmp_aggregation_root))
    cli.cmd_sessions(_ns())  # no explicit flag

    out = capsys.readouterr().out
    data = json.loads(out)
    assert {s["host"] for s in data} == {"laptop", "desktop"}


def test_cli_flag_overrides_env_var(
    monkeypatch, capsys, tmp_path: Path, tmp_aggregation_root: Path
) -> None:
    """The --aggregation-root CLI flag takes precedence over the env var."""
    monkeypatch.setenv("SESH_AGGREGATION_ROOT", str(tmp_path / "nonexistent"))
    cli.cmd_sessions(_ns(aggregation_root=str(tmp_aggregation_root)))

    out = capsys.readouterr().out
    data = json.loads(out)
    assert {s["host"] for s in data} == {"laptop", "desktop"}


def test_delete_refused_in_aggregation_mode(
    monkeypatch, capsys, tmp_aggregation_root: Path
) -> None:
    """`sesh delete` exits non-zero with a clear message in aggregation mode."""
    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(
            _ns(
                aggregation_root=str(tmp_aggregation_root),
                session_id="sess-laptop",
                force=True,
                dry_run=False,
            )
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "aggregation mode" in err


def test_resume_refused_for_aggregated_session(
    monkeypatch, capsys, tmp_aggregation_root: Path
) -> None:
    """`sesh resume` on an aggregated session prints a host-specific error."""
    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_resume(
            _ns(
                aggregation_root=str(tmp_aggregation_root),
                session_id="sess-laptop",
            )
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "laptop" in err
    assert "source host" in err


def test_local_mode_still_works(monkeypatch, capsys, tmp_claude_dir: Path) -> None:
    """With no aggregation flag/env, the index path is unchanged and host is null."""
    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    project_path = "/Users/me/local-only"
    _seed_claude_session(tmp_claude_dir.parent, project_path, "local-sess")

    # cmd_refresh writes index, then cmd_sessions reads it
    cli.cmd_refresh(_ns())
    capsys.readouterr()  # discard refresh JSON

    cli.cmd_sessions(_ns())
    data = json.loads(capsys.readouterr().out)
    # All entries should be host=None in local mode
    assert all(s["host"] is None for s in data)
