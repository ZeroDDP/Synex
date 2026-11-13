"""Программы сервера для эмулятора shell, которые нужны плагину sftp: sha256sum и cp -R
(копирование папки). Работают на виртуальной ФС FakeShell."""

from __future__ import annotations

import hashlib
import posixpath
import shutil

from tests.fake_shell import FakeShell, Program


def sftp_programs(shell: FakeShell) -> dict[str, Program]:
    def real(cwd: str, path: str):  # noqa: ANN202
        return shell.root.joinpath(
            *[p for p in posixpath.normpath(posixpath.join(cwd, path)).split("/") if p]
        )

    def sha256sum(args: list[str], cwd: str, env: dict[str, str]) -> tuple[str, str, int]:
        lines = []
        for name in [a for a in args if a != "--"]:
            file = real(cwd, name)
            if not file.is_file():
                return "", f"sha256sum: {name}: No such file or directory\n", 1
            lines.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {name}\n")
        return "".join(lines), "", 0

    def cp(args: list[str], cwd: str, env: dict[str, str]) -> tuple[str, str, int]:
        paths = [a for a in args if not a.startswith("-")]
        recursive = any(a in ("-R", "-r") for a in args)
        source, target = real(cwd, paths[0]), real(cwd, paths[1])
        if not source.exists():
            return "", f"cp: cannot stat '{paths[0]}': No such file or directory\n", 1
        if source.is_dir():
            if not recursive:
                return "", f"cp: -r not specified; omitting directory '{paths[0]}'\n", 1
            if target.is_dir():
                target = target / source.name
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copyfile(source, target / source.name if target.is_dir() else target)
        return "", "", 0

    return {"sha256sum": sha256sum, "cp": cp}
