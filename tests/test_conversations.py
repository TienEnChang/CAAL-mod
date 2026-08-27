import sqlite3

from caal.conversations import ConversationStore


def test_conversation_lifecycle(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")

    first_id = store.ensure_active()
    store.append_message(
        first_id,
        role="user",
        content="Please check the weather in Taipei tomorrow",
        message_id="user-1",
        created_at=10,
    )
    store.append_message(
        first_id,
        role="assistant",
        content="I will check that for you.",
        message_id="assistant-1",
        created_at=11,
    )

    listing = store.list()
    assert listing["active_id"] == first_id
    assert listing["conversations"][0]["title"] == (
        "Please check the weather in Taipei tomorrow"
    )
    assert listing["conversations"][0]["message_count"] == 2

    detail = store.detail(first_id)
    assert [message["role"] for message in detail["messages"]] == [
        "user",
        "assistant",
    ]
    assert store.context_messages(first_id) == [
        {"role": "user", "content": "Please check the weather in Taipei tomorrow"},
        {"role": "assistant", "content": "I will check that for you."},
    ]


def test_delete_active_selects_remaining_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    first_id = store.ensure_active()
    second_id = store.create("Second")

    replacement_id = store.delete(second_id)

    assert replacement_id == first_id
    assert store.list()["active_id"] == first_id


def test_rename_conversation_persists_and_prevents_automatic_retitle(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()

    assert store.rename(conversation_id, "  Taipei planning  ") == "Taipei planning"
    store.append_message(conversation_id, role="user", content="This should not replace the title")

    assert store.detail(conversation_id)["conversation"]["title"] == "Taipei planning"


def test_rename_rejects_blank_title(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()

    try:
        store.rename(conversation_id, "   ")
    except ValueError as error:
        assert str(error) == "Conversation title cannot be empty"
    else:
        raise AssertionError("rename accepted a blank conversation title")


def test_duplicate_message_id_is_ignored(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()

    store.append_message(
        conversation_id,
        role="user",
        content="Hello",
        message_id="same-id",
    )
    store.append_message(
        conversation_id,
        role="user",
        content="Hello again",
        message_id="same-id",
    )

    assert len(store.detail(conversation_id)["messages"]) == 1


def test_message_requires_existing_conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")

    try:
        store.append_message("missing", role="user", content="Hello")
    except KeyError as error:
        assert error.args == ("missing",)
    else:
        raise AssertionError("append_message accepted a missing conversation")


def test_context_window_obeys_message_and_character_budgets(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    for index, content in enumerate(["one", "two", "three", "four"]):
        store.append_message(
            conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=content,
            message_id=f"message-{index}",
        )

    by_count = store.context_window(conversation_id, limit=2, max_chars=100)
    assert by_count.messages == [
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    assert by_count.truncated is True

    by_chars = store.context_window(conversation_id, limit=4, max_chars=9)
    assert by_chars.messages == [
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    assert by_chars.truncated is True


def test_summary_state_and_source_exclude_tool_rows(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="Remember Taipei")
    store.append_message(conversation_id, role="tool", content="Used weather")
    store.append_message(conversation_id, role="assistant", content="I will remember")
    window = store.context_window(conversation_id, limit=1, max_chars=100)

    pending = store.unsummarized_messages(
        conversation_id,
        after_rowid=0,
        before_rowid=window.oldest_rowid,
        max_chars=100,
    )
    assert [(message["role"], message["content"]) for message in pending] == [
        ("user", "Remember Taipei")
    ]

    store.save_summary(
        conversation_id,
        "The user asked CAAL to remember Taipei.",
        through_rowid=pending[-1]["rowid"],
    )
    assert store.get_summary(conversation_id) == (
        "The user asked CAAL to remember Taipei.",
        pending[-1]["rowid"],
    )


def test_rehydration_uses_only_bounded_messages_after_summary_checkpoint(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="Already summarized")
    first = store.unsummarized_messages(
        conversation_id,
        after_rowid=0,
        max_chars=100,
    )[0]
    store.save_summary(
        conversation_id,
        "Earlier memory",
        through_rowid=first["rowid"],
    )
    store.append_message(conversation_id, role="assistant", content="Recent answer")
    store.append_message(conversation_id, role="user", content="Latest question")

    window = store.rehydration_window(conversation_id, limit=1, max_chars=100)

    assert window.messages == [{"role": "user", "content": "Latest question"}]
    assert window.truncated is True


def test_interrupted_assistant_output_is_not_rehydrated(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="Question")
    store.append_message(
        conversation_id,
        role="assistant",
        content="Partial answer",
        metadata={"interrupted": True},
    )

    assert store.rehydration_window(conversation_id).messages == [
        {"role": "user", "content": "Question"}
    ]


def test_summary_checkpoint_compare_and_swap_rejects_stale_writer(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="First")
    first_rowid = store.unsummarized_messages(
        conversation_id,
        after_rowid=0,
        max_chars=100,
    )[0]["rowid"]

    assert store.save_summary(
        conversation_id,
        "Current",
        through_rowid=first_rowid,
        expected_through_rowid=0,
    ) is True
    assert store.save_summary(
        conversation_id,
        "Stale",
        through_rowid=first_rowid,
        expected_through_rowid=0,
    ) is False
    assert store.get_summary(conversation_id) == ("Current", first_rowid)


def test_unsummarized_stats_follow_checkpoint_and_exclude_interrupted_output(tmp_path):
    store = ConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.ensure_active()
    store.append_message(conversation_id, role="user", content="old")
    old_rowid = store.unsummarized_messages(
        conversation_id,
        after_rowid=0,
        max_chars=100,
    )[0]["rowid"]
    store.save_summary(conversation_id, "Old summary", through_rowid=old_rowid)
    store.append_message(conversation_id, role="user", content="four")
    store.append_message(
        conversation_id,
        role="assistant",
        content="ignored",
        metadata={"interrupted": True},
    )
    store.append_message(conversation_id, role="assistant", content="five!")

    assert store.unsummarized_stats(conversation_id) == (2, 9)


def test_existing_database_is_migrated_for_summaries(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    store = ConversationStore(database_path)
    conversation_id = store.ensure_active()

    assert store.get_summary(conversation_id) == (None, 0)
