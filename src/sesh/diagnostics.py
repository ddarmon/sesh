"""Read-only provider and installation diagnostics for ``sesh doctor``."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from sesh.discovery import PROVIDER_NAMES, construct_provider

_RANK = {"ok": 0, "warning": 1, "error": 2}


def _issue(severity: str, stage: str, message: str, exc: BaseException | None = None) -> dict:
    text = " ".join(str(message).split())[:500]
    result = {"severity": severity, "stage": stage, "message": text}
    if exc is not None:
        result["exception_type"] = type(exc).__name__
    return result


def _path_record(label: str, path: Path) -> dict[str, Any]:
    try:
        exists = path.exists()
        is_dir = path.is_dir() if exists else False
        is_file = path.is_file() if exists else False
        readable = os.access(path, os.R_OK) if exists else False
        return {"label": label, "path": str(path), "exists": exists,
                "is_dir": is_dir, "is_file": is_file, "readable": readable}
    except OSError as exc:
        return {"label": label, "path": str(path), "exists": False,
                "is_dir": False, "is_file": False, "readable": False,
                "error": " ".join(str(exc).split())[:500]}


def _status(issues: list[dict]) -> str:
    return max((i["severity"] for i in issues), key=_RANK.get, default="ok")


def _probe_provider(name: str, *, base_dir: Path | None, host: str | None) -> dict:
    started = time.perf_counter()
    result = {"provider": name, "host": host, "status": "ok", "paths": [],
              "projects": 0, "sessions": 0, "duration_ms": 0, "issues": []}
    try:
        provider = construct_provider(name, cache=None, base_dir=base_dir, host=host)
    except Exception as exc:
        result["issues"].append(_issue("error", "construction", str(exc), exc))
        result["status"] = "error"
        result["duration_ms"] = round((time.perf_counter() - started) * 1000)
        return result

    try:
        declared = provider.diagnostic_paths()
    except Exception as exc:
        declared = []
        result["issues"].append(_issue("error", "paths", str(exc), exc))
    result["paths"] = [_path_record(label, path) for label, path in declared]
    if not declared:
        result["issues"].append(_issue("warning", "paths", "provider declared no diagnostic roots"))
    for path in result["paths"]:
        if path.get("error"):
            result["issues"].append(_issue("error", "paths", path["error"]))
        elif not path["exists"]:
            result["issues"].append(_issue("warning", "paths", f"missing root: {path['path']}"))
        elif not path["readable"]:
            result["issues"].append(_issue("error", "paths", f"unreadable root: {path['path']}"))

    try:
        projects = list(provider.discover_projects())
        result["projects"] = len(projects)
    except Exception as exc:
        projects = []
        result["issues"].append(_issue("error", "discover_projects", str(exc), exc))
    for project_path, _display_name in projects:
        try:
            result["sessions"] += len(provider.get_sessions(project_path, cache=None))
        except Exception as exc:
            result["issues"].append(_issue(
                "error", "get_sessions", f"{project_path}: {exc}", exc,
            ))
    if result["sessions"] == 0 and not any(i["severity"] == "error" for i in result["issues"]):
        result["issues"].append(_issue("warning", "discovery", "no sessions found"))
    result["status"] = _status(result["issues"])
    result["duration_ms"] = round((time.perf_counter() - started) * 1000)
    return result


def run_diagnostics(*, aggregation_root: Path | None = None,
                    provider: str | None = None) -> dict[str, Any]:
    """Run a fresh diagnostic scan without creating or saving any cache."""
    names = [provider] if provider else list(PROVIDER_NAMES)
    records: list[dict] = []
    top_issues: list[dict] = []
    mode = "aggregation" if aggregation_root is not None else "local"
    if aggregation_root is None:
        for name in names:
            records.append(_probe_provider(name, base_dir=None, host=None))
    else:
        root = Path(aggregation_root)
        try:
            hosts = [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
            if not root.is_dir():  # primarily documents intended classification
                hosts = []
        except OSError as exc:
            hosts = []
            top_issues.append(_issue("error", "aggregation_root", str(exc), exc))
        if not root.is_dir():
            top_issues.append(_issue("error", "aggregation_root", f"not a readable directory: {root}"))
        elif not hosts:
            top_issues.append(_issue("warning", "aggregation_root", "no host directories found"))
        for host_dir in hosts:
            for name in names:
                records.append(_probe_provider(name, base_dir=host_dir, host=host_dir.name))

    dependencies = []
    from sesh.resume import RESUME_COMMANDS
    binaries = ["rg"] + [argv[0] for argv in RESUME_COMMANDS.values()]
    for binary in dict.fromkeys(binaries):
        resolved = shutil.which(binary)
        dependencies.append({"name": binary, "available": resolved is not None, "path": resolved})
        if resolved is None:
            top_issues.append(_issue("warning", "dependency", f"{binary} not found on PATH"))

    from sesh.paths import CACHE_DIR, CONFIG_DIR
    app_paths = [
        _path_record("cache_dir", CACHE_DIR), _path_record("config_dir", CONFIG_DIR),
        _path_record("index", CACHE_DIR / "index.json"),
        _path_record("preferences", CONFIG_DIR / "preferences.json"),
        _path_record("bookmarks", CONFIG_DIR / "bookmarks.json"),
    ]
    for item in app_paths:
        if item["exists"] and not item["readable"]:
            top_issues.append(_issue("error", "app_paths", f"unreadable: {item['path']}"))

    counts = {s: sum(r["status"] == s for r in records) for s in _RANK}
    statuses = [r["status"] for r in records] + [i["severity"] for i in top_issues]
    overall = max(statuses, key=_RANK.get, default="ok")
    return {"status": overall, "mode": mode,
            "aggregation_root": str(aggregation_root) if aggregation_root is not None else None,
            "providers": records, "dependencies": dependencies,
            "app_paths": app_paths, "issues": top_issues,
            "summary": {f"providers_{key}": counts[key] for key in ("ok", "warning", "error")}}

def format_diagnostics_text(report: dict[str, Any]) -> str:
    """Render a concise, terminal-friendly diagnostics report."""
    marks = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    mode = report["mode"]
    if report.get("aggregation_root"):
        mode += f" ({report['aggregation_root']})"
    lines = [f"sesh doctor: {marks[report['status']]}  mode: {mode}", ""]

    lines.append("Providers")
    if not report["providers"]:
        lines.append("  (none scanned)")
    for item in report["providers"]:
        identity = item["provider"]
        if item.get("host"):
            identity = f"{item['host']}/{identity}"
        lines.append(
            f"  {marks[item['status']]:5} {identity:20} "
            f"{item['projects']} projects, {item['sessions']} sessions "
            f"({item['duration_ms']} ms)"
        )
        for issue in item["issues"]:
            lines.append(
                f"        {marks[issue['severity']]:5} "
                f"[{issue['stage']}] {issue['message']}"
            )

    lines.extend(("", "Dependencies"))
    for dependency in report["dependencies"]:
        state = "OK" if dependency["available"] else "MISSING"
        suffix = f" -> {dependency['path']}" if dependency.get("path") else ""
        lines.append(f"  {state:7} {dependency['name']}{suffix}")

    lines.extend(("", "Application paths"))
    for item in report["app_paths"]:
        if not item["exists"]:
            state = "MISSING"
        elif not item["readable"]:
            state = "ERROR"
        else:
            state = "OK"
        lines.append(f"  {state:7} {item['label']}: {item['path']}")

    if report.get("issues"):
        lines.extend(("", "General issues"))
        for issue in report["issues"]:
            lines.append(
                f"  {marks[issue['severity']]:5} "
                f"[{issue['stage']}] {issue['message']}"
            )

    summary = report["summary"]
    lines.extend((
        "",
        "Summary: "
        f"{summary['providers_ok']} ok, "
        f"{summary['providers_warning']} warning, "
        f"{summary['providers_error']} error",
    ))
    return "\n".join(lines)
