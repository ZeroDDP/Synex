"""Буфер вывода SSH-сессии.

Воркер отдаёт результат команды целиком, когда она закончилась, поэтому буфер хранит
не поток байтов, а записи: какая команда запущена, чем закончилась, какая ошибка
случилась. У каждой записи свой номер (seq), он растёт на 1. UI, который переподключился
или открыл сессию во второй вкладке, запрашивает записи после последнего известного
ему номера (since) и получает всё, что пропустил.

Размер буфера ограничен (SSH_OUTPUT_BUFFER_BYTES): старые записи вытесняются. Если
одна запись больше всего буфера, у её stdout/stderr остаётся конец (в терминале важнее
последние строки) и ставится truncated=true.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

EntryKind = Literal["opened", "command", "result", "error"]

# Поля, которые можно обрезать у слишком большой записи
TRUNCATABLE = ("stdout", "stderr")


@dataclass(frozen=True)
class OutputEntry:
    seq: int
    kind: EntryKind
    data: dict[str, Any]
    ts: int = field(default_factory=lambda: time.time_ns() // 1_000_000)  # Unix, мс
    size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, **self.data}


@dataclass(frozen=True)
class Replay:
    entries: list[OutputEntry]
    gap: bool  # часть записей после since уже вытеснена из буфера
    first_seq: int  # самая старая запись в буфере (0, если буфер пуст)
    last_seq: int  # последняя выданная запись (0, если записей ещё не было)


class OutputBuffer:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1024, max_bytes)
        self._entries: deque[OutputEntry] = deque()
        self._size = 0
        self._last_seq = 0

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def first_seq(self) -> int:
        return self._entries[0].seq if self._entries else 0

    @property
    def size(self) -> int:
        return self._size

    def __len__(self) -> int:
        return len(self._entries)

    def append(self, kind: EntryKind, data: dict[str, Any]) -> OutputEntry:
        data, size = _fit(data, self.max_bytes)
        self._last_seq += 1
        entry = OutputEntry(seq=self._last_seq, kind=kind, data=data, size=size)
        self._entries.append(entry)
        self._size += size
        while self._size > self.max_bytes and len(self._entries) > 1:
            self._size -= self._entries.popleft().size
        return entry

    def since(self, seq: int = 0, limit: int | None = None) -> Replay:
        """Записи с номером больше seq (по порядку, не больше limit штук)."""
        seq = max(0, seq)
        entries = [entry for entry in self._entries if entry.seq > seq]
        if limit is not None:
            entries = entries[:limit]
        first = self.first_seq
        gap = seq < self._last_seq and bool(first) and first > seq + 1
        return Replay(entries=entries, gap=gap, first_seq=first, last_seq=self._last_seq)


def _size_of(data: dict[str, Any]) -> int:
    # Приблизительно, по длине строк: точный размер JSON считать дорого
    return 64 + sum(len(value) for value in data.values() if isinstance(value, str))


def _fit(data: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], int]:
    size = _size_of(data)
    if size <= max_bytes:
        return data, size

    data = dict(data)
    for name in TRUNCATABLE:
        value = data.get(name)
        if not isinstance(value, str) or not value:
            continue
        excess = size - max_bytes
        if excess <= 0:
            break
        keep = max(0, len(value) - excess)
        data[name] = value[len(value) - keep :]
        size -= len(value) - keep
    data["truncated"] = True
    return data, size
