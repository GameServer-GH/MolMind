"""进程级证据索引缓存：按路径 mtime/size 失效，避免每次 screen 全量重载。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_lock = threading.RLock()
_cache: dict[str, tuple[tuple[Any, ...], Any]] = {}


def paths_fingerprint(paths: list[Path] | tuple[Path, ...]) -> tuple[Any, ...]:
    """稳定指纹：路径 + mtime_ns + size；缺失文件记为占位。"""
    parts: list[tuple[str, int | None, int | None]] = []
    for path in sorted({Path(p) for p in paths}, key=lambda item: str(item)):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        try:
            stat = resolved.stat()
            parts.append((str(resolved), int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            parts.append((str(resolved), None, None))
    return tuple(parts)


def get_or_load(cache_key: str, fingerprint: tuple[Any, ...], loader: Callable[[], T]) -> T:
    with _lock:
        hit = _cache.get(cache_key)
        if hit is not None and hit[0] == fingerprint:
            return hit[1]  # type: ignore[no-any-return]
        value = loader()
        _cache[cache_key] = (fingerprint, value)
        return value


def clear_index_cache() -> None:
    with _lock:
        _cache.clear()
