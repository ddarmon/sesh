"""Pilot tests for the delete dialog's "also delete prompt history" checkbox."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Checkbox

from sesh.app import ConfirmDeleteScreen

pytestmark = pytest.mark.integration


class _Host(App):
    def __init__(self, screen: ConfirmDeleteScreen) -> None:
        super().__init__()
        self.dialog = screen
        self.result: bool | None = None

    def on_mount(self) -> None:
        self.push_screen(self.dialog, lambda confirmed: setattr(self, "result", confirmed))


@pytest.mark.asyncio
async def test_checkbox_starts_unchecked() -> None:
    host = _Host(ConfirmDeleteScreen("summary", history_option=True))
    async with host.run_test() as pilot:
        await pilot.pause()
        assert host.dialog.query_one("#confirm-history", Checkbox).value is False
        await pilot.click("#confirm-yes")
        await pilot.pause()
    assert host.result is True
    assert host.dialog.include_history is False


@pytest.mark.asyncio
async def test_checked_box_reports_include_history() -> None:
    host = _Host(ConfirmDeleteScreen("summary", history_option=True))
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#confirm-history")
        await pilot.click("#confirm-yes")
        await pilot.pause()
    assert host.result is True
    assert host.dialog.include_history is True


@pytest.mark.asyncio
async def test_without_history_option_there_is_no_checkbox() -> None:
    host = _Host(ConfirmDeleteScreen("Snapshot abc"))
    async with host.run_test() as pilot:
        await pilot.pause()
        assert not host.dialog.query("#confirm-history")
        await pilot.click("#confirm-no")
        await pilot.pause()
    assert host.result is False
    assert host.dialog.include_history is False
