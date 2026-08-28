"""
title: CAAL Tool Offload
author: AbdulShahzeb
version: 0.2
required_open_webui_version: 0.3.9
"""

from typing import Optional

import requests
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        caal_url: str = Field(
            default="http://172.17.0.1:8889",
            description="CAAL server URL (host:port, no trailing slash)",
        )
        timeout: int = Field(
            default=120,
            description="Request timeout in seconds",
        )
        tool_keywords: str = Field(
            default="hey caal",
            description="Comma-separated phrases that trigger CAAL (case-insensitive)",
        )
        reload_keywords: str = Field(
            default="reload caal,caal reload",
            description="Comma-separated phrases that trigger CAAL reload (case-insensitive)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _should_route(self, message: str) -> bool:
        message_lower = message.lower()
        keywords = [k.strip().lower() for k in self.valves.tool_keywords.split(",")]
        return any(kw in message_lower for kw in keywords if kw)

    def _should_reload(self, message: str) -> bool:
        message_lower = message.lower().strip()
        keywords = [k.strip().lower() for k in self.valves.reload_keywords.split(",")]
        return any(kw == message_lower for kw in keywords if kw)

    def _reload_caal(self) -> str:
        try:
            resp = requests.post(
                f"{self.valves.caal_url}/api/chat/reload",
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return (
                    f"CAAL reloaded. Provider: {data.get('llm_provider', '?')}, "
                    f"model: {data.get('llm_model', '?')}, "
                    f"tools: {data.get('tools_loaded', '?')}, "
                    f"sessions cleared: {data.get('sessions_cleared', '?')}"
                )
            else:
                return f"[CAAL reload error: HTTP {resp.status_code}]"
        except requests.exceptions.ConnectionError:
            return f"[CAAL reload error: cannot reach {self.valves.caal_url}]"
        except Exception as e:
            return f"[CAAL reload error: {e}]"

    def _call_caal(self, message: str) -> str:
        try:
            resp = requests.post(
                f"{self.valves.caal_url}/api/chat",
                json={
                    "text": message,
                    "reuse_session": True,
                },
                timeout=self.valves.timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", "")
            else:
                return f"[CAAL error: HTTP {resp.status_code}]"

        except requests.exceptions.Timeout:
            return "[CAAL error: request timed out]"
        except requests.exceptions.ConnectionError:
            return f"[CAAL error: cannot reach {self.valves.caal_url}]"
        except Exception as e:
            return f"[CAAL error: {e}]"

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        if not messages:
            return body

        last = messages[-1]
        if last.get("role") != "user":
            return body

        user_text = last.get("content", "")

        if self._should_reload(user_text):
            print("[CAAL Filter] reload keyword detected, reloading CAAL")
            caal_response = self._reload_caal()
        elif self._should_route(user_text):
            print("[CAAL Filter] keyword detected, routing to CAAL")
            caal_response = self._call_caal(user_text)
        else:
            return body

        last["content"] = (
            "OUTPUT ONLY THE FOLLOWING TEXT EXACTLY AS WRITTEN. "
            "DO NOT ADD ANYTHING. DO NOT REMOVE ANYTHING. "
            "DO NOT PARAPHRASE. COPY THIS EXACTLY:\n\n"
            f"{caal_response}"
        )
        return body

    def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body
