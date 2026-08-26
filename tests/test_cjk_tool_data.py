"""Non-ASCII tool data must reach the LLM as real characters.

json.dumps defaults to ensure_ascii=True, which re-encodes CJK as \\uXXXX
escapes. Qwen reads those as literal escape text: given an escaped Traditional
Chinese calendar entry it dropped the characters entirely and mistranslated the
summary, while the same data sent raw was reproduced exactly.
"""

import json

from caal.llm import ToolDataCache

TRADITIONAL = "第三次請求撤銷"
SIMPLIFIED = "第三次请求撤销"


def test_tool_data_cache_preserves_traditional_chinese():
    cache = ToolDataCache(max_entries=3)
    cache.add("google_calendar", {"events": [{"summary": TRADITIONAL}]}, arguments={})

    message = cache.get_context_message()

    assert TRADITIONAL in message
    assert "\\u" not in message, "CJK was escaped instead of passed through"


def test_tool_arguments_preserve_non_ascii():
    cache = ToolDataCache(max_entries=3)
    cache.add("search", {"ok": True}, arguments={"query": TRADITIONAL})

    message = cache.get_context_message()

    assert TRADITIONAL in message
    assert "\\u" not in message


def test_traditional_is_not_converted_to_simplified():
    """The two scripts are distinct; silently normalising loses the original."""
    cache = ToolDataCache(max_entries=3)
    cache.add("google_calendar", {"summary": TRADITIONAL}, arguments={})

    message = cache.get_context_message()

    assert SIMPLIFIED not in message


def test_escaping_is_what_the_default_would_have_done():
    """Guards the premise: plain json.dumps mangles this, which is the bug."""
    assert "\\u" in json.dumps({"summary": TRADITIONAL})
    assert "\\u" not in json.dumps({"summary": TRADITIONAL}, ensure_ascii=False)
