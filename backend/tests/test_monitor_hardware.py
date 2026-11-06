"""Диски и видеокарты сервера в мониторинге: разбор df/nvidia-smi и опрос раз в
MONITOR_HARDWARE_INTERVAL."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.monitor.hardware import (
    GPU_SEPARATOR,
    MIB,
    Disk,
    Gpu,
    parse_df,
    parse_hardware,
    parse_nvidia_smi,
)
from tests.fake_worker import HARDWARE_OUTPUT
from tests.test_monitor import make_rig

KIB = 1024

DF = """Filesystem     1024-blocks      Used Available Capacity Mounted on
overlay           61255652  12000000  46115380      21% /
tmpfs                65536         0     65536       0% /dev
/dev/nvme0n1p2   491134588 200000000 266133560      43% /etc/hosts
/dev/nvme0n1p2   491134588 200000000 266133560      43% /etc/hostname
/dev/loop3           56832     56832         0     100% /snap/core18/2829
/dev/sdb1        976762584 100000000 876762584      11% /media/My Disk
udev               8086108         0   8086108       0% /dev
"""


def test_parse_df_keeps_real_disks_and_root():
    disks = parse_df(DF)
    assert [(d.mount, d.filesystem) for d in disks] == [
        ("/", "overlay"),  # корень контейнера на overlay
        ("/etc/hosts", "/dev/nvme0n1p2"),  # одно устройство показано один раз
        ("/media/My Disk", "/dev/sdb1"),  # пробел в точке монтирования
    ]
    root = disks[0]
    assert (root.total, root.used, root.available) == (
        61255652 * KIB,
        12000000 * KIB,
        46115380 * KIB,
    )
    assert root.percent == round(12000000 / (12000000 + 46115380) * 100, 2)


def test_parse_nvidia_smi():
    text = (
        "0, NVIDIA GeForce RTX 3080, 37, 2048, 10240, 61\n"
        "1, Tesla T4, [N/A], [N/A], 15360, [Not Supported]\n"
        "NVIDIA-SMI has failed because it couldn't communicate with the driver\n"
    )
    assert parse_nvidia_smi(text) == [
        Gpu(0, "NVIDIA GeForce RTX 3080", 37.0, 2048 * MIB, 10240 * MIB, 61.0),
        Gpu(1, "Tesla T4", None, None, 15360 * MIB, None),
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        f"{GPU_SEPARATOR}\n",  # нет ни df, ни nvidia-smi (не Linux, урезанный busybox)
        "",
        "fish: Unknown command: df\n",
    ],
)
def test_nothing_available(stdout):
    assert parse_hardware(stdout).disks == [] and parse_hardware(stdout).gpus == []


def test_parse_hardware_splits_df_and_gpu():
    hardware = parse_hardware(HARDWARE_OUTPUT)
    assert [d.mount for d in hardware.disks] == ["/", "/data"]
    assert hardware.gpus[0].name == "NVIDIA GeForce RTX 3080"
    assert isinstance(hardware.disks[0], Disk)


@pytest.fixture
def key_file(tmp_path: Path) -> str:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return str(key)


@pytest.fixture
async def rig(fake_worker, missing_socket_path, key_file, monkeypatch):
    monkeypatch.setattr(settings, "MONITOR_HARDWARE_INTERVAL", 10.0)
    monkeypatch.setattr(settings, "MONITOR_POLL_TIMEOUT", 2.0)
    rig = await make_rig(fake_worker, missing_socket_path, key_file)
    yield rig
    await rig.monitor.stop()
    await rig.harness.disconnect()


def hardware_requests(rig) -> list[dict]:
    return [
        r
        for r in rig.state.requests
        if r["action"] == "ssh_exec" and GPU_SEPARATOR in r["payload"]["command"]
    ]


async def test_disks_and_gpus_are_polled_rarely(rig, monkeypatch):
    await rig.open("server-a")
    assert rig.server("server-a:22").disks is None  # ещё не опрашивали

    await rig.poll()
    server = rig.server("server-a:22")
    assert [(d.mount, d.percent) for d in server.disks] == [("/", 52.71), ("/data", 50.0)]
    assert [(g.name, g.utilization, g.memory_total) for g in server.gpus] == [
        ("NVIDIA GeForce RTX 3080", 37.0, 10240 * MIB)
    ]
    assert server.hardware_updated_at is not None

    # Следующие опросы CPU/памяти идут как обычно, а df/nvidia-smi только через 10 с
    await rig.poll()
    await rig.poll()
    assert len(hardware_requests(rig)) == 1
    assert len(rig.monitor_requests()) == 3

    monkeypatch.setattr(settings, "MONITOR_HARDWARE_INTERVAL", 0.0)
    await rig.poll()
    assert len(hardware_requests(rig)) == 2


async def test_hardware_commands_do_not_reach_the_terminal(rig):
    session_id = await rig.open("server-a")
    await rig.poll()
    entries = rig.manager.get(session_id).buffer.since(0).entries
    assert [e.kind for e in entries] == ["opened"]
