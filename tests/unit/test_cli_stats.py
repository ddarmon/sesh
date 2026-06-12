from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sesh import cli
from sesh.cache import _session_to_dict
from sesh.models import Provider
from tests.helpers import make_session


def _ns(**kwargs):
    kwargs.setdefault("project", None)
    kwargs.setdefault("provider", None)
    return argparse.Namespace(**kwargs)


def _session_dict(**overrides) -> dict:
    return _session_to_dict(make_session(**overrides))


def _run_stats(monkeypatch, capsys, sessions: list[dict], **ns_kwargs) -> dict:
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: {"sessions": sessions})
    cli.cmd_stats(_ns(**ns_kwargs))
    return json.loads(capsys.readouterr().out)


def test_cmd_stats_totals_and_provider_rollups(monkeypatch, capsys) -> None:
    """Totals and per-provider rollups sum tokens and track timestamp ranges."""
    sessions = [
        _session_dict(
            id="a",
            provider=Provider.CLAUDE,
            project_path="/p1",
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            input_tokens=100,
            output_tokens=10,
            cumulative_input_tokens=300,
        ),
        _session_dict(
            id="b",
            provider=Provider.CLAUDE,
            project_path="/p1",
            timestamp=datetime(2025, 3, 1, tzinfo=timezone.utc),
            input_tokens=200,
            output_tokens=20,
            cumulative_input_tokens=500,
        ),
        _session_dict(
            id="c",
            provider=Provider.CODEX,
            project_path="/p2",
            timestamp=datetime(2025, 2, 1, tzinfo=timezone.utc),
            input_tokens=None,
            output_tokens=5,
            cumulative_input_tokens=None,
        ),
    ]
    out = _run_stats(monkeypatch, capsys, sessions)

    assert out["totals"] == {
        "sessions": 3,
        "sessions_with_tokens": 3,
        "output_tokens": 35,
        "cumulative_input_tokens": 800,
        "earliest": "2025-01-01T00:00:00+00:00",
        "latest": "2025-03-01T00:00:00+00:00",
    }

    assert [p["provider"] for p in out["providers"]] == ["claude", "codex"]
    claude = out["providers"][0]
    assert claude["sessions"] == 2
    assert claude["sessions_with_tokens"] == 2
    assert claude["output_tokens"] == 30
    assert claude["cumulative_input_tokens"] == 800
    assert claude["earliest"] == "2025-01-01T00:00:00+00:00"
    assert claude["latest"] == "2025-03-01T00:00:00+00:00"

    codex = out["providers"][1]
    assert codex["sessions"] == 1
    assert codex["output_tokens"] == 5
    assert codex["cumulative_input_tokens"] == 0


def test_cmd_stats_project_rollups(monkeypatch, capsys) -> None:
    """Per-project rollups group by project path and are sorted by path."""
    sessions = [
        _session_dict(id="a", project_path="/zeta", output_tokens=1),
        _session_dict(id="b", project_path="/alpha", output_tokens=2),
        _session_dict(id="c", project_path="/alpha", output_tokens=3),
    ]
    out = _run_stats(monkeypatch, capsys, sessions)

    assert [p["project_path"] for p in out["projects"]] == ["/alpha", "/zeta"]
    alpha = out["projects"][0]
    assert alpha["sessions"] == 2
    assert alpha["output_tokens"] == 5
    assert alpha["host"] is None


def test_cmd_stats_cumulative_falls_back_to_input_tokens(monkeypatch, capsys) -> None:
    """When cumulative_input_tokens is absent, input_tokens is used instead."""
    sessions = [
        _session_dict(
            id="a",
            input_tokens=100,
            output_tokens=10,
            cumulative_input_tokens=None,
        ),
        _session_dict(
            id="b",
            input_tokens=999,
            output_tokens=20,
            cumulative_input_tokens=400,
        ),
    ]
    out = _run_stats(monkeypatch, capsys, sessions)
    assert out["totals"]["cumulative_input_tokens"] == 500


def test_cmd_stats_sessions_without_tokens_counted_separately(monkeypatch, capsys) -> None:
    """Cursor-style sessions without token data count toward sessions only."""
    sessions = [
        _session_dict(
            id="a",
            provider=Provider.CURSOR,
            input_tokens=None,
            output_tokens=None,
            cumulative_input_tokens=None,
        ),
        _session_dict(
            id="b",
            provider=Provider.CLAUDE,
            input_tokens=None,
            output_tokens=10,
            cumulative_input_tokens=None,
        ),
    ]
    out = _run_stats(monkeypatch, capsys, sessions)

    assert out["totals"]["sessions"] == 2
    assert out["totals"]["sessions_with_tokens"] == 1
    assert out["totals"]["output_tokens"] == 10
    assert out["totals"]["cumulative_input_tokens"] == 0

    cursor = [p for p in out["providers"] if p["provider"] == "cursor"][0]
    assert cursor["sessions"] == 1
    assert cursor["sessions_with_tokens"] == 0


def test_cmd_stats_provider_and_project_filters(monkeypatch, capsys) -> None:
    """--provider and --project narrow the aggregated input set."""
    sessions = [
        _session_dict(id="a", provider=Provider.CLAUDE, project_path="/p1", output_tokens=1),
        _session_dict(id="b", provider=Provider.CODEX, project_path="/p1", output_tokens=2),
        _session_dict(id="c", provider=Provider.CLAUDE, project_path="/p2", output_tokens=4),
    ]

    out = _run_stats(monkeypatch, capsys, sessions, provider="claude")
    assert out["totals"]["sessions"] == 2
    assert [p["provider"] for p in out["providers"]] == ["claude"]
    assert out["totals"]["output_tokens"] == 5

    out = _run_stats(monkeypatch, capsys, sessions, project="/p1")
    assert out["totals"]["sessions"] == 2
    assert [p["project_path"] for p in out["projects"]] == ["/p1"]
    assert out["totals"]["output_tokens"] == 3

    out = _run_stats(monkeypatch, capsys, sessions, project="/p1", provider="codex")
    assert out["totals"]["sessions"] == 1
    assert out["totals"]["output_tokens"] == 2


def test_cmd_stats_hosts_keep_identical_paths_separate(monkeypatch, capsys) -> None:
    """In aggregation mode, the same project path on two hosts yields two rollups."""
    sessions = [
        _session_dict(id="a", project_path="/repo", host="laptop", output_tokens=1),
        _session_dict(id="b", project_path="/repo", host="desktop", output_tokens=2),
        _session_dict(id="c", project_path="/repo", host="laptop", output_tokens=4),
    ]
    out = _run_stats(monkeypatch, capsys, sessions)

    assert out["totals"]["sessions"] == 3
    assert [(p["host"], p["project_path"]) for p in out["projects"]] == [
        ("desktop", "/repo"),
        ("laptop", "/repo"),
    ]
    laptop = out["projects"][1]
    assert laptop["sessions"] == 2
    assert laptop["output_tokens"] == 5


def test_cmd_stats_empty_index(monkeypatch, capsys) -> None:
    """An empty index produces zeroed totals and empty rollup lists."""
    out = _run_stats(monkeypatch, capsys, [])
    assert out == {
        "totals": {
            "sessions": 0,
            "sessions_with_tokens": 0,
            "output_tokens": 0,
            "cumulative_input_tokens": 0,
            "earliest": None,
            "latest": None,
        },
        "providers": [],
        "projects": [],
    }


def test_cmd_stats_unparseable_timestamp_skipped_for_range(monkeypatch, capsys) -> None:
    """Sessions with bad timestamps still count but don't poison earliest/latest."""
    good = _session_dict(
        id="a",
        timestamp=datetime(2025, 5, 1, tzinfo=timezone.utc),
        output_tokens=1,
    )
    bad = _session_dict(id="b", output_tokens=2)
    bad["timestamp"] = "not-a-timestamp"

    out = _run_stats(monkeypatch, capsys, [good, bad])
    assert out["totals"]["sessions"] == 2
    assert out["totals"]["earliest"] == "2025-05-01T00:00:00+00:00"
    assert out["totals"]["latest"] == "2025-05-01T00:00:00+00:00"
