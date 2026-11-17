"""Remove only regenerable media intermediates, preserving saved work."""

import logging
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_IN_USE = Counter()
_RENDER_FILE = re.compile(r"(?:master_khmer_voice|silence|fitted_lip_\d+_\d+(?:\.\d+)?)\.wav\Z")
ACTIVE_STATUSES = {"queued", "processing", "running", "rendering", "pausing"}


@dataclass
class CacheCleanupReport:
    deleted_files: int = 0
    freed_bytes: int = 0
    skipped_active: int = 0
    skipped_referenced: int = 0
    errors: int = 0


@contextmanager
def cache_in_use(*paths):
    """Lease working folders so a concurrent submission cannot delete them."""
    paths = [Path(path).resolve() for path in paths if path]
    with _LOCK:
        _IN_USE.update(paths)
    try:
        yield
    finally:
        with _LOCK:
            for path in paths:
                _IN_USE[path] -= 1
                if not _IN_USE[path]:
                    del _IN_USE[path]


def _saved_paths(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _saved_paths(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _saved_paths(nested)
    elif isinstance(value, str) and (value.startswith("/") or value.startswith("storage/")):
        try:
            yield Path(value).resolve()
        except (OSError, ValueError):
            pass


def _safe_file(path, root):
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        return path.is_file() and path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def cleanup_processing_cache(storage_root, jobs, orphan_age_seconds=3600):
    """Clear allowlisted WAV/PCM files; unknown orphans get a one-hour grace period.

    Never delete source media, voice MP3s, exports, captions, job metadata, or
    referenced files. The lock also serializes concurrent cleanup requests.
    """
    report = CacheCleanupReport()
    root = Path(storage_root).absolute()
    if root.is_symlink() or not root.is_dir():
        return report
    root = root.resolve()
    now = time.time()
    with _LOCK:
        references, active_paths, owners = set(), set(_IN_USE), []
        for key, job in jobs.items():
            references.update(_saved_paths(job))
            active = job.get("status") in ACTIVE_STATUSES
            if active:
                for path in _saved_paths(job):
                    active_paths.add(path.parent)
            job_id = str(job.get("id") or key.split(":")[-1])
            if re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                owners.append((job_id, active))

        candidates = []
        dubbed = root / "dubbed"
        if dubbed.is_dir() and not dubbed.is_symlink():
            for directory in dubbed.glob("temp_*"):
                if directory.is_symlink() or not directory.is_dir():
                    continue
                matching = [active for job_id, active in owners if directory.name in
                            (f"temp_{job_id}", f"temp_dubbed_{job_id}") or
                            directory.name.startswith(f"temp_dubbed_{job_id}_visual_") or
                            directory.name.startswith(f"temp_dubbed_{job_id}_batch_")]
                for path in directory.iterdir():
                    if _RENDER_FILE.fullmatch(path.name):
                        candidates.append((path, bool(matching), any(matching)))
        for name in ("uploads", "downloads"):
            directory = root / name
            if directory.is_dir() and not directory.is_symlink():
                candidates.extend((path, False, False) for path in directory.rglob("*_16k.pcm"))

        for path, known_owner, active_owner in candidates:
            if not _safe_file(path, root):
                continue
            if path.resolve() in references:
                report.skipped_referenced += 1
                continue
            if active_owner or any(path == active or path.is_relative_to(active) for active in active_paths):
                report.skipped_active += 1
                continue
            try:
                before = path.stat()
                if not known_owner and now - before.st_mtime < orphan_age_seconds:
                    continue
                after = path.lstat()
                if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
                    continue
                path.unlink()
                report.deleted_files += 1
                report.freed_bytes += before.st_size
            except FileNotFoundError:
                continue
            except OSError as error:
                report.errors += 1
                logger.warning("Could not remove processing cache %s: %s", path.name, error)
    if report.deleted_files:
        logger.info("Processing cache cleanup: removed %s files, freed %.1f MiB",
                    report.deleted_files, report.freed_bytes / 1024 ** 2)
    return report


def purge_processing_cache(storage_root):
    """Explicit fresh start: drop transcripts, voices, previews and intermediates.

    Source videos, attached subtitles, finished renders, split clips, exports,
    logos and model downloads are outside these cache directories.
    """
    report = CacheCleanupReport()
    root = Path(storage_root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid storage directory")
    root = root.resolve()
    with _LOCK:
        if _IN_USE:
            raise RuntimeError("Processing files are still in use. Wait for workers to stop before clearing caches.")
        folders = [root / name for name in ("transcripts", "temp", "thumbnails")]
        for parent, pattern in (("downloads", "voice_*"), ("dubbed", "temp_*")):
            directory = root / parent
            if directory.is_dir() and not directory.is_symlink():
                folders.extend(path for path in directory.glob(pattern) if path.is_dir() and not path.is_symlink())
        files = []
        for directory in folders:
            if directory.is_dir() and not directory.is_symlink():
                files.extend(directory.rglob("*"))
        for name in ("uploads", "downloads"):
            directory = root / name
            if directory.is_dir() and not directory.is_symlink():
                files.extend(directory.rglob("*_16k.pcm"))
        dubbed = root / "dubbed"
        if dubbed.is_dir() and not dubbed.is_symlink():
            files.extend(dubbed.glob("preview_*.mp3"))
        for path in set(files):
            if _safe_file(path, root):
                size = path.stat().st_size
                path.unlink()
                report.deleted_files += 1
                report.freed_bytes += size
        for directory in folders:
            if directory.is_dir() and not directory.is_symlink():
                for child in sorted(directory.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                    if child.is_dir() and not child.is_symlink():
                        try:
                            child.rmdir()
                        except OSError:
                            pass
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return report
