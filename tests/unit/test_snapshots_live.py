"""Process-based resume resolution for terminal tab snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sesh.models import LiveProcess, Provider, SearchResult
from sesh.providers.agy import AgyProvider
from sesh.providers.claude import ClaudeProvider
from sesh.providers.copilot import CopilotProvider
from sesh.snapshots import core, live
from sesh.snapshots.backend import CapturedTab, TtyProcess
from sesh.snapshots.terminal_app import _parse_ps_processes
from tests.helpers import make_session, make_snapshot, make_snapshot_resume, make_snapshot_tab

START = 1_800_000_000.0
LINE = "this is a long enough line of regular english to qualify probably yeah"


def _proc(pid: int, ppid: int, *argv: str, start: float | None = START) -> TtyProcess:
    return TtyProcess(pid=pid, ppid=ppid, start=start, argv=list(argv))


def _shell_and(*agent_argv: str, pid: int = 200) -> list[TtyProcess]:
    return [
        _proc(100, 1, "login", "-pf", "me", start=START - 10),
        _proc(101, 100, "-zsh", start=START - 10),
        _proc(pid, 101, *agent_argv),
    ]


def _tab(processes: list[TtyProcess], cwd: str = "/proj", scrollback: str = "") -> CapturedTab:
    return CapturedTab(
        window=1, tab=1, tty="/dev/ttys001", cwd=cwd,
        scrollback_tail=scrollback, processes=processes,
    )


def _resolve(tab: CapturedTab, sessions: list | None = None, monkeypatch=None):
    ctx = core._ResolveContext(None)
    ctx._sessions = list(sessions or [])
    return core._resolve_resume(tab, tab.scrollback_tail, tab.cwd, ctx)


def _session(sid: str, provider: Provider, cwd: str, at: float):
    return make_session(
        id=sid, provider=provider, project_path=cwd,
        timestamp=datetime.fromtimestamp(at, tz=timezone.utc),
    )


# ----- ps parsing -----------------------------------------------------------


def test_parse_ps_processes_reads_pid_ppid_start_and_argv() -> None:
    out = (
        "84534 84533 Thu Nov 26 11:38:01 2026     -zsh\n"
        "84892 84534 Thu Nov 26 11:38:13 2026     claude --resume abc-123\n"
        "garbage line\n"
    )
    procs = _parse_ps_processes(out)
    assert [(p.pid, p.ppid, p.argv) for p in procs] == [
        (84534, 84533, ["-zsh"]),
        (84892, 84534, ["claude", "--resume", "abc-123"]),
    ]
    assert procs[1].start == datetime(2026, 11, 26, 11, 38, 13).timestamp()


def test_parse_ps_processes_keeps_a_process_with_an_unparseable_start() -> None:
    procs = _parse_ps_processes("5 1 Xxx Yyy 99 99:99:99 2026     codex\n")
    assert len(procs) == 1 and procs[0].start is None and procs[0].argv == ["codex"]


# ----- finding the agent ----------------------------------------------------


def test_find_agent_ignores_shells_and_helpers() -> None:
    procs = _shell_and("claude") + [
        _proc(300, 200, "node", "/x/bin/xcodebuildmcp", "mcp", start=START + 1),
        _proc(301, 200, "caffeinate", "-i", start=START + 2),
    ]
    provider, proc = live.find_agent(procs)
    assert provider is Provider.CLAUDE and proc.pid == 200


def test_find_agent_sees_through_a_node_launcher_and_full_paths() -> None:
    provider, proc = live.find_agent(_shell_and("node", "/n/bin/copilot"))
    assert provider is Provider.COPILOT and proc.pid == 200
    provider, _ = live.find_agent(_shell_and("/opt/homebrew/bin/codex", "resume", "x"))
    assert provider is Provider.CODEX
    provider, _ = live.find_agent(
        _shell_and("/n/node", "--max-old-space-size=8192", "/opt/homebrew/bin/gemini")
    )
    assert provider is Provider.GEMINI


def test_find_agent_none_for_a_plain_shell() -> None:
    assert live.find_agent(_shell_and("vim", "notes.md")) is None
    assert live.find_agent([]) is None


def test_find_agent_prefers_the_earliest_started_match() -> None:
    procs = _shell_and("node", "/n/bin/gemini") + [
        _proc(250, 200, "node", "/n/bin/gemini", start=START + 3),
    ]
    assert live.find_agent(procs)[1].pid == 200


def test_descendant_pids_walks_the_tree() -> None:
    procs = _shell_and("node", "/n/bin/copilot") + [
        _proc(201, 200, "/n/native/copilot"),
        _proc(202, 201, "helper"),
        _proc(999, 101, "unrelated"),
    ]
    assert live.descendant_pids(200, procs) == (200, 201, 202)


# ----- provider records -----------------------------------------------------


def _write_claude_record(claude_dir: Path, pid: int, session_id: str, started: float) -> None:
    sessions = claude_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "startedAt": int(started * 1000),
    }))


def test_claude_live_session_id_reads_the_pid_record(tmp_claude_dir: Path) -> None:
    _write_claude_record(tmp_claude_dir, 200, "sess-live", START + 1)
    lp = LiveProcess(pid=200, pids=(200,), start=START)
    assert ClaudeProvider().live_session_id(lp) == "sess-live"


def test_claude_live_session_id_rejects_a_record_from_a_reused_pid(tmp_claude_dir: Path) -> None:
    _write_claude_record(tmp_claude_dir, 200, "sess-old", START - 86_400)
    lp = LiveProcess(pid=200, pids=(200,), start=START)
    assert ClaudeProvider().live_session_id(lp) is None


def test_claude_live_session_id_none_without_a_usable_record(tmp_claude_dir: Path) -> None:
    lp = LiveProcess(pid=200, pids=(200,), start=START)
    assert ClaudeProvider().live_session_id(lp) is None
    sessions = tmp_claude_dir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "200.json").write_text("{not json")
    assert ClaudeProvider().live_session_id(lp) is None
    # A record naming another pid, or an id unsafe to pass to a shell.
    (sessions / "200.json").write_text(json.dumps({"pid": 7, "sessionId": "s"}))
    assert ClaudeProvider().live_session_id(lp) is None
    (sessions / "200.json").write_text(json.dumps({"pid": 200, "sessionId": "../x"}))
    assert ClaudeProvider().live_session_id(lp) is None


def test_copilot_live_session_id_matches_a_lock_held_by_a_child(tmp_copilot_dir: Path) -> None:
    (tmp_copilot_dir / "sess-live").mkdir(parents=True)
    (tmp_copilot_dir / "sess-live" / "inuse.201.lock").write_text("201")
    # A lock left behind by a dead process names a pid outside the tree.
    (tmp_copilot_dir / "sess-stale").mkdir()
    (tmp_copilot_dir / "sess-stale" / "inuse.5581.lock").write_text("5581")
    lp = LiveProcess(pid=200, pids=(200, 201), start=START)
    assert CopilotProvider().live_session_id(lp) == "sess-live"
    assert CopilotProvider().live_session_id(LiveProcess(pid=300, pids=(300,))) is None


def test_agy_live_session_id_needs_the_lock_to_be_open(tmp_agy_dir: Path) -> None:
    conv = "9c2fe783-f774-4a75-8a83-0e2bb219b13f"
    presence = tmp_agy_dir / "presence"
    presence.mkdir(parents=True)
    (presence / f"{conv}.lock").write_text("")
    provider = AgyProvider()
    # The file exists for every past conversation; only an open one counts.
    assert provider.live_session_id(LiveProcess(pid=200, pids=(200,))) is None
    lp = LiveProcess(
        pid=200, pids=(200,),
        open_files=[str(presence / f"{conv}.lock"), str(presence / "not-a-uuid.lock")],
    )
    assert provider.live_session_id(lp) == conv


def test_agy_live_session_id_none_when_two_roots_are_open(tmp_agy_dir: Path) -> None:
    presence = tmp_agy_dir / "presence"
    a = "9c2fe783-f774-4a75-8a83-0e2bb219b13f"
    b = "1e4c7487-59c9-4770-bb30-a2bb9a4c95d8"
    lp = LiveProcess(pid=200, open_files=[str(presence / f"{a}.lock"), str(presence / f"{b}.lock")])
    assert AgyProvider().live_session_id(lp) is None


# ----- tiers ----------------------------------------------------------------


def test_process_record_beats_a_stale_resume_line_in_scrollback(tmp_claude_dir: Path) -> None:
    """A previous session's exit hint must not name the tab's session."""
    _write_claude_record(tmp_claude_dir, 200, "sess-live", START)
    tab = _tab(_shell_and("claude"), scrollback="claude --resume sess-exited\n")
    r = _resolve(tab)
    assert (r.session_id, r.source, r.is_guess) == ("sess-live", "process", False)
    assert r.cmd_args == ["claude", "--resume", "sess-live"]


def test_argv_names_the_session_when_there_is_no_process_record() -> None:
    r = _resolve(_tab(_shell_and("/opt/homebrew/bin/codex", "resume", "thread-1")))
    assert (r.provider, r.session_id, r.source, r.is_guess) == (
        Provider.CODEX, "thread-1", "argv", False,
    )


def test_argv_of_a_fork_is_not_the_live_session() -> None:
    assert core._argv_resume(Provider.CLAUDE, ["claude", "--resume", "a", "--fork-session"]) is None
    assert core._argv_resume(Provider.CODEX, ["codex", "fork", "a"]) is None
    # Another provider's flag shape is not read off this provider's argv.
    assert core._argv_resume(Provider.PI, ["pi", "--resume", "a"]) is None


def test_single_session_active_since_launch_is_a_marked_guess() -> None:
    sessions = [
        _session("before", Provider.CODEX, "/proj", START - 3600),
        _session("during", Provider.CODEX, "/proj", START + 60),
        _session("elsewhere", Provider.CODEX, "/other", START + 60),
        _session("other-cli", Provider.PI, "/proj", START + 60),
    ]
    r = _resolve(_tab(_shell_and("codex")), sessions)
    assert (r.session_id, r.source, r.is_guess) == ("during", "activity", True)


def test_several_active_sessions_are_settled_by_scrollback_among_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        _session("tab-a", Provider.CODEX, "/proj", START + 60),
        _session("tab-b", Provider.CODEX, "/proj", START + 90),
    ]
    hits = [
        # Same cwd, wrong provider: what resolved two Claude tabs to opencode.
        SearchResult(session_id="ses_x", project_path="/proj", provider=Provider.OPENCODE,
                     matched_line="x", file_path="/nope"),
        # Right provider and cwd, but not active under this process.
        SearchResult(session_id="old", project_path="/proj", provider=Provider.CODEX,
                     matched_line="x", file_path="/nope"),
        SearchResult(session_id="tab-a", project_path="/proj", provider=Provider.CODEX,
                     matched_line="x", file_path="/nope"),
    ]
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: hits)
    r = _resolve(_tab(_shell_and("codex"), scrollback=LINE), sessions)
    assert (r.session_id, r.source, r.is_guess) == ("tab-a", "search", True)


def test_several_active_sessions_and_no_scrollback_hit_stay_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        _session("tab-a", Provider.CODEX, "/proj", START + 60),
        _session("tab-b", Provider.CODEX, "/proj", START + 90),
    ]
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [])
    assert _resolve(_tab(_shell_and("codex"), scrollback=LINE), sessions) is None


def test_search_for_a_live_agent_never_returns_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [SearchResult(session_id="ses_x", project_path="/proj", provider=Provider.OPENCODE,
                         matched_line="x", file_path="/nope")]
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: hits)
    assert _resolve(_tab(_shell_and("pi"), scrollback=LINE), []) is None


def test_tab_with_no_agent_still_uses_its_scrollback() -> None:
    tab = _tab(_shell_and("vim"), scrollback="$ claude --resume sess-exited\n")
    r = _resolve(tab)
    assert (r.session_id, r.source, r.is_guess) == ("sess-exited", "explicit", False)


def test_unknown_process_start_makes_no_activity_guess() -> None:
    sessions = [_session("during", Provider.CODEX, "/proj", START + 60)]
    procs = [_proc(200, 1, "codex", start=None)]
    assert _resolve(_tab(procs), sessions) is None


# ----- phrases, model, plan -------------------------------------------------


def test_candidate_phrases_are_always_literal_queries() -> None:
    from sesh.search import _is_literal

    text = "\n".join([
        "Next, add an sshd AllowGroups allowlist that excludes the departing teammate. (turn off",
        "approve the fixes, or start the circuits tutorial before inserting those nine cards. (disable",
        "is it done? no. (a) (b) [c] {d} e|f",
    ])
    phrases = core._candidate_phrases(text)
    assert phrases and all(_is_literal(p) for p in phrases)
    assert "Next, add an sshd AllowGroups allowlist that" in phrases


def test_resume_round_trips_new_sources_and_reports_guess() -> None:
    for source, guess in [("process", False), ("argv", False), ("explicit", False),
                          ("activity", True), ("search", True)]:
        d = make_snapshot_resume(source=source).to_dict()
        assert d["guess"] is guess
        assert core.SnapshotResume.from_dict(d).source == source
    assert core.SnapshotResume.from_dict({
        "provider": "claude", "session_id": "a", "source": "bogus",
    }).source == "explicit"


def test_restore_plan_marks_a_guessed_session(tmp_path: Path) -> None:
    snap = make_snapshot(tabs=[
        make_snapshot_tab(tab=1, cwd=str(tmp_path), resume=make_snapshot_resume(source="process")),
        make_snapshot_tab(tab=2, cwd=str(tmp_path), resume=make_snapshot_resume(source="activity")),
    ])
    sure, guessed = core.build_restore_plan(snap).items
    assert not sure.guess and "guessed" not in sure.label
    assert guessed.guess and guessed.label.endswith("(guessed session)")
    assert guessed.to_dict()["guess"] is True
