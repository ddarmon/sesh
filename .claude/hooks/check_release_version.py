#!/usr/bin/env python3
"""PreToolUse guardrail: block tagging / publishing a release unless the
version was actually bumped and the two version files agree.

Registered as a Bash PreToolUse hook. It only acts on commands that create a
version tag or a GitHub release (`git tag vX.Y.Z`, `git push ... vX.Y.Z`,
`gh release create`); every other command passes through untouched.

On a release command it verifies:
  * pyproject.toml `version` == src/sesh/__init__.py `__version__`
  * that version is strictly greater than the latest `vX.Y.Z` tag

If a check fails it returns a PreToolUse `deny` with the reason (exit 0 +
structured JSON, the recommended contract). Otherwise it stays silent and
lets the normal permission flow proceed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys


def _allow() -> None:
    # Silent pass-through: normal permission flow still applies.
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# A release-triggering command: creating/pushing a vX.Y.Z tag or a GH release.
_SEMVER = r"v?\d+\.\d+\.\d+"
_RELEASE_PATTERNS = [
    re.compile(r"\bgh\s+release\s+create\b"),
    re.compile(rf"\bgit\s+tag\b.*\b{_SEMVER}\b"),
    re.compile(rf"\bgit\s+push\b.*\b{_SEMVER}\b"),
]


def _is_release_command(cmd: str) -> bool:
    return any(p.search(cmd) for p in _RELEASE_PATTERNS)


def _repo_root() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _read_version(path: str, pattern: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(pattern, line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def _last_tag_version(root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "tag", "--sort=-v:refname"],
            capture_output=True, text=True, timeout=10, cwd=root,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if re.fullmatch(r"v?\d+\.\d+\.\d+", line):
                return line.lstrip("v")
    except Exception:
        return None
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        _allow()

    if data.get("tool_name") != "Bash":
        _allow()

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not _is_release_command(cmd):
        _allow()

    root = _repo_root()
    if root is None:
        _allow()  # not in a git repo; nothing to guard

    pyproj = _read_version(f"{root}/pyproject.toml", r'version\s*=\s*"([^"]+)"')
    init = _read_version(f"{root}/src/sesh/__init__.py", r'__version__\s*=\s*"([^"]+)"')

    if pyproj is None or init is None:
        _deny(
            "Release blocked: couldn't read the version from "
            f"{'pyproject.toml' if pyproj is None else ''} "
            f"{'src/sesh/__init__.py' if init is None else ''}".strip()
            + ". Verify both version lines exist before tagging/releasing."
        )

    if pyproj != init:
        _deny(
            f"Release blocked: version files disagree — pyproject.toml is "
            f"{pyproj} but src/sesh/__init__.py is {init}. Sync them, then retry."
        )

    last = _last_tag_version(root)
    if last is not None and _version_tuple(pyproj) <= _version_tuple(last):
        _deny(
            f"Release blocked: version {pyproj} is not greater than the last "
            f"release tag v{last}. Bump pyproject.toml + src/sesh/__init__.py "
            f"(see the /release skill) before tagging or publishing."
        )

    _allow()


if __name__ == "__main__":
    main()
