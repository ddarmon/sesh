from __future__ import annotations

from sesh.models import Provider
from sesh.providers import pi
from tests.helpers import make_session, write_jsonl


def test_delete_session_unlinks_source_file(tmp_path) -> None:
    file_path = tmp_path / "session.jsonl"
    other = tmp_path / "other.jsonl"
    write_jsonl(file_path, [{"type": "session", "id": "s1", "cwd": "/repo"}])
    write_jsonl(other, [{"type": "session", "id": "s2", "cwd": "/repo"}])

    pi.PiProvider().delete_session(
        make_session(provider=Provider.PI, source_path=str(file_path))
    )
    assert not file_path.exists()
    assert other.exists()


def test_delete_session_missing_file_is_no_op(tmp_path) -> None:
    file_path = tmp_path / "gone.jsonl"
    pi.PiProvider().delete_session(
        make_session(provider=Provider.PI, source_path=str(file_path))
    )
    assert not file_path.exists()


def test_delete_session_no_source_path_is_no_op() -> None:
    pi.PiProvider().delete_session(
        make_session(provider=Provider.PI, source_path=None)
    )
