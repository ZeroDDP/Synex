"""Ключи серверов, которым уже доверяет OpenSSH на этом компьютере.

Если пользователь уже подключался к серверу обычным `ssh`, ключ сервера записан в
known_hosts OpenSSH, и подтверждать его в Synex второй раз незачем. Когда воркер отвечает
host_key_unknown (в known_hosts.json backend сервера ещё нет), backend ищет ключ, который
показал сервер, в файлах OpenSSH (lookup):

  * trusted: ключ записан там для этого сервера. Backend сохраняет отпечаток у себя
    (known_hosts.json, source "openssh") и подключается;
  * changed: для сервера там записан другой ключ того же типа. `ssh` в этом случае
    пишет REMOTE HOST IDENTIFICATION HAS CHANGED; backend отвечает ssh_host_key_mismatch;
  * revoked: ключ помечен там как отозванный (@revoked): ssh_host_key_revoked;
  * unknown: сервера там нет или записаны только ключи других типов. Обычное
    подтверждение пользователем.

Файлы OpenSSH только читаются. Свой known_hosts.json важнее: если отпечаток сервера в нём
есть, в файлы OpenSSH backend не заглядывает.

Формат строки (sshd(8), «SSH_KNOWN_HOSTS FILE FORMAT»):

    [@cert-authority | @revoked] имена тип-ключа ключ-в-base64 [комментарий]

Имена идут через запятую, с шаблонами `*` и `?` и отрицанием `!`; сервер на
нестандартном порту записан как `[host]:port`. Либо имя одно и хешировано:
`|1|соль|HMAC-SHA1(соль, имя)`. Строки @cert-authority не учитываются: сертификаты
серверов воркер не проверяет.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from app.core.config import settings
from app.plugins.ssh.known_hosts import normalize_host

logger = structlog.get_logger()

DEFAULT_PORT = 22
MAX_FILE_BYTES = 4 * 1024 * 1024  # файл больше не читается: это не known_hosts
HASHED_PREFIX = "|1|"
REVOKED = "@revoked"
_BLANKS = re.compile(r"[ \t]+")

Verdict = Literal["trusted", "changed", "revoked", "unknown"]


@dataclass(frozen=True)
class HostKeyLine:
    """Строка known_hosts с ключом сервера."""

    path: Path
    number: int  # номер строки в файле, с 1
    names: str
    key_type: str
    fingerprint: str
    revoked: bool = False

    def matches(self, name: str) -> bool:
        """Строка относится к серверу name (`host` или `[host]:port`, строчными)."""
        if self.names.startswith(HASHED_PREFIX):
            return _hashed_name_matches(self.names, name)
        matched = False
        for pattern in self.names.split(","):
            negated = pattern.startswith("!")
            pattern = pattern[1:] if negated else pattern
            if pattern and wildcard_match(pattern.lower(), name):
                if negated:
                    return False  # отрицание сильнее совпадения с другим шаблоном
                matched = True
        return matched


@dataclass(frozen=True)
class Lookup:
    verdict: Verdict
    # Строка, по которой вынесен вердикт (None у unknown)
    line: HostKeyLine | None = None

    @property
    def where(self) -> str:
        """Файл и номер строки для сообщений."""
        return f"{self.line.path}:{self.line.number}" if self.line is not None else ""


def lookup(
    host: str,
    port: int,
    fingerprint: str,
    key_type: str = "",
    files: list[Path] | None = None,
) -> Lookup:
    """Что OpenSSH на этом компьютере знает о ключе fingerprint сервера host:port.
    key_type: тип ключа, который показал сервер (без него вердикта changed не бывает).
    files: какие файлы читать (по умолчанию из настроек). Читает файлы: вызывать в потоке."""
    host = normalize_host(host)
    primary = host_name(host, port)
    trusted: HostKeyLine | None = None
    same_type: HostKeyLine | None = None
    without_port: HostKeyLine | None = None
    for path in known_hosts_files() if files is None else files:
        for line in read_file(path):
            same_key = line.fingerprint == fingerprint
            if line.revoked:
                # Отозванный ключ не годится ни одному серверу, который подходит под
                # имена строки (обычно там "*")
                if same_key and (line.matches(primary) or line.matches(host)):
                    return Lookup("revoked", line)
            elif line.matches(primary):
                if same_key:
                    trusted = trusted or line
                elif key_type and line.key_type == key_type:
                    same_type = same_type or line
            elif port != DEFAULT_PORT and same_key and line.matches(host):
                without_port = without_port or line
    if trusted is not None:
        return Lookup("trusted", trusted)
    if same_type is not None:
        return Lookup("changed", same_type)
    if without_port is not None:
        # Как ssh: для сервера на нестандартном порту годится и запись без порта, если
        # ключ тот же (один сервер, sshd на двух портах)
        return Lookup("trusted", without_port)
    return Lookup("unknown")


def host_name(host: str, port: int) -> str:
    """Под каким именем OpenSSH записывает сервер: `host`, а с нестандартным портом
    `[host]:port`."""
    host = normalize_host(host)
    return host if port == DEFAULT_PORT else f"[{host}]:{port}"


def known_hosts_files() -> list[Path]:
    """Файлы known_hosts по настройкам: OPENSSH_KNOWN_HOSTS выключает поиск,
    OPENSSH_KNOWN_HOSTS_FILES задаёт свои файлы вместо стандартных."""
    if not settings.OPENSSH_KNOWN_HOSTS:
        return []
    custom = [item.strip() for item in settings.OPENSSH_KNOWN_HOSTS_FILES.split(",")]
    if any(custom):
        return [_expand(item) for item in custom if item]
    return default_files()


def default_files(*, windows: bool = sys.platform == "win32") -> list[Path]:
    """Где known_hosts ищет сам OpenSSH: файлы пользователя, потом общие для компьютера."""
    files: list[Path] = []
    try:
        ssh_dir = Path.home() / ".ssh"
        files += [ssh_dir / "known_hosts", ssh_dir / "known_hosts2"]
    except RuntimeError:  # домашняя папка неизвестна (служба без профиля)
        pass
    if windows:
        program_data = os.environ.get("PROGRAMDATA")
        if program_data:
            files.append(Path(program_data) / "ssh" / "ssh_known_hosts")
    else:
        files += [Path("/etc/ssh/ssh_known_hosts"), Path("/etc/ssh/ssh_known_hosts2")]
    return files


def read_file(path: Path) -> list[HostKeyLine]:
    """Строки с ключами серверов из файла known_hosts. Файла нет или его не прочитать:
    пустой список. Строки, которые не разобрать, пропускаются, как это делает ssh."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            logger.warning("OpenSSH known_hosts file is too large, skipped", path=str(path))
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = []
    # Как ssh: строку заканчивает только "\n". splitlines делит и по другим символам
    # (\x0b, \x85, \u2028): тогда хвост комментария читался бы как отдельная строка с ключом
    for number, raw in enumerate(text.split("\n"), 1):
        line = parse_line(path, number, raw)
        if line is not None:
            lines.append(line)
    return lines


def parse_line(path: Path, number: int, text: str) -> HostKeyLine | None:
    # Поля делят пробел и табуляция, как в ssh; "\r" в конце остаётся от файла с концами
    # строк Windows
    fields = [field for field in _BLANKS.split(text.strip(" \t\r")) if field]
    if not fields or fields[0].startswith("#"):
        return None
    revoked = False
    if fields[0].startswith("@"):
        if fields[0] != REVOKED:
            return None  # @cert-authority и пометки, которых мы не знаем
        revoked = True
        fields = fields[1:]
    if len(fields) < 3:
        return None
    names, key_type, encoded = fields[:3]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None  # в том числе строки SSH1: "имена биты экспонента модуль"
    # В самом ключе записан его тип: строка с другим типом испорчена
    if _blob_key_type(blob) != key_type:
        return None
    return HostKeyLine(path, number, names, key_type, fingerprint_of(blob), revoked)


def fingerprint_of(blob: bytes) -> str:
    """Отпечаток OpenSSH (как `ssh-keygen -lf`): SHA256 и base64 без "=" в конце."""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def wildcard_match(pattern: str, text: str) -> bool:
    """Шаблон OpenSSH: `*` любая строка (и пустая), `?` один символ. Скобки и точки
    обычные символы (поэтому не fnmatch: в `[host]:2222` скобки это часть имени). Время
    линейное при любом шаблоне."""
    p = t = 0
    star, mark = -1, 0
    while t < len(text):
        if p < len(pattern) and pattern[p] in ("?", text[t]):
            p += 1
            t += 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif star != -1:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _hashed_name_matches(names: str, name: str) -> bool:
    """`|1|соль|хеш`: хеш это HMAC-SHA1 имени сервера с солью (так пишет ssh с
    HashKnownHosts, по умолчанию в Debian и Ubuntu)."""
    parts = names.split("|")
    if len(parts) != 4:
        return False
    try:
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except (ValueError, binascii.Error):
        return False
    actual = hmac.new(salt, name.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(actual, expected)


def _blob_key_type(blob: bytes) -> str | None:
    """Тип ключа из самого ключа: он начинается со строки (длина 4 байта и текст)."""
    if len(blob) < 4:
        return None
    length = int.from_bytes(blob[:4], "big")
    if not 0 < length <= 64 or len(blob) < 4 + length:
        return None
    return blob[4 : 4 + length].decode("ascii", errors="replace")


def _expand(path: str) -> Path:
    """Путь из настроек: "~" это домашняя папка, относительный считается от backend/."""
    try:
        expanded = Path(path).expanduser()
    except RuntimeError:
        expanded = Path(path)
    return expanded if expanded.is_absolute() else settings.BASE_DIR / expanded
