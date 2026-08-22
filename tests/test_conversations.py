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
