"""Прерывание команды, которая выполняется в сессии (Ctrl+C в терминале UI).

Воркер не умеет отменять ssh_exec, а stdin команды закрыт (wrapper.py), поэтому
прерывание идёт отдельной служебной командой во втором SSH-подключении к тому же
серверу (companion): она находит процессы команды и шлёт им сигнал.

Как найти процессы: у каждой команды пользователя свой маркер (wrapper.new_marker), и он
есть в аргументах процессов обёртки (`sh -c '<...маркер...>'`). Сам скрипт прерывания
маркер целиком не содержит (собирается из двух половин), поэтому себя не найдёт.

Кому сигнал:
* есть `timeout` (Linux): он лидер своей группы процессов, в ней вся команда. Сигнал
  получают все процессы группы, кроме самого timeout: команда пользователя получает его,
  как от Ctrl+C, а timeout просто видит, что она закончилась, и отдаёт её код (130 у
  INT, 143 у TERM, 137 у KILL). Если бы сигнал получил и timeout, он считал бы его своим
  поводом остановить команду: timeout из uutils (Ubuntu 26.04 и новее) тогда отдаёт 124,
  как при таймауте, а GNU timeout через 3 с (-k) добивает команду KILL;
* нет `timeout` (macOS, busybox без него): процессам-детям внутренней оболочки обёртки.

Сначала INT (как Ctrl+C); если команда через INTERRUPT_GRACE секунд ещё жива, TERM, потом
KILL. Ответ скрипта: `synex-interrupt: done <сигнал>`, `none` (команда уже закончилась)
или `alive` (не помог и KILL: например, процесс другого пользователя после sudo).
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER = "synex-interrupt:"
SIGNALS = ("INT", "TERM", "KILL")
INTERRUPT_GRACE = 2.0  # сек на каждый сигнал


@dataclass(frozen=True)
class InterruptOutcome:
    state: str  # done | none | alive | unknown
    signal: str | None  # каким сигналом команда закончилась


def interrupt_script(marker: str, grace: float = INTERRUPT_GRACE) -> str:
    # Две половины в своих кавычках: целиком маркер в тексте скрипта не встречается
    # (маркер состоит из букв, цифр, _ и :, кавычки внутри него не нужны экранировать)
    half = len(marker) // 2
    left, right = f"'{marker[:half]}'", f"'{marker[half:]}'"
    steps = max(1, round(grace / 0.1))
    return f"""\
M={left}{right}
export M
procs() {{
  ps -eo pid=,pgid=,ppid=,comm=,args= 2>/dev/null || ps -o pid=,pgid=,ppid=,comm=,args=
}}
alive() {{
  procs | awk 'index($0, ENVIRON["M"]) {{ found = 1 }} END {{ exit !found }}'
}}
targets() {{
  procs | awk '
    BEGIN {{ m = ENVIRON["M"]; e = m "E" }}
    {{ pid[NR] = $1; pgid[NR] = $2; ppid[NR] = $3; comm[NR] = $4; line[NR] = $0 }}
    END {{
      for (i = 1; i <= NR; i++)
        if (index(line[i], m) && comm[i] ~ /(^|\\/)timeout$/ && pgid[i] == pid[i]) {{
          group[pgid[i]] = 1; leader[pid[i]] = 1; t = 1
        }}
      if (t) {{
        for (i = 1; i <= NR; i++)
          if ((pgid[i] in group) && !(pid[i] in leader)) print pid[i]
        exit
      }}
      for (i = 1; i <= NR; i++)
        if (index(line[i], m) && !index(line[i], e)) inner[pid[i]] = 1
      for (i = 1; i <= NR; i++)
        if (ppid[i] in inner && !index(line[i], m)) print pid[i]
    }}'
}}
if ! alive; then echo "{HEADER} none"; exit 0; fi
for sig in {" ".join(SIGNALS)}; do
  for t in $(targets); do kill -s "$sig" -- "$t" 2>/dev/null || kill -"$sig" "$t" 2>/dev/null; done
  i=0
  while [ "$i" -lt {steps} ]; do
    if ! alive; then echo "{HEADER} done $sig"; exit 0; fi
    sleep 0.1
    i=$((i + 1))
  done
done
echo "{HEADER} alive"
"""


def parse_outcome(stdout: str) -> InterruptOutcome:
    for line in stdout.splitlines():
        if line.startswith(HEADER):
            fields = line.removeprefix(HEADER).split()
            state = fields[0] if fields else "unknown"
            signal = fields[1] if len(fields) > 1 and fields[1] in SIGNALS else None
            return InterruptOutcome(state, signal)
    return InterruptOutcome("unknown", None)
