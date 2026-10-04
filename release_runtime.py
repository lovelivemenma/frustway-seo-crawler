from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path


_JOB_RE = re.compile(r"^[a-f0-9]{32}$")

_MAX_CONCURRENT = max(
    1,
    int(
        os.environ.get(
            "FRUSTWAY_MAX_CONCURRENT_CRAWLS",
            "2",
        )
    ),
)

_CRAWL_SEMAPHORE = threading.BoundedSemaphore(
    _MAX_CONCURRENT
)


def new_job_id() -> str:
    return uuid.uuid4().hex


def normalize_job_id(value) -> str | None:
    if value is None:
        return None

    value = str(value).strip().lower()

    if _JOB_RE.fullmatch(value):
        return value

    return None


def ensure_job_dir(
    jobs_root: str | Path,
    job_id: str,
) -> Path:
    root = Path(jobs_root)
    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    job_dir = root / job_id
    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    touch_job(job_dir)
    return job_dir


def touch_job(job_dir: str | Path) -> None:
    path = Path(job_dir)

    try:
        now = time.time()
        os.utime(
            path,
            (now, now),
        )
    except OSError:
        pass


def cleanup_old_jobs(
    jobs_root: str | Path,
    ttl_hours: int = 24,
    exclude_job_ids=None,
) -> int:
    root = Path(jobs_root)

    if not root.exists():
        return 0

    excluded = set(
        exclude_job_ids or []
    )

    cutoff = time.time() - (
        max(int(ttl_hours), 1)
        * 3600
    )

    removed = 0

    for child in root.iterdir():
        if not child.is_dir():
            continue

        if child.name in excluded:
            continue

        if not _JOB_RE.fullmatch(
            child.name
        ):
            continue

        try:
            modified = child.stat().st_mtime
        except OSError:
            continue

        if modified >= cutoff:
            continue

        try:
            shutil.rmtree(child)
            removed += 1
        except OSError:
            pass

    return removed


def acquire_crawl_slot() -> bool:
    return _CRAWL_SEMAPHORE.acquire(
        blocking=False
    )


def release_crawl_slot() -> None:
    try:
        _CRAWL_SEMAPHORE.release()
    except ValueError:
        pass


def max_concurrent_crawls() -> int:
    return _MAX_CONCURRENT
