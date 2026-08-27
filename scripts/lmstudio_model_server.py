#!/usr/bin/env python3
"""Keep CAAL's headless LM Studio daemon, API, and selected model ready."""

from __future__ import annotations

import argparse
import signal
import subprocess
import time
import urllib.error
import urllib.request


def run(*args: str, timeout: float = 180) -> None:
    result = subprocess.run(args, check=False, text=True, timeout=timeout)
    if result.returncode:
        raise SystemExit(result.returncode)


def unload_owned_instance(lms: str, identifier: str) -> None:
    """Unload only CAAL's instance; other LM Studio clients keep their models."""
    subprocess.run(
        [lms, "unload", identifier],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lms", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--identifier", default="caal-model")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--context-length", type=int, default=32768)
    parser.add_argument("--parallel", type=int, default=4)
    args = parser.parse_args()

    if args.parallel < 1:
        parser.error("--parallel must be at least 1")

    run(args.lms, "daemon", "up")
    run(args.lms, "server", "start", "--port", str(args.port), "--bind", "127.0.0.1")
    # A service restart or model switch must converge on one CAAL-owned
    # instance without evicting models loaded by Bionic or another client.
    unload_owned_instance(args.lms, args.identifier)
    run(
        args.lms,
        "load",
        args.model,
        "--context-length",
        str(args.context_length),
        "--parallel",
        str(args.parallel),
        "--identifier",
        args.identifier,
        "--yes",
    )

    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        if not ready(args.port):
            # An intentional guard reload briefly interrupts the API. Keep the
            # service supervisor alive and ask llmster to restore the endpoint.
            try:
                subprocess.run(
                    [
                        args.lms,
                        "server",
                        "start",
                        "--port",
                        str(args.port),
                        "--bind",
                        "127.0.0.1",
                    ],
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        time.sleep(2)


if __name__ == "__main__":
    main()
