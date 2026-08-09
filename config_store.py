"""Small, safe persistence helpers for the local YAML configuration."""
import os
import tempfile
import threading
import time
from contextlib import contextmanager

import yaml


_CONFIG_LOCK = threading.RLock()
_LOCK_TIMEOUT_SECONDS = 10
_STALE_LOCK_SECONDS = 60


@contextmanager
def _process_lock(path):
    """A small cross-process lock for YAML read-modify-write operations."""
    lock_path = f"{path}.lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                is_stale = time.time() - os.path.getmtime(lock_path) > _STALE_LOCK_SECONDS
                if is_stale:
                    os.unlink(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for configuration lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def _load_yaml_unlocked(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml_unlocked(path, config):
    directory = os.path.dirname(os.path.abspath(path))
    payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    fd, temporary_path = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def load_yaml(path):
    """Read a configuration snapshot.

    Writers use ``os.replace`` below, so readers always see either the old
    complete file or the new complete file, never a partially-written YAML
    document.
    """
    with _CONFIG_LOCK:
        return _load_yaml_unlocked(path)


def save_yaml(path, config):
    """Atomically replace *path* with a serialized YAML configuration."""
    with _CONFIG_LOCK:
        with _process_lock(path):
            _save_yaml_unlocked(path, config)


def update_yaml(path, mutator):
    """Atomically apply ``mutator(config)`` and return its result.

    This protects read-modify-write flows used by concurrent Flask request
    threads.  ``os.replace`` keeps concurrent readers safe as well.
    """
    with _CONFIG_LOCK:
        with _process_lock(path):
            config = _load_yaml_unlocked(path)
            result = mutator(config)
            _save_yaml_unlocked(path, config)
            return result
