from __future__ import annotations

from argparse import Namespace

import pytest

from sesh import cli, diagnostics


def _args(**overrides):
    values = {"aggregation_root": None, "provider": None, "human": False, "strict": False}
    values.update(overrides)
    return Namespace(**values)


def test_cmd_doctor_json_by_default(monkeypatch, capsys):
    report = {"status": "ok"}
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda **kwargs: report)

    cli.cmd_doctor(_args(provider="claude"))

    assert capsys.readouterr().out == '{\n  "status": "ok"\n}\n'


def test_cmd_doctor_human_and_strict(monkeypatch, capsys):
    report = {"status": "warning"}
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda **kwargs: report)
    monkeypatch.setattr(diagnostics, "format_diagnostics_text", lambda value: "pretty report")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(_args(human=True, strict=True))

    assert exc.value.code == 1
    assert capsys.readouterr().out == "pretty report\n"
