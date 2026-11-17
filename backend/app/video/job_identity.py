"""Stable identities for media and processing options, without saving credentials."""

import hashlib
import json
import threading
import os
import tempfile
from pathlib import Path

_HASHES = {}
_LOCK = threading.RLock()


def clear_media_hash_cache():
    with _LOCK:
        _HASHES.clear()


def remember_hash(path, digest):
    path = Path(path).resolve()
    stat = path.stat()
    with _LOCK:
        _HASHES[(str(path), stat.st_size, stat.st_mtime_ns)] = digest
    return digest


def media_hash(path):
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _LOCK:
        digest = _HASHES.get(key)
    if digest:
        return digest
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return remember_hash(path, hasher.hexdigest())


def processing_key(options):
    """Provider credentials stay in memory; source/subtitle identities are separate."""
    public = {key: value for key, value in options.items()
              if key not in {"api_key", "video_source", "subtitle_file_path", "is_url"}}
    return hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".checkpoint-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
