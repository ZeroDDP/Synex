"""Обёртка команды для ssh_exec: cwd, точный код возврата и таймаут на сервере.

Воркер выполняет каждую команду в новом exec-канале (CoreDocs 5.2), поэтому:
  * `cd` между командами не сохраняется: cwd храним в Python и подставляем;
  * воркер всегда отдаёт exit_code=0 (сборка 2026-11-03), поэтому код печатаем сами;
  * воркер обрабатывает запросы подключения по одному и не умеет отменять команду,
    поэтому таймаут нужно применять на сервере: иначе `tail -f` занимает воркер навсегда.

Что уходит на сервер (упрощённо, <M> это маркер со случайной частью):

    if timeout -k 3 1 true >/dev/null 2>&1; then          # есть ли рабочий timeout
      ( timeout -k 3 <N> "$SHELL" -c '<inner>' &           # в фоне: нужен его PID
        pid=$!; wait $pid; rc=$?
        case $rc in 124|137) kill -KILL -$pid;; esac        # добить группу
        exit $rc ) 3>&2 2>/dev/null
    else
      "$SHELL" -c '<inner>'                                # таймаут только в Python
    fi
    __synex_rc=$?; printf '\\n<M>E%s' "$__synex_rc"; exit $__synex_rc

<inner>:
    exec </dev/null                                        # команда не ждёт ввода
    cd -- <cwd> || { <ошибка>; cd; printf '\\n<M>C1|%s' "$PWD"; exit 1; }
    <команда пользователя>
    __synex_rc=$?; printf '\\n<M>C%s|%s' "$__synex_rc" "$PWD"; exit $__synex_rc

* `timeout` (coreutils) по истечении N секунд шлёт TERM всей группе процессов
  команды, через 3 секунды KILL. Код 124 (TERM) или 137 (KILL) значит «прервано».
* После прерывания обёртка сама шлёт KILL всей группе timeout (её номер = PID timeout).
  Иначе процесс, который игнорирует TERM, держал бы вывод открытым, и канал SSH не
  закрылся бы: timeout из uutils (Ubuntu 26.04 и новее) шлёт KILL только своему
  дочернему процессу, а GNU timeout не трогает группу, если команда умерла от TERM.
* Маркер C печатается, только если команда дошла до конца: в нём код и новая cwd.
  Маркер E печатает внешняя оболочка всегда: он даёт код даже после `exit 3`.
* `3>&2 2>/dev/null` и `exec 2>&3` в начале <inner>: stderr команды идёт как обычно,
  а сообщения оболочки вида "Killed ..." (bash печатает в них весь скрипт) глушатся.
* stdin команды это /dev/null. Ввести что-то в команду через API нельзя, а без этого
  `cat`, `read`, `sudo` с паролем или вопрос `apt install` [Y/n] ждали бы ввода до
  таймаута. Теперь они сразу получают конец ввода и завершаются.
* Если директории из cwd больше нет, команда НЕ выполняется (иначе `rm -rf *`
  сработал бы в домашней папке): exit_code=1, cwd=$HOME.
* Переводы строк в команде приводятся к LF (normalize_newlines): CR из Windows-текста
  shell считает частью слова, и `cd /tmp\r` падает с "can't cd to /tmp".

Нужен POSIX-совместимый login shell на сервере (bash, zsh, sh/dash); проверено на всех
четырёх. Для fish используйте track_cwd=false: команда уйдёт как есть.
"""

from __future__ import annotations

import secrets
import shlex
from dataclasses import dataclass

RC_VAR = "__synex_rc"
PID_VAR = "__synex_pid"
KILL_AFTER = 3  # сек между TERM и KILL
TIMEOUT_EXIT_CODES = frozenset({124, 137})  # timeout: TERM / KILL


def normalize_newlines(command: str) -> str:
    """CRLF и одиночный CR (вставка из Windows, Enter в терминале) → LF."""
    return command.replace("\r\n", "\n").replace("\r", "\n")


def new_marker() -> str:
    """Маркер со случайной частью, чтобы вывод команды не мог его подделать."""
    return f"__SYNEX_{secrets.token_hex(6)}__:"


def build_command(
    command: str,
    cwd: str | None,
    marker: str,
    timeout: int | None,
    kill_after: int = KILL_AFTER,
) -> str:
    inner: list[str] = ["exec </dev/null"]
    if cwd:
        quoted = shlex.quote(cwd)
        inner.append(
            f"cd -- {quoted} 2>/dev/null || {{ "
            f"printf 'synex: directory %s is not available, switched to home\\n' {quoted} >&2; "
            f"cd; printf '\\n{marker}C1|%s' \"$PWD\"; exit 1; }}"
        )
    inner.append(command)
    inner.append(f'{RC_VAR}=$?; printf \'\\n{marker}C%s|%s\' "${RC_VAR}" "$PWD"; exit ${RC_VAR}')
    body = "\n".join(inner)

    shell = '"${SHELL:-/bin/sh}"'
    if timeout:
        body_with_stderr = shlex.quote("exec 2>&3 3>&-\n" + body)
        # timeout в фоне, чтобы знать его PID: это и номер его группы процессов
        run = (
            f"if timeout -k {kill_after} 1 true >/dev/null 2>&1; then "
            f"( timeout -k {kill_after} {timeout} {shell} -c {body_with_stderr} </dev/null & "
            f"{PID_VAR}=$!; wait ${PID_VAR}; {RC_VAR}=$?; "
            f"case ${RC_VAR} in 124|137) kill -KILL -${PID_VAR} 2>/dev/null;; esac; "
            f"exit ${RC_VAR} ) "
            f"3>&2 2>/dev/null; "
            f"else {shell} -c {shlex.quote(body)}; fi"
        )
    else:
        run = f"{shell} -c {shlex.quote(body)}"

    return f"{run}\n{RC_VAR}=$?; printf '\\n{marker}E%s' \"${RC_VAR}\"; exit ${RC_VAR}"


@dataclass(frozen=True)
class WrappedResult:
    stdout: str  # вывод команды без служебных маркеров
    exit_code: int | None  # None: маркеров нет, остаётся код от воркера
    cwd: str | None  # новая cwd; None: команда не дошла до конца, cwd не меняется
    timed_out: bool  # команду прервал timeout на сервере


def parse_output(stdout: str, marker: str, *, timeout_applied: bool) -> WrappedResult:
    text = stdout.rstrip("\r\n")

    wrapper_rc: int | None = None
    index = text.rfind(f"{marker}E")
    if index != -1:
        tail = text[index + len(marker) + 1 :].strip()
        if tail.isdigit():
            wrapper_rc = int(tail)
        text = text[:index].removesuffix("\n")

    command_rc: int | None = None
    cwd: str | None = None
    index = text.rfind(f"{marker}C")
    if index != -1:
        line = text[index + len(marker) + 1 :].split("\n", 1)[0].strip()
        rc_text, sep, path = line.partition("|")
        if sep and rc_text.isdigit():
            command_rc = int(rc_text)
            cwd = path or None
        text = text[:index].removesuffix("\n")

    timed_out = timeout_applied and command_rc is None and wrapper_rc in TIMEOUT_EXIT_CODES
    exit_code = command_rc if command_rc is not None else wrapper_rc
    return WrappedResult(
        stdout=text.rstrip("\r\n"), exit_code=exit_code, cwd=cwd, timed_out=timed_out
    )
