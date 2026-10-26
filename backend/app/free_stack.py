"""Run the API and durable job processes in one small demo container.

Render's free plan does not provide standalone background workers. This module
co-locates the API, Dramatiq worker, and reconciler for the free demo deployment
while keeping the normal multi-service commands available elsewhere.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence


def process_commands(port: str) -> tuple[tuple[str, Sequence[str]], ...]:
    """Return the supervised commands in startup order."""

    return (
        (
            "api",
            (
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                port,
            ),
        ),
        (
            "worker",
            (
                "dramatiq",
                "app.workers.worker",
                "--processes",
                "1",
                "--threads",
                "1",
            ),
        ),
        ("reconciler", (sys.executable, "-m", "app.workers.reconciler")),
    )


def _wait_for_api(port: str, process: subprocess.Popen[bytes]) -> None:
    """Give the API cold start exclusive CPU before launching job processes."""

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"API exited during startup with code {return_code}.")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("API did not open its port within 90 seconds.")


def _stop(processes: list[tuple[str, subprocess.Popen[bytes]]]) -> None:
    for _, process in reversed(processes):
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 10
    for _, process in reversed(processes):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    """Migrate the database, start every process, and fail as one unit."""

    print("free-stack applying database migrations...", flush=True)
    try:
        subprocess.run(("alembic", "upgrade", "head"), check=True)
        print("free-stack database migrations applied successfully", flush=True)
    except Exception as exc:
        print(f"free-stack warning: database migration encountered error: {exc}", flush=True)

    processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    shutting_down = False

    def handle_shutdown(signum: int, _frame: object) -> None:
        nonlocal shutting_down
        shutting_down = True
        print(f"free-stack received signal {signum}; stopping", flush=True)
        _stop(processes)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        for name, command in process_commands(os.getenv("PORT", "8000")):
            print(f"free-stack starting {name}", flush=True)
            process = subprocess.Popen(command)
            processes.append((name, process))
            if name == "api":
                _wait_for_api(os.getenv("PORT", "8000"), process)

        while not shutting_down:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    print(
                        f"free-stack process {name} exited with code {return_code}",
                        flush=True,
                    )
                    return return_code or 1
            time.sleep(1)
    finally:
        _stop(processes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
