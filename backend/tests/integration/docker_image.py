"""Образ Docker для интеграционных тестов без скачивания из сети: `sh` и `sleep` этой
машины вместе с их библиотеками, собранные в архив и загруженные через `docker import`.
Контейнер из такого образа запускается, пишет в лог и переживает restart."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

# Раз в секунду строка в stdout, каждая пятая ещё и в stderr
TICKER = (
    'i=0; while :; do i=$((i+1)); echo "tick $i"; '
    '[ $((i % 5)) -eq 0 ] && echo "warn $i" >&2; sleep 1; done'
)


def docker_works() -> bool:
    if not shutil.which("docker") or not shutil.which("ldd"):
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _libraries(binary: str) -> list[str]:
    output = subprocess.run(["ldd", binary], capture_output=True, text=True).stdout
    return sorted(set(re.findall(r"(/\S+)\s+\(0x", output)))


def build_image(tag: str, workdir: Path) -> None:
    """Образ `tag` с /bin/sh и /bin/sleep."""
    files: dict[str, str] = {}
    for name in ("sh", "sleep"):
        found = shutil.which(name)
        assert found is not None, f"no {name} on this machine"
        binary = os.path.realpath(found)
        files[f"bin/{name}"] = binary
        for library in _libraries(binary):
            files[library.lstrip("/")] = os.path.realpath(library)
    archive = workdir / "rootfs.tar"
    with tarfile.open(archive, "w") as tar:
        for name, source in sorted(files.items()):
            tar.add(source, arcname=name)
    docker("import", str(archive), tag)


def run_ticker(name: str, image: str) -> None:
    """Контейнер, который каждую секунду пишет строку в лог. Сеть ему не нужна."""
    docker("run", "-d", "--name", name, "--network", "none", "--label", "synex.test=1", image,
           "/bin/sh", "-c", TICKER)  # fmt: skip


def remove(*names: str, image: str | None = None) -> None:
    for name in names:
        docker("rm", "-f", name, check=False)
    if image is not None:
        docker("rmi", "-f", image, check=False)
