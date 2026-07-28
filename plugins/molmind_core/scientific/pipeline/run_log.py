"""流水线双语运行日志（中/英分行输出）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

LogLevel = Literal["INFO", "SUCCESS", "WARN", "ERROR"]
LogLang = Literal["zh", "en"]
LogSink = Callable[["RunLogEntry"], None]


@dataclass(frozen=True)
class RunLogEntry:
    level: LogLevel
    message: str
    lang: LogLang
    progress: int | None = None
    ts: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["progress"] is None:
            del d["progress"]
        return d


class RunLogCollector:
    """收集并可选向外推送（流式）日志条目；emit 会分别输出中文与英文两条。"""

    def __init__(self, sink: LogSink | None = None) -> None:
        self._entries: list[RunLogEntry] = []
        self._sink = sink

    @property
    def entries(self) -> list[RunLogEntry]:
        return list(self._entries)

    def as_dicts(self) -> list[dict]:
        return [e.to_dict() for e in self._entries]

    def _push(
        self,
        level: LogLevel,
        message: str,
        lang: LogLang,
        *,
        progress: int | None = None,
    ) -> RunLogEntry:
        entry = RunLogEntry(
            level=level,
            message=message,
            lang=lang,
            progress=progress,
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._entries.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry

    def emit(
        self,
        level: LogLevel,
        zh: str,
        en: str,
        *,
        progress: int | None = None,
    ) -> tuple[RunLogEntry, RunLogEntry]:
        """同时输出两条完整日志：先中文，后英文。进度仅挂在中文条上。"""
        zh_entry = self._push(level, zh, "zh", progress=progress)
        en_entry = self._push(level, en, "en", progress=None)
        return zh_entry, en_entry


__all__ = ["LogLang", "LogLevel", "LogSink", "RunLogCollector", "RunLogEntry"]
