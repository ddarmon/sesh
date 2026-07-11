---
Status: complete
Type: feature
Owner: David
Branch: current
Created: 2026-07-11
Updated: 2026-07-11
---

# `sesh doctor` provider diagnostics

## Goal

Add a read-only `sesh doctor` command that distinguishes “no provider data” from
“provider discovery failed” and emits structured JSON suitable for both people
and automation. It must diagnose local and aggregation modes without mutating
the session index or hiding provider-construction/discovery exceptions.

## Scope

### V1 includes

- One diagnostic record per provider (and per host/provider in aggregation
  mode).
- Provider roots scanned, with existence, directory/file type, and readability.
- Provider construction, project discovery, and session discovery outcomes.
- Counts for projects and sessions found and elapsed discovery time.
- Sanitized exception type/message and the stage that failed; no traceback in
  normal output.
- Availability and resolved paths for provider resume CLIs and `rg`.
- Cache/config/index path checks using the existing XDG path resolution.
- Top-level summary status: `ok`, `warning`, or `error`.
- `--provider` filtering and support for the global `--aggregation-root` option.
- `--strict` for CI/automation: exit nonzero when warnings or errors are found.
  Default mode emits the report and exits zero if the diagnostic run itself
  completed.

### Explicitly deferred

- TUI provider-health modal or status badge.
- Repair, cache deletion, permission changes, or CLI installation.
- Reading every message body or validating every source record.
- A provider plugin/registry refactor.
- Persisting historical health reports.

V1 reports file-level parse failures that escape provider APIs. Some providers
currently skip malformed individual records defensively; counting every skipped
record requires a separate provider parser-observability pass and should not be
implied by the initial command.

## Output contract

`doctor` follows the project’s JSON-only CLI convention. Proposed shape:

```json
{
  "status": "warning",
  "mode": "local",
  "aggregation_root": null,
  "providers": [
    {
      "provider": "claude",
      "host": null,
      "status": "ok",
      "paths": [
        {"label": "projects", "path": "/home/me/.claude/projects", "exists": true, "readable": true}
      ],
      "projects": 4,
      "sessions": 28,
      "duration_ms": 31,
      "issues": []
    }
  ],
  "dependencies": [
    {"name": "rg", "available": true, "path": "/opt/homebrew/bin/rg"}
  ],
  "app_paths": [],
  "summary": {"providers_ok": 6, "providers_warning": 1, "providers_error": 0}
}
```

Status rules:

- `ok`: scan completed and at least one configured root is usable.
- `warning`: expected root or optional binary is absent, no sessions were found,
  or a non-fatal path/config issue exists.
- `error`: provider construction or discovery raised, an existing required root
  is unreadable, or the aggregation root/host cannot be scanned.
- Missing provider data is a warning rather than an error because a provider may
  simply not be installed or used.

Exception messages must be bounded and represented as data, not printed to
stderr. Paths are intentionally included because this is a local diagnostic
command; no transcript content is included.

## Design

### 1. Diagnostic models and runner

Create `src/sesh/diagnostics.py` with dataclasses (or equivalent pure records)
for paths, issues, dependencies, provider results, and the complete report.
Keep status aggregation and JSON conversion pure and unit-testable.

The runner constructs each provider independently, times each stage, and
continues after failures. Aggregation mode produces records keyed by
`(host, provider)`, preserving failures on one host without suppressing healthy
hosts.

### 2. Share provider construction without changing normal discovery behavior

Refactor provider construction in `src/sesh/discovery.py` into a single ordered
provider specification/factory list used by both normal discovery and doctor.
Normal `discover_all()` retains its existing return type and best-effort
behavior, avoiding a broad caller migration. The diagnostic path captures the
exceptions that normal discovery intentionally tolerates.

Do not couple Gemini and opencode construction in one `try` block; each provider
must receive an independent result.

### 3. Provider diagnostic roots

Add a small non-abstract `diagnostic_paths()` capability to
`SessionProvider` in `src/sesh/providers/__init__.py`, returning labeled paths.
Implement it for all seven providers using their existing resolved local or
`base_dir` paths. Keeping it non-abstract avoids breaking test doubles and
future providers.

This API exposes scan roots only; it does not perform I/O or introduce a global
provider registry.

### 4. Discovery probe

For each provider:

1. Check declared roots with non-mutating `stat`/access probes.
2. Materialize `discover_projects()` while capturing exceptions.
3. Call `get_sessions()` once per discovered project and aggregate counts,
   capturing project-specific failures rather than aborting the provider.
4. Run without saving `SessionCache` or `index.json`.

A fresh uncached scan is preferred so doctor tests the parser and source access,
not merely cached metadata. If runtime becomes problematic, add an explicit
future `--quick` mode rather than silently weakening the default diagnosis.

### 5. Dependencies and app paths

- Use `shutil.which` for `rg` and each provider’s resume binary from
  `resume.RESUME_COMMANDS`.
- Deduplicate binaries shared across records.
- Reuse cache/config path helpers rather than reconstructing XDG rules.
- Report path state only; do not create directories as a side effect.

### 6. CLI wiring

Add `cmd_doctor` and a `doctor` parser in `src/sesh/cli.py`:

```text
sesh doctor [--provider PROVIDER] [--strict]
sesh --aggregation-root /mirror doctor [--provider PROVIDER] [--strict]
```

The command writes exactly one JSON document to stdout. Argument errors retain
argparse behavior. `--strict` exits `1` after writing the report if overall
status is not `ok`.

## Rollout

1. Add pure diagnostic models, status rules, serialization, and tests.
2. Refactor provider factories in `discovery.py`; verify normal discovery output
   is unchanged and provider failures remain isolated.
3. Add `diagnostic_paths()` to the provider base and seven implementations.
4. Implement local diagnostic scans, dependency checks, and app-path checks.
5. Add aggregation host/provider diagnostics and invalid-root handling.
6. Wire `sesh doctor`, provider filtering, and strict exit semantics.
7. Update README, CLI table, and `CLAUDE.md`; move this plan to `.plans/done/`
   after validation.

## Tests

### Unit

- Status precedence and JSON serialization.
- Missing root => warning; unreadable existing root => error.
- Constructor, `discover_projects`, and per-project `get_sessions` exceptions
  are retained with provider/stage attribution.
- One failing project does not hide counts/results from other projects.
- Provider factories are independent, especially Gemini and opencode.
- Dependency availability and missing binaries.
- Provider filter validation and strict/default exit behavior.
- Exception text is bounded and no traceback leaks into JSON.

### Integration

- `sesh doctor` emits valid JSON against isolated fixture roots and does not
  write the normal index.
- All seven providers appear in local mode.
- Aggregation output contains one record per host/provider and reports an
  invalid root clearly.
- A malformed/raising provider fixture does not prevent healthy provider
  results.
- Absolute, empty, and relative XDG environment cases follow existing path
  behavior.

## Validation

```bash
uv run pytest -q tests
uv run sesh doctor
uv run sesh doctor --provider claude
uv run sesh doctor --strict
uv run sesh --aggregation-root /tmp/sesh-doctor-fixture doctor
```

Manual review should confirm that stdout remains valid JSON in warning/error
cases and that running doctor does not change `index.json`, preferences,
bookmarks, or provider source data.

## Risks and mitigations

- **False confidence about malformed records:** Describe V1 as boundary/path
  diagnostics and explicitly report that deep record validation is not run.
- **Slow scans:** Time every provider and preserve full scans initially; add an
  explicit quick mode only from measured need.
- **Behavior drift from discovery refactor:** Keep `discover_all()`’s public
  contract unchanged and add regression tests around provider isolation.
- **Permission checks can race:** Treat checks as advisory; actual discovery
  exceptions remain authoritative.
- **Sensitive diagnostics:** Include local paths but never message content,
  environment values, or tracebacks.

## Decision log

- 2026-07-11: Start with a CLI-only, read-only JSON command; defer TUI health UI.
- 2026-07-11: Default exit is zero for completed diagnostics; `--strict` enables
  CI-friendly failure semantics.
- 2026-07-11: Use fresh discovery without writing cache/index so doctor probes
  real sources and remains side-effect free.
- 2026-07-11: Preserve JSON as the automation-friendly default and add
  `--human` for a concise terminal report.
