#!/usr/bin/env python3
"""Run mlx-lm's server with a localhost prompt-cache cleanup endpoint."""

from __future__ import annotations

import json
import logging

from mlx_lm import server

_original_do_post = server.APIHandler.do_POST


def _do_post(self) -> None:
    if self.path != "/v1/cache/clear":
        _original_do_post(self)
        return

    self.response_generator.prompt_cache.trim_to(n_sequences=0)
    self.response_generator._state_machine_cache.clear()
    server.mx.clear_cache()
    self.response_generator._log_cache_stats()
    logging.info("Cleared prompt cache after call end")

    body = json.dumps({"status": "ok", "sequences": 0}).encode()
    self._set_completion_headers(200)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
    self.wfile.flush()


server.APIHandler.do_POST = _do_post


if __name__ == "__main__":
    # server.main() configures logging itself, but basicConfig is a no-op once
    # the root logger has handlers. Configuring first guarantees INFO reaches
    # stderr, which start-native.sh redirects into .native/logs/qwen.log - the
    # file --memory-report parses for "Prompt Cache: N sequences" lines.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    server.main()
