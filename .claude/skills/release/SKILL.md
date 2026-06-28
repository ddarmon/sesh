---
name: release
description: Cut a sesh release — derive the version bump from commit prefixes since the last tag, bump pyproject.toml + src/sesh/__init__.py in sync, run the tests, tag, and draft the GitHub release. Use when the user wants to cut/ship/publish a release or bump the version.
---

# Cut a sesh release

Releases use an **aggregate-per-release** version bump (see the Versioning
section of `CLAUDE.md`): individual PRs don't bump the version; the bump
happens once here, covering everything merged to `main` since the last
`vX.Y.Z` tag.

Follow these steps. Stop and report if any precheck fails.

## 1. Prechecks

```bash
git rev-parse --abbrev-ref HEAD      # must be: main
git status --porcelain               # must be empty (clean tree)
git fetch origin --tags
git pull --ff-only origin main       # main must fast-forward cleanly
```

If not on `main`, the tree is dirty, or the pull isn't a clean
fast-forward, stop and tell the user.

## 2. Determine the bump from commit prefixes

```bash
LAST=$(git tag --sort=-v:refname | head -1)   # e.g. v0.17.0
git log --oneline "$LAST"..HEAD
```

Read the prefixes of the commits in that range and decide:

- any `[FEATURE]` in the range → **minor** bump (X.**Y+1**.0)
- else any `[BUGFIX]` → **patch** bump (X.Y.**Z+1**)
- else (only `[CHORE]`/`[DOCS]`) → there's nothing release-worthy; ask the
  user whether to release at all (default: don't).

Compute the new version from the current version in `pyproject.toml`.

## 3. Confirm with the user

Show: last tag, the new version, the bump type + why (e.g. "minor — range
contains [FEATURE]"), and the commit list grouped by prefix. **Wait for the
user to confirm** before changing anything.

## 4. Bump both version files (keep in sync)

Edit the `version = "X.Y.Z"` line in `pyproject.toml` and the
`__version__ = "X.Y.Z"` line in `src/sesh/__init__.py` to the new version.
They must match exactly.

## 5. Test

```bash
uv run pytest -q tests
```

Abort the release (revert the version edits) if anything fails.

## 6. Commit, push, tag

```bash
git add pyproject.toml src/sesh/__init__.py
git commit -m "[CHORE] Release vX.Y.Z"
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

The `check_release_version` PreToolUse hook runs on the `git tag` /
`gh release create` step and will block if the two version files disagree
or the version wasn't bumped past the last tag — that's the safety net, not
a problem to work around.

## 7. Draft the GitHub release

Build notes from the commit range, grouped by prefix (Features /
Bug fixes / Chores), then:

```bash
gh release create vX.Y.Z --title vX.Y.Z --notes "<grouped changelog>"
```

## 8. Report

Tell the user the new version, the tag, and the release URL.

---

**Notes**

- If `main` is branch-protected and the direct `git push origin main` in
  step 6 is rejected, instead create a `chore/release-vX.Y.Z` branch with
  the bump commit, open a `[CHORE]` PR, and (after it merges) do the tag +
  `gh release create` on `main`.
- Prefix convention: `[FEATURE]` (minor), `[BUGFIX]` (patch),
  `[CHORE]`/`[DOCS]` (no bump). Use `[BUGFIX]`, not `[FIX]`.
