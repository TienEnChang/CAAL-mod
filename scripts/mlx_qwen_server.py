#!/usr/bin/env python3
"""Run mlx-lm's server with a localhost prompt-cache cleanup endpoint."""

from __future__ import annotations

import json
import logging
import os

from mlx_lm import server

GIB = 1024**3

# mlx-lm's main() wires max_recommended_working_set_size - 11.84 GiB of a 16 GiB
# machine. Wired pages cannot be paged out, so as Qwen's KV cache grows the
# kernel can only make room by evicting every other process to swap. That is why
# swap climbs while memory_pressure still reports healthy headroom: evicting
# frees pages, so the "free percentage" rises as the cost is paid in swap.
#
# Clamping the limits keeps Qwen inside a budget that leaves room for the rest of
# CAAL, so it reclaims its own cache instead of displacing everything else.
QWEN_WIRED_LIMIT = int(float(os.getenv("CAAL_QWEN_WIRED_LIMIT_GB", "6")) * GIB)
QWEN_MEMORY_LIMIT = int(float(os.getenv("CAAL_QWEN_MEMORY_LIMIT_GB", "7")) * GIB)

_original_set_wired_limit = server.mx.set_wired_limit


def _clamped_set_wired_limit(limit: int) -> int:
    """Cap mlx-lm's wired limit at the CAAL budget."""
    capped = min(limit, QWEN_WIRED_LIMIT)
    if capped != limit:
        logging.info(
            "Wired limit capped at %.2f GiB (mlx-lm asked for %.2f GiB)",
            capped / GIB,
            limit / GIB,
        )
    return _original_set_wired_limit(capped)


server.mx.set_wired_limit = _clamped_set_wired_limit


_original_do_get = server.APIHandler.do_GET
_original_do_post = server.APIHandler.do_POST


def _do_get(self) -> None:
    """Expose MLX's own memory use - the only leading signal for Qwen pressure.

    Free-memory percentage and swap both lag: macOS swaps while it still
    reports healthy headroom, and swap growth is by definition after the fact.
    MLX's active allocation is measurable before either moves.
    """
    if self.path != "/v1/memory":
        _original_do_get(self)
        return

    body = json.dumps({
        "active_bytes": server.mx.get_active_memory(),
        "cache_bytes": server.mx.get_cache_memory(),
        "peak_bytes": server.mx.get_peak_memory(),
        "wired_limit_bytes": QWEN_WIRED_LIMIT,
        "memory_limit_bytes": QWEN_MEMORY_LIMIT,
        "prompt_cache_sequences": len(self.response_generator.prompt_cache),
        "prompt_cache_bytes": self.response_generator.prompt_cache.nbytes,
    }).encode()
    self._set_completion_headers(200)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
    self.wfile.flush()


server.APIHandler.do_GET = _do_get


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
    # main() sets the wired limit itself (through the clamp installed above) but
    # never touches the memory limit, which defaults to 1.5x the working set -
    # more than this machine physically has.
    server.mx.set_memory_limit(QWEN_MEMORY_LIMIT)
    logging.info(
        "Qwen memory budget: wired <= %.2f GiB, allocations <= %.2f GiB",
        QWEN_WIRED_LIMIT / GIB,
        QWEN_MEMORY_LIMIT / GIB,
    )
    server.main()
