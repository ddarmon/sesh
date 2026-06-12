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
    env.pop("XDG_CACHE_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("SESH_AGGREGATION_ROOT", None)
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
    """Full pipeline: refresh discovers Claude fixtures, then projects returns valid JSON."""
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
    """'sesh sessions --provider --project' filters to matching sessions only."""
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
    """'sesh messages' reads back the fixture data written to Claude JSONL."""
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


@pytest.mark.integration
def test_messages_last(tmp_path: Path) -> None:
    """'sesh messages last' resolves to the most recently active session."""
    _write_claude_fixture(tmp_path)

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    messages = _run_cli(tmp_path, "messages", "last", "--provider", "claude")
    assert messages.returncode == 0, messages.stderr
    data = json.loads(messages.stdout)
    assert data["total"] >= 2


@pytest.mark.integration
def test_sessions_since_until_limit(tmp_path: Path) -> None:
    """--since/--until/--limit narrow the sessions list by timestamp."""
    _write_claude_fixture(tmp_path)  # fixture timestamp: 2025-01-01

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    inside = _run_cli(tmp_path, "sessions", "--since", "2024-12-01", "--limit", "1")
    assert inside.returncode == 0, inside.stderr
    assert len(json.loads(inside.stdout)) == 1

    too_late = _run_cli(tmp_path, "sessions", "--since", "2025-06-01")
    assert too_late.returncode == 0, too_late.stderr
    assert json.loads(too_late.stdout) == []

    too_early = _run_cli(tmp_path, "sessions", "--until", "2024-12-31")
    assert too_early.returncode == 0, too_early.stderr
    assert json.loads(too_early.stdout) == []


@pytest.mark.integration
def test_export_output_file(tmp_path: Path) -> None:
    """'sesh export -o FILE' writes the Markdown file and prints a confirmation."""
    session_id = _write_claude_fixture(tmp_path)

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    out_file = tmp_path / "exports" / "session.md"
    export = _run_cli(tmp_path, "export", session_id, "-o", str(out_file))
    assert export.returncode == 0, export.stderr

    confirmation = json.loads(export.stdout)
    assert confirmation["exported"]["session_id"] == session_id
    assert confirmation["exported"]["format"] == "md"
    assert confirmation["exported"]["path"] == str(out_file)

    content = out_file.read_text(encoding="utf-8")
    assert f"# Session: {session_id}" in content
    assert "hello from integration" in content


@pytest.mark.integration
def test_bookmarks_endpoint_and_sessions_filter(tmp_path: Path) -> None:
    """'sesh bookmarks' joins the index; 'sessions --bookmarked' filters by it."""
    session_id = _write_claude_fixture(tmp_path)

    refresh = _run_cli(tmp_path, "refresh")
    assert refresh.returncode == 0, refresh.stderr

    bookmarks_file = tmp_path / ".config" / "sesh" / "bookmarks.json"
    bookmarks_file.parent.mkdir(parents=True, exist_ok=True)
    bookmarks_file.write_text(
        json.dumps(
            {
                "bookmarks": [
                    {"provider": "claude", "session_id": session_id},
                    {"provider": "codex", "session_id": "gone-session"},
                ]
            }
        )
    )

    listed = _run_cli(tmp_path, "bookmarks")
    assert listed.returncode == 0, listed.stderr
    data = json.loads(listed.stdout)
    by_key = {(e["provider"], e["session_id"]): e for e in data}
    assert by_key[("claude", session_id)]["in_index"] is True
    assert by_key[("claude", session_id)]["project_path"] == "/Users/me/repo"
    assert by_key[("codex", "gone-session")]["in_index"] is False

    filtered = _run_cli(tmp_path, "sessions", "--bookmarked")
    assert filtered.returncode == 0, filtered.stderr
    sessions = json.loads(filtered.stdout)
    assert [s["id"] for s in sessions] == [session_id]
