"""Terminal tab snapshots — capture and reopen running coding-agent sessions.

Public API. Backend-specific code lives behind `get_backend()` in
`sesh.snapshots.backend`; everything in this package is platform-agnostic
above that seam.
"""

from __future__ import annotations

from sesh.paths import SNAPSHOTS_DIR
from sesh.snapshots.core import (
    SCHEMA_VERSION,
    SCROLLBACK_MAX_LINES,
    PreviewResult,
    RestoreItem,
    RestorePlan,
    RestoreReport,
    Snapshot,
    SnapshotResume,
    SnapshotSummary,
    SnapshotsError,
    SnapshotsNotFoundError,
    SnapshotsSchemaError,
    SnapshotsUnsupportedError,
    SnapshotTab,
    build_restore_plan,
    capture,
    delete,
    list_snapshots,
    load,
    restore,
    save,
)

__all__ = [
    "SCHEMA_VERSION",
    "SCROLLBACK_MAX_LINES",
    "SNAPSHOTS_DIR",
    "PreviewResult",
    "RestoreItem",
    "RestorePlan",
    "RestoreReport",
    "Snapshot",
    "SnapshotResume",
    "SnapshotSummary",
    "SnapshotTab",
    "SnapshotsError",
    "SnapshotsNotFoundError",
    "SnapshotsSchemaError",
    "SnapshotsUnsupportedError",
    "build_restore_plan",
    "capture",
    "delete",
    "list_snapshots",
    "load",
    "restore",
    "save",
]
