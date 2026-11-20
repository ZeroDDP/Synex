"""Файлы логов на сервере: список, последние строки и новые строки по мере записи.

Команды — короткие POSIX-скрипты (sh, dash, bash, ash из busybox): `[ -r ]`, stat, tail -c,
head -c, grep -F. stat в формате GNU и busybox (-c), на BSD и macOS (-f) запасной вариант.

Файл читается кусками по байтам: скрипт печатает строку-заголовок (размер, inode, с какого
байта читает), потом байты файла. Так живой лог не теряет и не повторяет строки:

* следующий опрос читает с того байта, на котором закончил прошлый;
* недописанная последняя строка (без перевода строки) не отдаётся, её байты читаются
  снова в следующий раз;
* файл стал меньше или у пути другой inode (logrotate переименовал старый и создал
  новый): читаем новый файл с начала, UI получает rotated=true;
* новых байт больше LOGS_CHUNK_BYTES (лог пишет очень быстро, или UI переподключился со
  старым offset): середина пропускается, UI получает skipped (сколько байт).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

HEADER = "synex-log:"
# Строка после байтов файла. Вывод команды приходит без последних переводов строки (так
# его готовит обёртка команд для терминала), а для живого лога важно, кончается ли файл
# переводом строки: без него последняя строка ещё дописывается
END = f"{HEADER} end"
_END = f"printf '\\n%s\\n' \"{END}\""

# Сжатые и двоичные файлы в списке не нужны
_SKIP = "|".join(
    (
        *("*.gz", "*.xz", "*.bz2", "*.zst", "*.lz4", "*.zip", "*.journal", "*.journal~"),
        *("*/wtmp", "*/wtmp.*", "*/btmp", "*/btmp.*", "*/lastlog", "*/faillog"),
    )
)

# Размер и inode файла $f в $1 $2 (GNU, busybox; BSD и macOS)
_STAT = 'set -- $(stat -L -c "%s %i" -- "$f" 2>/dev/null || stat -L -f "%z %i" -- "$f")'

_CHECK = f"""\
if [ ! -e "$f" ]; then echo "{HEADER} missing"; exit 0; fi
if [ -d "$f" ]; then echo "{HEADER} directory"; exit 0; fi
if [ ! -r "$f" ]; then echo "{HEADER} denied"; exit 0; fi
{_STAT}
size=$1 inode=$2
"""


def list_command(directory: str) -> str:
    """Файлы папки и её подпапок первого уровня: r или -, размер, mtime (сек), путь."""
    return f"""\
d={shlex.quote(directory)}
if [ ! -d "$d" ]; then echo "{HEADER} missing"; exit 0; fi
echo "{HEADER} ok"
for f in "$d"/* "$d"/*/*; do
  [ -f "$f" ] || continue
  case "$f" in {_SKIP}) continue ;; esac
  if [ -r "$f" ]; then r=r; else r=-; fi
  s=$(stat -L -c "%s %Y" -- "$f" 2>/dev/null || stat -L -f "%z %m" -- "$f" 2>/dev/null)
  printf '%s\\t%s\\t%s\\n' "$r" "$s" "$f"
done"""


def tail_command(path: str, window: int) -> str:
    """Последние байты файла (не больше window): заголовок как у follow_command (начало =
    пропущено: всё до окна), потом байты от начала окна до размера на момент чтения."""
    return f"""\
f={shlex.quote(path)} W={int(window)}
{_CHECK}start=$(( size > W ? size - W : 0 ))
echo "{HEADER} ok $size $inode $start same $start"
tail -c +$((start + 1)) -- "$f" | head -c $((size - start))
{_END}"""


def grep_command(path: str, text: str, lines: int) -> str:
    """Последние `lines` строк файла с текстом text (без учёта регистра латиницы)."""
    return f"""\
f={shlex.quote(path)} N={int(lines)}
{_CHECK}echo "{HEADER} ok $size $inode 0"
head -c "$size" -- "$f" | grep -i -F -e {shlex.quote(text)} | tail -n "$N" """


def follow_command(path: str, offset: int, inode: str | None, chunk: int) -> str:
    """Новые байты после offset: заголовок `ok размер inode начало состояние пропущено`."""
    return f"""\
f={shlex.quote(path)} O={int(offset)} I={shlex.quote(inode or "")} C={int(chunk)}
{_CHECK}from=$O state=same skipped=0
if {{ [ -n "$I" ] && [ "$inode" != "$I" ]; }} || [ "$size" -lt "$O" ]; then
  from=0 state=rotated
fi
if [ $((size - from)) -gt "$C" ]; then skipped=$((size - C - from)) from=$((size - C)); fi
echo "{HEADER} ok $size $inode $from $state $skipped"
if [ "$size" -gt "$from" ]; then tail -c +$((from + 1)) -- "$f" | head -c $((size - from)); fi
{_END}"""


# ------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class LogFile:
    path: str
    readable: bool
    size: int | None
    modified: int | None  # мс Unix

    @property
    def rotated(self) -> bool:
        """Старая копия после logrotate: syslog.1, messages-20261120."""
        name = self.path.rsplit("/", 1)[-1]
        last = name.rsplit(".", 1)[-1] if "." in name else ""
        dashed = name.rsplit("-", 1)[-1] if "-" in name else ""
        return last.isdigit() or (len(dashed) == 8 and dashed.isdigit())


@dataclass
class Chunk:
    """Прочитанный кусок файла."""

    status: str  # ok | missing | directory | denied
    size: int = 0
    inode: str | None = None
    start: int = 0  # с какого байта прочитано
    state: str = "same"  # same | rotated
    skipped: int = 0
    text: str = ""
    lines: list[str] = field(default_factory=list)
    offset: int = 0  # с какого байта читать в следующий раз


def _header(stdout: str) -> tuple[list[str], str]:
    """(поля заголовка, байты файла). Без заголовка: ([], вывод)."""
    first, _, rest = stdout.partition("\n")
    if not first.startswith(HEADER):
        return [], stdout
    body, end, _ = rest.rpartition(f"\n{END}")
    return first.removeprefix(HEADER).split(), body if end else rest


def parse_list(stdout: str) -> tuple[str, list[LogFile]]:
    fields, rest = _header(stdout)
    status = fields[0] if fields else "missing"
    files = []
    for line in rest.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2].startswith("/"):
            continue
        flag, stat, path = parts
        numbers = stat.split()
        size = int(numbers[0]) if numbers and numbers[0].isdigit() else None
        mtime = int(numbers[1]) * 1000 if len(numbers) > 1 and numbers[1].isdigit() else None
        files.append(LogFile(path=path, readable=flag == "r", size=size, modified=mtime))
    files.sort(key=lambda item: item.path)
    return status, files


def parse_chunk(stdout: str, *, hold_partial: bool, limit: int = 0) -> Chunk:
    """Ответ tail_command или follow_command.

    hold_partial (живой лог): недописанная последняя строка не отдаётся, offset указывает
    на её начало, и следующий опрос прочитает её целиком. Исключение: строка без перевода
    строки длиннее limit байт, ждать её конца бессмысленно."""
    fields, text = _header(stdout)
    if not fields or fields[0] != "ok":
        return Chunk(status=fields[0] if fields else "missing")
    size = _int(fields, 1)
    chunk = Chunk(
        status="ok",
        size=size,
        inode=fields[2] if len(fields) > 2 else None,
        start=_int(fields, 3),
        state=fields[4] if len(fields) > 4 else "same",
        skipped=_int(fields, 5),
        text=text,
        offset=size,
    )
    if chunk.skipped > 0:
        # Читали с середины файла (окно последних байт или пропуск): первая строка неполная
        _, newline, text = text.partition("\n")
        if not newline:
            text = ""
    if text and not text.endswith("\n"):
        head, _, partial = text.rpartition("\n")
        held = len(partial.encode("utf-8"))
        if hold_partial and (head or held < limit):
            chunk.offset = size - held
            text = head + "\n" if head else ""
        else:
            text += "\n"
    chunk.lines = [line.removesuffix("\r") for line in text.split("\n")[:-1]]
    return chunk


def parse_grep(stdout: str) -> Chunk:
    """Ответ grep_command: строки, в которых найден текст."""
    fields, text = _header(stdout)
    if not fields or fields[0] != "ok":
        return Chunk(status=fields[0] if fields else "missing")
    size = _int(fields, 1)
    lines = [line.removesuffix("\r") for line in text.splitlines()]
    return Chunk(
        status="ok",
        size=size,
        inode=fields[2] if len(fields) > 2 else None,
        text=text,
        lines=lines,
        offset=size,
    )


def _int(fields: list[str], index: int) -> int:
    try:
        return int(fields[index])
    except (IndexError, ValueError):
        return 0
