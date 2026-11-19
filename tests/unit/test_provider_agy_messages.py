"""Transcript parsing for the Antigravity CLI (agy) provider."""

from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers.agy import (
    AgyProvider,
    read_transcript,
    records_to_messages,
    split_user_input,
)
from tests.helpers import (
    agy_reply,
    agy_step,
    agy_user,
    make_session,
    write_agy_conversation,
    write_jsonl,
)

CONV = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


def _session(transcript: Path, conversation_id: str = CONV):
    return make_session(
        id=conversation_id, provider=Provider.AGY, source_path=str(transcript)
    )


def test_split_user_input_separates_prompt_from_injected_blocks() -> None:
    prompt, rest = split_user_input(
        "<USER_REQUEST>\nHi!\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nThe time is now.\n</ADDITIONAL_METADATA>"
    )
    assert prompt == "Hi!"
    assert rest.startswith("<ADDITIONAL_METADATA>")


def test_split_user_input_keeps_a_prompt_that_quotes_the_closing_tag() -> None:
    prompt, rest = split_user_input(
        "<USER_REQUEST>\nWhat does </USER_REQUEST> mean?\n</USER_REQUEST>"
    )
    assert prompt == "What does </USER_REQUEST> mean?"
    assert rest == ""


def test_split_user_input_passes_unwrapped_content_through() -> None:
    assert split_user_input("plain prompt") == ("plain prompt", "")


def test_messages_cover_text_thinking_tools_and_hidden_records(tmp_agy_dir) -> None:
    transcript = write_agy_conversation(tmp_agy_dir, records=[
        agy_user(0, "Read note.txt"),
        agy_reply(
            1,
            thinking="Plan the read.\n\n",
            tool_calls=[
                {"name": "read_file", "args": {"Path": "/repo/note.txt"}},
                {"name": "run_command", "args": {"CommandLine": "ls"}},
            ],
        ),
        agy_step(2, "GENERIC", content="1: hello"),
        agy_step(3, "GENERIC", content="note.txt"),
        agy_reply(4, "The note says hello."),
        agy_step(5, "SYSTEM_MESSAGE", content=(
            "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-19T02:04:17Z "
            "sender=system priority=MESSAGE_PRIORITY_LOW content=[Notice] restart\n"
            "</SYSTEM_MESSAGE>"
        )),
    ])

    messages = AgyProvider().get_messages(_session(transcript))

    assert [(m.role, m.content_type, m.is_system) for m in messages] == [
        ("user", "text", False),
        ("user", "text", True),          # injected metadata block
        ("assistant", "thinking", False),
        ("assistant", "tool_use", False),
        ("assistant", "tool_use", False),
        ("tool", "tool_result", False),
        ("tool", "tool_result", False),
        ("assistant", "text", False),
        ("system", "text", True),
    ]
    assert messages[0].content == "Read note.txt"
    assert messages[2].thinking == "Plan the read."
    assert messages[3].tool_name == "read_file"
    assert '"Path": "/repo/note.txt"' in messages[3].tool_input
    # Results carry no tool name of their own; they pair with calls in order.
    assert [m.tool_name for m in messages[5:7]] == ["read_file", "run_command"]
    assert messages[5].tool_output == "1: hello"
    assert messages[7].content == "The note says hello."
    assert messages[0].timestamp is not None


def test_unpaired_tool_result_and_unknown_record_types_are_kept(tmp_agy_dir) -> None:
    transcript = write_agy_conversation(tmp_agy_dir, records=[
        agy_user(0, "Go"),
        agy_step(1, "GENERIC", content="orphan output"),
        agy_step(2, "CHECKPOINT", content="compacted summary"),
        agy_step(3, "EPHEMERAL"),
    ])

    messages = AgyProvider().get_messages(_session(transcript))

    assert messages[2].content_type == "tool_result"
    assert messages[2].tool_name == ""
    assert messages[3].is_system and messages[3].content == "compacted summary"
    assert len(messages) == 4  # the content-less record adds nothing


def test_rewind_drops_the_abandoned_steps_but_keeps_their_tokens(tmp_agy_dir) -> None:
    """A rewind appends records that restart at an earlier ``step_index``."""
    transcript = write_agy_conversation(tmp_agy_dir, records=[
        agy_user(0, "one"), agy_reply(1, "ONE", output_tokens=1),
        agy_user(2, "two"), agy_reply(3, "TWO", output_tokens=2),
        agy_user(4, "three"), agy_reply(5, "THREE", output_tokens=4),
        agy_user(2, "four"), agy_reply(3, "FOUR", output_tokens=8),
    ])

    records, totals = read_transcript(transcript)
    visible = [m.content for m in records_to_messages(records) if not m.is_system]

    assert visible == ["one", "ONE", "four", "FOUR"]
    assert totals["output_tokens"] == 15
    assert totals["cumulative_input_tokens"] == 400


def test_malformed_lines_are_skipped(tmp_agy_dir) -> None:
    transcript = write_agy_conversation(tmp_agy_dir)
    with open(transcript, "a") as f:
        f.write("not json\n[1, 2]\n\n")

    messages = AgyProvider().get_messages(_session(transcript))

    assert [m.content for m in messages if not m.is_system] == ["Fix the parser", "Done."]


def test_missing_transcript_yields_no_messages(tmp_agy_dir) -> None:
    session = _session(tmp_agy_dir / "brain" / CONV / "nope.jsonl")
    assert AgyProvider().get_messages(session) == []


def test_load_subagents_renders_the_parent_message_as_the_prompt(tmp_agy_dir) -> None:
    parent = write_agy_conversation(tmp_agy_dir, records=[
        agy_user(0, "Spawn a helper"),
        agy_reply(1, tool_calls=[{"name": "invoke_subagent", "args": {}}]),
        agy_step(2, "GENERIC", content="Created the following subagent"),
        agy_step(3, "SYSTEM_MESSAGE", content=(
            f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-19T02:04:17Z "
            f"sender={CHILD} priority=MESSAGE_PRIORITY_HIGH content=pong\n"
            "</SYSTEM_MESSAGE>"
        )),
        agy_reply(4, "The helper said pong."),
    ])
    write_agy_conversation(
        tmp_agy_dir, conversation_id=CHILD, parent_id=CONV,
        agent_name="research", role="Pong Responder", title="", preview="",
        records=[
            agy_step(0, "SYSTEM_MESSAGE", created_at="2026-11-19T02:00:02Z", content=(
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
                f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-19T02:04:11Z "
                f"sender={CONV} priority=MESSAGE_PRIORITY_HIGH content=Reply pong\n"
                "</SYSTEM_MESSAGE>"
            )),
            agy_reply(1, "Replied.", output_tokens=7),
        ],
    )

    provider = AgyProvider()
    loaded = provider.load_subagents(_session(parent))

    assert len(loaded) == 1
    meta, messages = loaded[0]
    assert meta.agent_id == CHILD
    assert meta.agent_type == "research"
    assert meta.description == "Pong Responder"
    assert meta.message_count == 2
    assert meta.output_tokens == 7
    assert meta.first_timestamp is not None
    assert [(m.role, m.content, m.is_system) for m in messages] == [
        ("user", "Reply pong", False),
        ("assistant", "Replied.", False),
    ]
    # In the parent the child's message stays a hidden record.
    parent_messages = provider.get_messages(_session(parent))
    assert [m.is_system for m in parent_messages if "pong" in m.content][:1] == [True]


def test_load_subagents_includes_nested_descendants(tmp_agy_dir) -> None:
    grandchild = "33333333-3333-4333-8333-333333333333"
    parent = write_agy_conversation(tmp_agy_dir)
    write_agy_conversation(tmp_agy_dir, conversation_id=CHILD, parent_id=CONV)
    write_agy_conversation(
        tmp_agy_dir, conversation_id=grandchild, parent_id=CHILD,
        records=[agy_reply(0, "deep", created_at="2026-11-19T03:00:00Z")],
    )

    loaded = AgyProvider().load_subagents(_session(parent))

    assert [meta.agent_id for meta, _messages in loaded] == [CHILD, grandchild]


def test_write_jsonl_fallback_to_truncated_transcript(tmp_agy_dir) -> None:
    """Without ``transcript_full.jsonl`` the truncated transcript is used."""
    logs = tmp_agy_dir / "brain" / CONV / ".system_generated" / "logs"
    write_jsonl(logs / "transcript.jsonl", [agy_user(0, "hello"), agy_reply(1, "hi")])

    provider = AgyProvider()
    assert provider.transcript_path(CONV) == str(logs / "transcript.jsonl")
