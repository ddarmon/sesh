"""Aggregation-mode discovery tests.

When ``discover_all(aggregation_root=...)`` is invoked, each immediate
subdirectory of *aggregation_root* is treated as one host's mirrored
$HOME (.claude/, .codex/, .pi/, etc.). Sessions from different hosts
must stay separate even when their project paths collide, and every
returned object must carry the host name.
"""

from __future__ import annotations

from pathlib import Path

from sesh.discovery import discover_all
from sesh.models import encode_claude_path
from tests.helpers import write_jsonl


def _make_claude_session(host_root: Path, project_path: str, session_id: str) -> None:
    """Create a minimal Claude JSONL session under host_root/.claude/projects/..."""
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


def test_aggregation_missing_root_returns_empty(tmp_path: Path) -> None:
    """If the aggregation root doesn't exist, discovery returns empty dicts."""
    projects, sessions = discover_all(aggregation_root=tmp_path / "nope")
    assert projects == {}
    assert sessions == {}


def test_aggregation_single_host_stamps_host(tmp_path: Path) -> None:
    """A single-host aggregation root stamps host on Project and SessionMeta."""
    agg = tmp_path / "agg"
    laptop = agg / "laptop"
    _make_claude_session(laptop, "/Users/me/repo-a", "sess-a")

    projects, sessions = discover_all(aggregation_root=agg)

    assert len(projects) == 1
    key = next(iter(projects))
    assert key == "laptop::/Users/me/repo-a"
    proj = projects[key]
    assert proj.host == "laptop"
    assert proj.path == "/Users/me/repo-a"

    sess_list = sessions[key]
    assert len(sess_list) == 1
    assert sess_list[0].host == "laptop"
    assert sess_list[0].project_path == "/Users/me/repo-a"


def test_aggregation_same_path_two_hosts_stays_separate(tmp_path: Path) -> None:
    """Identical project paths on different hosts produce two Project entries."""
    agg = tmp_path / "agg"
    _make_claude_session(agg / "laptop", "/Users/me/shared", "sess-laptop")
    _make_claude_session(agg / "desktop", "/Users/me/shared", "sess-desktop")

    projects, sessions = discover_all(aggregation_root=agg)

    assert set(projects) == {
        "desktop::/Users/me/shared",
        "laptop::/Users/me/shared",
    }
    laptop_proj = projects["laptop::/Users/me/shared"]
    desktop_proj = projects["desktop::/Users/me/shared"]
    assert laptop_proj.host == "laptop"
    assert desktop_proj.host == "desktop"

    laptop_sess = sessions["laptop::/Users/me/shared"]
    desktop_sess = sessions["desktop::/Users/me/shared"]
    assert {s.id for s in laptop_sess} == {"sess-laptop"}
    assert {s.id for s in desktop_sess} == {"sess-desktop"}
    assert laptop_sess[0].host == "laptop"
    assert desktop_sess[0].host == "desktop"


def test_aggregation_host_with_no_providers_does_not_break_discovery(
    tmp_path: Path,
) -> None:
    """A host subdir with no recognized provider dirs is silently skipped."""
    agg = tmp_path / "agg"
    (agg / "empty-host").mkdir(parents=True)
    _make_claude_session(agg / "real-host", "/Users/me/proj", "sess-1")

    projects, _ = discover_all(aggregation_root=agg)

    assert set(projects) == {"real-host::/Users/me/proj"}


def test_aggregation_skips_hidden_subdirs(tmp_path: Path) -> None:
    """Subdirectories starting with '.' (e.g. .DS_Store-like dirs) are skipped."""
    agg = tmp_path / "agg"
    _make_claude_session(agg / ".hidden", "/Users/me/secret", "sess-hidden")
    _make_claude_session(agg / "laptop", "/Users/me/repo", "sess-laptop")

    projects, _ = discover_all(aggregation_root=agg)

    assert set(projects) == {"laptop::/Users/me/repo"}


def test_local_mode_unchanged_no_host(tmp_path: Path, tmp_claude_dir: Path) -> None:
    """Without aggregation_root, sessions have host=None and key=raw path."""
    project_path = "/Users/me/local"
    encoded = encode_claude_path(project_path)
    write_jsonl(
        tmp_claude_dir / "projects" / encoded / "sess.jsonl",
        [
            {
                "sessionId": "sess-1",
                "cwd": project_path,
                "timestamp": "2025-01-01T00:00:00Z",
                "type": "summary",
                "summary": "Local",
            },
            {
                "sessionId": "sess-1",
                "cwd": project_path,
                "timestamp": "2025-01-01T00:00:01Z",
                "uuid": "u1",
                "parentUuid": None,
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )

    projects, sessions = discover_all()

    assert project_path in projects
    assert projects[project_path].host is None
    assert sessions[project_path][0].host is None
