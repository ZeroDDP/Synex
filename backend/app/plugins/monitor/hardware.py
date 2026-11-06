"""Диски и видеокарты сервера: одна команда через ssh_exec раз в MONITOR_HARDWARE_INTERVAL.

monitor_get_values воркера отдаёт только CPU и память. Диски (`df -kP`) и видеокарты
NVIDIA (`nvidia-smi`) backend получает сам, реже основного опроса: они меняются
медленно. Когда воркер научится отдавать их сам, здесь сменится только источник.

* Диски: реальные устройства (/dev/..., кроме loop: это образы snap) и корень "/"
  (в контейнере он на overlay). Одно устройство, смонтированное в нескольких местах,
  показывается один раз. Размеры в байтах.
* GPU: только NVIDIA (nvidia-smi). Нет nvidia-smi или карт: пустой список. Значения,
  которые карта не сообщает ([N/A]), приходят как null.
"""

from __future__ import annotations

from dataclasses import dataclass

KIB = 1024
MIB = 1024 * 1024
GPU_SEPARATOR = "__SYNEX_GPU__"
GPU_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"

# Одна строка для любой POSIX-оболочки (и fish): ошибки отсутствующих утилит глушатся
COMMAND = (
    f"df -kP 2>/dev/null; echo {GPU_SEPARATOR}; "
    f"nvidia-smi --query-gpu={GPU_QUERY} --format=csv,noheader,nounits 2>/dev/null; true"
)


@dataclass(frozen=True)
class Disk:
    mount: str
    filesystem: str
    total: int
    used: int
    available: int
    percent: float


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    utilization: float | None  # %
    memory_used: int | None  # байты
    memory_total: int | None
    temperature: float | None  # °C


@dataclass(frozen=True)
class Hardware:
    disks: list[Disk]
    gpus: list[Gpu]


def parse_hardware(stdout: str) -> Hardware:
    df_text, _, gpu_text = stdout.partition(GPU_SEPARATOR)
    return Hardware(disks=parse_df(df_text), gpus=parse_nvidia_smi(gpu_text))


def parse_df(text: str) -> list[Disk]:
    """Вывод `df -kP`: Filesystem 1024-blocks Used Available Capacity Mounted-on."""
    disks: list[Disk] = []
    seen: set[str] = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not all(p.isdigit() for p in parts[1:4]):
            continue
        filesystem, mount = parts[0], " ".join(parts[5:])  # в точке монтирования бывают пробелы
        real = filesystem.startswith("/dev/") and not filesystem.startswith("/dev/loop")
        if not (real or mount == "/") or filesystem in seen:
            continue
        seen.add(filesystem)
        total, used, available = (int(p) * KIB for p in parts[1:4])
        # Как в df: занято от доступного пользователю (без резерва root)
        percent = round(used / (used + available) * 100, 2) if used + available else 0.0
        disks.append(Disk(mount, filesystem, total, used, available, percent))
    return disks


def parse_nvidia_smi(text: str) -> list[Gpu]:
    gpus: list[Gpu] = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) != 6 or not fields[0].isdigit():
            continue
        index, name, utilization, used, total, temperature = fields
        gpus.append(
            Gpu(
                index=int(index),
                name=name,
                utilization=_number(utilization),
                memory_used=_mib(used),
                memory_total=_mib(total),
                temperature=_number(temperature),
            )
        )
    return gpus


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def _mib(value: str) -> int | None:
    number = _number(value)
    return None if number is None else int(number * MIB)
