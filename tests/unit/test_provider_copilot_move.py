"""Tests for Copilot provider project move."""

from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers.copilot import CopilotProvider, _parse_workspace_yaml
from tests.helpers import write_workspace_yaml


def test_move_rewrites_workspace_yaml(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1",
        "cwd": "/old/repo",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    provider = CopilotProvider()
    report = provider.move_project("/old/repo", "/new/repo")
    assert report.success is True
    assert report.provider is Provider.COPILOT
    assert report.files_modified == 1

    meta = _parse_workspace_yaml(s_dir / "workspace.yaml")
    assert meta["cwd"] == "/new/repo"


def test_move_no_match_returns_zero_modified(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1",
        "cwd": "/other/repo",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    provider = CopilotProvider()
    report = provider.move_project("/old/repo", "/new/repo")
    assert report.success is True
    assert report.files_modified == 0


def test_move_missing_dir_returns_success(tmp_copilot_dir: Path) -> None:
    """When COPILOT_DIR doesn't exist, move succeeds with zero modifications."""
    provider = CopilotProvider()
    report = provider.move_project("/old/repo", "/new/repo")
    assert report.success is True
    assert report.files_modified == 0
