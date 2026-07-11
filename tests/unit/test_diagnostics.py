from __future__ import annotations

from pathlib import Path

from sesh import diagnostics


class FakeProvider:
    def __init__(self, root: Path, *, fail_project: str | None = None) -> None:
        self.root = root
        self.fail_project = fail_project

    def diagnostic_paths(self):
        return [("sessions", self.root)]

    def discover_projects(self):
        return iter((("/one", "one"), ("/two", "two")))

    def get_sessions(self, project_path, cache=None):
        if project_path == self.fail_project:
            raise ValueError("bad\nproject")
        return [object()]


def test_probe_counts_projects_and_keeps_project_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "construct_provider",
        lambda *args, **kwargs: FakeProvider(tmp_path, fail_project="/two"),
    )

    result = diagnostics._probe_provider("claude", base_dir=None, host=None)

    assert result["projects"] == 2
    assert result["sessions"] == 1
    assert result["status"] == "error"
    assert result["issues"][0]["stage"] == "get_sessions"
    assert result["issues"][0]["exception_type"] == "ValueError"
    assert "\n" not in result["issues"][0]["message"]


def test_missing_root_is_warning(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        diagnostics, "construct_provider",
        lambda *args, **kwargs: FakeProvider(missing),
    )

    result = diagnostics._probe_provider("pi", base_dir=None, host=None)

    assert result["status"] == "warning"
    assert result["paths"][0]["exists"] is False


def test_constructor_exception_is_reported(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("constructor exploded")

    monkeypatch.setattr(diagnostics, "construct_provider", fail)
    result = diagnostics._probe_provider("gemini", base_dir=None, host=None)

    assert result["status"] == "error"
    assert result["issues"] == [{
        "severity": "error",
        "stage": "construction",
        "message": "constructor exploded",
        "exception_type": "RuntimeError",
    }]


def test_human_formatter_contains_sections():
    report = {
        "status": "warning", "mode": "local", "aggregation_root": None,
        "providers": [{
            "provider": "claude", "host": None, "status": "ok",
            "projects": 1, "sessions": 2, "duration_ms": 3, "issues": [],
        }],
        "dependencies": [{"name": "rg", "available": True, "path": "/bin/rg"}],
        "app_paths": [{"label": "index", "path": "/tmp/index", "exists": False,
                       "readable": False}],
        "issues": [{"severity": "warning", "stage": "dependency", "message": "missing"}],
        "summary": {"providers_ok": 1, "providers_warning": 0, "providers_error": 0},
    }

    text = diagnostics.format_diagnostics_text(report)
    assert "sesh doctor: WARN" in text
    assert "1 projects, 2 sessions" in text
    assert "Dependencies" in text
    assert "Application paths" in text
    assert "Summary: 1 ok, 0 warning, 0 error" in text
