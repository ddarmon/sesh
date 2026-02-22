from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import write_jsonl


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    existing_pythonpath = env.get("PYTHONPATH")
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = (
        src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    )
    return subprocess.run(
        [sys.executable, "-m", "sesh.cli", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _write_claude_fixture(home: Path, project_path: str = "/Users/me/repo") -> str:
    encoded = project_path.replace("/", "-").replace(" ", "-")
    project_dir = home / ".claude" / "projects" / encoded
    session_id = "claude-int-1"
    write_jsonl(
        project_dir / "session.jsonl",
        [
            {
                "sessionId": session_id,
                "timestamp": "2025-01-01T00:00:00Z",
                "uuid": "root-1",
                "parentUuid": None,
                "cwd": project_path,
                "message": {"role": "user", "content": "hello from integration"},
            },
            {
                "sessionId": session_id,
                "timestamp": "2025-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": "hi"},
            },
        ],
    )
    return session_id


@pytest.mark.integration
def test_refresh_then_projects(tmp_path: Path) -> None:
    _write_claude_fixture(tmp_path)

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr
    refresh_json = json.loads(refresh.stdout)
    assert refresh_json["projects"] == 1
    assert refresh_json["sessions"] == 1
    assert refresh_json["providers"] == ["claude"]

    projects = _run_cli(tmp_path, "projects")
    assert projects.returncode == 0, projects.stderr
    projects_json = json.loads(projects.stdout)
    assert len(projects_json) == 1
    assert projects_json[0]["path"] == "/Users/me/repo"


@pytest.mark.integration
def test_sessions_filter(tmp_path: Path) -> None:
    _write_claude_fixture(tmp_path, "/Users/me/repo1")
    _write_claude_fixture(tmp_path, "/Users/me/repo2")

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    sessions = _run_cli(tmp_path, "sessions", "--provider", "claude", "--project", "/Users/me/repo1")
    assert sessions.returncode == 0, sessions.stderr
    data = json.loads(sessions.stdout)
    assert len(data) == 1
    assert data[0]["provider"] == "claude"
    assert data[0]["project_path"] == "/Users/me/repo1"
    assert "source_path" not in data[0]


@pytest.mark.integration
def test_messages_roundtrip(tmp_path: Path) -> None:
    session_id = _write_claude_fixture(tmp_path)

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    messages = _run_cli(tmp_path, "messages", session_id, "--provider", "claude", "--limit", "10")
    assert messages.returncode == 0, messages.stderr
    data = json.loads(messages.stdout)
    assert data["total"] >= 2
    roles = [m["role"] for m in data["messages"]]
    assert "user" in roles
    assert any("integration" in (m["content"] or "") for m in data["messages"])

