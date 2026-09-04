"""Run pytest in parallel with a bounded worker count."""

from __future__ import annotations

import os
import subprocess
import sys

import psutil

_MAX_DEFAULT_WORKERS = 8
_WORKERS_ENV = "PASSAGEN_PYTEST_WORKERS"


def _available_cpu_count() -> int:
    physical = psutil.cpu_count(logical=False) or 1
    if hasattr(os, "sched_getaffinity"):
        return max(1, min(physical, len(os.sched_getaffinity(0))))
    return physical


def _worker_count() -> int:
    configured = os.environ.get(_WORKERS_ENV)
    if configured is not None:
        try:
            workers = int(configured)
        except ValueError as error:
            raise SystemExit(f"{_WORKERS_ENV} must be a positive integer") from error
        if workers < 1:
            raise SystemExit(f"{_WORKERS_ENV} must be a positive integer")
        return workers

    return max(1, min(_MAX_DEFAULT_WORKERS, _available_cpu_count() // 2))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    workers = _worker_count()
    print(f"pytest-xdist workers: {workers}", flush=True)
    return subprocess.call([sys.executable, "-m", "pytest", f"-n{workers}", *args])


if __name__ == "__main__":
    raise SystemExit(main())
