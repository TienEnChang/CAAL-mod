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
