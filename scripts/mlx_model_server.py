#!/usr/bin/env python3
"""Run mlx-lm's server with CAAL's memory budget, cache control, and downloads.

mlx-lm already provides the three things CAAL asks of a model server:
``/v1/chat/completions`` with tools and streaming, ``/v1/models`` listing every
MLX model in the Hugging Face cache, and per-request model switching - its
``ModelProvider.load`` drops the resident model whenever a request names a
different one. This wrapper adds only what CAAL needs on top and is deliberately
model-agnostic: nothing here knows or cares which model is loaded.

  POST /v1/cache/clear      release the prompt cache without unloading weights
  GET  /v1/memory           MLX's own allocation counters
  POST /v1/model/unload     drop the weights; the next request reloads them
  POST /v1/models/download  fetch a repo into the cache so it joins /v1/models
  GET  /v1/models/download  progress of downloads started by this process
"""

from __future__ import annotations

import json
import logging
import os
import threading

from mlx_lm import server

GIB = 1024**3

# mlx-lm's main() wires max_recommended_working_set_size - 11.84 GiB of a 16 GiB
# machine. Wired pages cannot be paged out, so as the KV cache grows the kernel
# can only make room by evicting every other process to swap. That is why swap
# climbs while memory_pressure still reports healthy headroom: evicting frees
# pages, so the "free percentage" rises as the cost is paid in swap.
#
# Clamping the limits keeps the model inside a budget that leaves room for the
# rest of CAAL, so it reclaims its own cache instead of displacing everything
# else. The CAAL_QWEN_* names are the pre-LM-Studio spelling, still honoured so
# an existing .env keeps working.
def _budget(name: str, legacy: str, default: str) -> int:
    raw = os.getenv(name) or os.getenv(legacy) or default
    return int(float(raw) * GIB)


WIRED_LIMIT = _budget("CAAL_MODEL_WIRED_LIMIT_GB", "CAAL_QWEN_WIRED_LIMIT_GB", "6")
# Must stay above the guard's trip (CAAL_MODEL_MEMORY_TRIP_GB, 8 GiB), or the
# trip is unreachable: MLX would refuse allocations at its own ceiling first
# and the guard would never observe the value that ends the call.
MEMORY_LIMIT = _budget("CAAL_MODEL_MEMORY_LIMIT_GB", "CAAL_QWEN_MEMORY_LIMIT_GB", "10")

_original_set_wired_limit = server.mx.set_wired_limit


def _clamped_set_wired_limit(limit: int) -> int:
    """Cap mlx-lm's wired limit at the CAAL budget."""
    capped = min(limit, WIRED_LIMIT)
    if capped != limit:
        logging.info(
            "Wired limit capped at %.2f GiB (mlx-lm asked for %.2f GiB)",
            capped / GIB,
            limit / GIB,
        )
    return _original_set_wired_limit(capped)


server.mx.set_wired_limit = _clamped_set_wired_limit


# =============================================================================
# Reclaim the outgoing model on a switch
# =============================================================================
# ModelProvider._load drops the resident model and loads the new one. The old
# weights stop being live, but MLX keeps their buffers in its allocator pool for
# reuse rather than returning them to the OS - so after a switch the process
# holds two models' worth of memory while only one is usable. A 4B model at
# 4-bit strands about 2 GiB that way, which is indistinguishable from a leak in
# Activity Monitor and survives until something calls clear_cache.
#
# The call-teardown clear cannot cover this: a switch happens on the next
# request, after that clear has already run.

_original_provider_load = server.ModelProvider._load


def _load_and_reclaim(self, *args, **kwargs):
    result = _original_provider_load(self, *args, **kwargs)
    reclaimed = server.mx.get_cache_memory()
    if reclaimed:
        server.mx.clear_cache()
        logging.info(
            "Released %.2f GiB held from the previous model", reclaimed / GIB
        )
    return result


server.ModelProvider._load = _load_and_reclaim


# =============================================================================
# Model downloads
# =============================================================================
# Adding a model option is "put it where /v1/models looks", and /v1/models looks
# at the Hugging Face cache. Downloads run on their own thread because a large
# repo outlives any sensible HTTP timeout; the client polls the status map
# instead of holding a connection open. ThreadingHTTPServer keeps inference
# responsive throughout.

_downloads: dict[str, dict[str, object]] = {}
_downloads_lock = threading.Lock()


def _record(repo_id: str, **fields: object) -> None:
    with _downloads_lock:
        _downloads.setdefault(repo_id, {})
        _downloads[repo_id].update(fields)


def _download(repo_id: str) -> None:
    from huggingface_hub import snapshot_download

    try:
        path = snapshot_download(repo_id)
    except Exception as error:  # network, auth, unknown repo, disk
        logging.error("Download of %s failed: %s", repo_id, error)
        _record(repo_id, state="failed", error=str(error))
        return
    logging.info("Downloaded %s to %s", repo_id, path)
    _record(repo_id, state="ready", path=str(path), error=None)


def _start_download(repo_id: str) -> str:
    with _downloads_lock:
        current = _downloads.get(repo_id, {}).get("state")
        if current == "downloading":
            return "downloading"
        _downloads[repo_id] = {"state": "downloading", "error": None}
    threading.Thread(target=_download, args=(repo_id,), daemon=True).start()
    return "started"


# =============================================================================
# Endpoints
# =============================================================================

_original_do_get = server.APIHandler.do_GET
_original_do_post = server.APIHandler.do_POST
_admin_lock = threading.Lock()

for required_method in ("stop_and_join", "_generate"):
    if not hasattr(server.ResponseGenerator, required_method):
        raise RuntimeError(
            f"mlx-lm 0.31.3 compatibility failure: ResponseGenerator.{required_method}"
        )


def _restart_generation_thread(response_generator) -> None:
    response_generator._stop = False
    response_generator._generation_thread = threading.Thread(
        target=response_generator._generate,
        daemon=False,
    )
    response_generator._generation_thread.start()


def _clear_after_generation_stops(response_generator, *, unload: bool) -> int:
    """Serialize administration after active generation leaves MLX state alone."""
    for required_attribute in (
        "prompt_cache",
        "_state_machine_cache",
        "_generation_thread",
        "model_provider",
    ):
        if not hasattr(response_generator, required_attribute):
            raise RuntimeError(
                "mlx-lm 0.31.3 compatibility failure: "
                f"ResponseGenerator.{required_attribute}"
            )
    with _admin_lock:
        response_generator.stop_and_join()
        provider = response_generator.model_provider
        response_generator.prompt_cache.trim_to(n_sequences=0)
        response_generator._state_machine_cache.clear()
        if unload:
            provider._model_map["default_model"] = None
            provider.cli_args.model = None
            provider.model_key = None
            provider.model = None
            provider.tokenizer = None
            provider.draft_model = None
        server.mx.clear_cache()
        active_bytes = int(server.mx.get_active_memory())
        _restart_generation_thread(response_generator)
        return active_bytes


def _write_json(handler: server.APIHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload).encode()
    handler._set_completion_headers(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _do_get(self) -> None:
    """Expose MLX's own memory use - the only leading signal for model pressure.

    Free-memory percentage and swap both lag: macOS swaps while it still
    reports healthy headroom, and swap growth is by definition after the fact.
    MLX's active allocation is measurable before either moves.
    """
    if self.path == "/v1/models/download":
        with _downloads_lock:
            _write_json(self, {"downloads": json.loads(json.dumps(_downloads))})
        return

    if self.path != "/v1/memory":
        _original_do_get(self)
        return

    _write_json(
        self,
        {
            "active_bytes": server.mx.get_active_memory(),
            "cache_bytes": server.mx.get_cache_memory(),
            "peak_bytes": server.mx.get_peak_memory(),
            "wired_limit_bytes": WIRED_LIMIT,
            "memory_limit_bytes": MEMORY_LIMIT,
            "prompt_cache_sequences": len(self.response_generator.prompt_cache),
            "prompt_cache_bytes": self.response_generator.prompt_cache.nbytes,
        },
    )


server.APIHandler.do_GET = _do_get


def _do_post(self) -> None:
    if self.path == "/v1/models/download":
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            repo_id = str(payload["repo_id"]).strip()
        except (ValueError, KeyError, TypeError):
            _write_json(self, {"error": "Body must be {\"repo_id\": \"org/name\"}"}, 400)
            return
        if not repo_id:
            _write_json(self, {"error": "repo_id must not be empty"}, 400)
            return
        state = _start_download(repo_id)
        _write_json(self, {"repo_id": repo_id, "state": state}, 202)
        return

    if self.path == "/v1/model/unload":
        # The hard-reset primitive, replacing LM Studio's out-of-process
        # unload/load pair. Clearing ModelProvider's references is necessary but
        # not sufficient: mlx-lm's generation thread keeps the model bound in
        # `current_model` between requests, so while that thread lives the
        # weights stay resident no matter what the provider holds. Cycling the
        # thread is what actually frees them.
        #
        # The default model is dropped first so the restarted thread's
        # load_default() is a no-op - otherwise it would immediately reload the
        # model we are trying to release. After a hard reset the server has no
        # default and every request must name its model, which CAAL always does.
        rg = self.response_generator
        before = server.mx.get_active_memory()
        after = _clear_after_generation_stops(rg, unload=True)
        logging.warning(
            "Unloaded model, reclaiming %.2f GiB; next request reloads it",
            (before - after) / GIB,
        )
        _write_json(
            self,
            {
                "status": "ok",
                "active_bytes": after,
                "reclaimed_bytes": max(before - after, 0),
            },
        )
        return

    if self.path != "/v1/cache/clear":
        _original_do_post(self)
        return

    _clear_after_generation_stops(self.response_generator, unload=False)
    self.response_generator._log_cache_stats()
    logging.info("Cleared prompt cache after call end")

    _write_json(self, {"status": "ok", "sequences": 0})


server.APIHandler.do_POST = _do_post


if __name__ == "__main__":
    # server.main() configures logging itself, but basicConfig is a no-op once
    # the root logger has handlers. Configuring first guarantees INFO reaches
    # stderr, which start-native.sh redirects into .native/logs/model.log.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    # main() sets the wired limit itself (through the clamp installed above) but
    # never touches the memory limit, which defaults to 1.5x the working set -
    # more than this machine physically has.
    server.mx.set_memory_limit(MEMORY_LIMIT)
    logging.info(
        "Model memory budget: wired <= %.2f GiB, allocations <= %.2f GiB",
        WIRED_LIMIT / GIB,
        MEMORY_LIMIT / GIB,
    )
    server.main()
