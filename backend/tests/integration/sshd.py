"""SSH-сервер для интеграционных тестов.

Если заданы SYNEX_TEST_SSH_HOST / _USER / _KEY [/ _PORT / _PASSWORD], тесты идут к этому
серверу. Иначе на Linux и macOS поднимается свой sshd на 127.0.0.1 со случайным портом:
ключи генерируются ssh-keygen во временной папке, вход по ключу под текущим
пользователем, конфиг не трогает систему (StrictModes no, UsePAM no). Обычный ключ и
ключ с парольной фразой; пароль включается только для внешнего сервера.

Воркер (с 2026-11-14) подключается только к серверу с подтверждённым ключом (TOFU).
Отпечаток ключа своего sshd известен заранее (из host.pub); у внешнего сервера его
узнаёт первое подключение (фикстура ui в conftest.py), как это делает UI.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from tests.conftest import get_free_port

PASSPHRASE = "integration-passphrase"


@dataclass(frozen=True)
class SSHTarget:
    host: str
    port: int
    username: str
    key_path: str
    encrypted_key_path: str | None = None  # ключ с парольной фразой PASSPHRASE
    password: str | None = None
    # Отпечаток ключа сервера (SHA256:...), если известен заранее (свой sshd)
    host_key_fingerprint: str | None = None

    def connection(self, **auth: object) -> dict:
        """Тело POST /api/ssh/sessions (по умолчанию вход по обычному ключу)."""
        body: dict = {"host": self.host, "port": self.port, "username": self.username}
        body.update(auth or {"key_path": self.key_path})
        return body


def public_key_fingerprint(public_key_line: str) -> str:
    """Отпечаток OpenSSH (как `ssh-keygen -lf`) по строке открытого ключа "тип base64 ..."."""
    blob = base64.b64decode(public_key_line.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def external_target() -> SSHTarget | None:
    host = os.environ.get("SYNEX_TEST_SSH_HOST")
    if not host:
        return None
    return SSHTarget(
        host=host,
        port=int(os.environ.get("SYNEX_TEST_SSH_PORT", "22")),
        username=os.environ["SYNEX_TEST_SSH_USER"],
        key_path=os.environ["SYNEX_TEST_SSH_KEY"],
        password=os.environ.get("SYNEX_TEST_SSH_PASSWORD") or None,
    )


def find_sshd() -> str | None:
    if sys.platform == "win32":
        return None
    for candidate in (shutil.which("sshd"), "/usr/sbin/sshd", "/usr/local/sbin/sshd"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


class LocalSSHD:
    """sshd в отдельной папке: start() → SSHTarget, stop() останавливает процесс."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.process: subprocess.Popen | None = None
        self.log = workdir / "sshd.log"

    def start(self) -> SSHTarget:
        sshd = find_sshd()
        keygen = shutil.which("ssh-keygen")
        if sshd is None or keygen is None:
            raise RuntimeError("sshd or ssh-keygen not found")
        d = self.workdir
        d.mkdir(parents=True, exist_ok=True)
        for name, passphrase in (("host", ""), ("client", ""), ("client_enc", PASSPHRASE)):
            subprocess.run(
                [keygen, "-q", "-t", "ed25519", "-N", passphrase, "-C", name, "-f", d / name],
                check=True,
            )
        authorized = d / "authorized_keys"
        authorized.write_text((d / "client.pub").read_text() + (d / "client_enc.pub").read_text())
        for path in (d / "client", d / "client_enc", authorized):
            path.chmod(0o600)

        root = os.geteuid() == 0
        if root:
            Path("/run/sshd").mkdir(parents=True, exist_ok=True)  # privsep-папка Debian/Ubuntu
        port = get_free_port()
        config = d / "sshd_config"
        config.write_text(
            "\n".join(
                [
                    f"Port {port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {d / 'host'}",
                    f"PidFile {d / 'sshd.pid'}",
                    f"AuthorizedKeysFile {authorized}",
                    "StrictModes no",
                    "UsePAM no",
                    "PubkeyAuthentication yes",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    f"PermitRootLogin {'prohibit-password' if root else 'no'}",
                    "LogLevel VERBOSE",
                    # SFTP без отдельной программы sftp-server: плагин sftp ходит через неё
                    "Subsystem sftp internal-sftp",
                    "",
                ]
            )
        )
        self.process = subprocess.Popen(
            [sshd, "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=self.log.open("wb"),
        )
        self._wait_ready(port)
        return SSHTarget(
            host="127.0.0.1",
            port=port,
            username=getpass.getuser(),
            key_path=str(d / "client"),
            encrypted_key_path=str(d / "client_enc"),
            host_key_fingerprint=public_key_fingerprint((d / "host.pub").read_text()),
        )

    def _wait_ready(self, port: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"sshd exited: {self.log_tail()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as conn:
                    if conn.recv(64).startswith(b"SSH-"):
                        return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"sshd did not start: {self.log_tail()}")

    def log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
