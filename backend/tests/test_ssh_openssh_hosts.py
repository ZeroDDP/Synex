"""Ключи серверов из known_hosts OpenSSH (app/plugins/ssh/openssh_hosts.py): сервер, к
которому уже подключались через ssh, в Synex подтверждать второй раз не нужно.

Файл OpenSSH в тестах свой (tests/conftest.py подменяет OPENSSH_KNOWN_HOSTS_FILES), а
фейковый воркер ведёт себя как сборка 2026-11-14: без отпечатка не подключается.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.plugins.ssh import openssh_hosts
from app.plugins.ssh.known_hosts import KnownHostsStore, get_known_hosts
from app.plugins.ssh.openssh_hosts import fingerprint_of, lookup, wildcard_match
from tests.fake_worker import FAKE_KEY_TYPE
from tests.ssh_helpers import make_api

HOST = "10.0.0.1"


def key_blob(seed: str, key_type: str = "ssh-ed25519") -> bytes:
    """Ключ сервера в формате SSH: строка с типом, потом данные ключа."""
    name = key_type.encode()
    data = hashlib.sha256(seed.encode()).digest()
    return len(name).to_bytes(4, "big") + name + len(data).to_bytes(4, "big") + data


def key_line(names: str, seed: str, key_type: str = "ssh-ed25519", marker: str = "") -> str:
    encoded = base64.b64encode(key_blob(seed, key_type)).decode()
    return f"{marker + ' ' if marker else ''}{names} {key_type} {encoded} comment\n"


def fp(seed: str, key_type: str = "ssh-ed25519") -> str:
    return fingerprint_of(key_blob(seed, key_type))


def hashed(name: str, salt: bytes = b"0123456789abcdefghij") -> str:
    digest = hmac.new(salt, name.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"


@pytest.fixture
def known_hosts() -> Path:
    """Файл known_hosts OpenSSH этого теста (пока не создан)."""
    path = Path(settings.OPENSSH_KNOWN_HOSTS_FILES)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ lookup


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen (OpenSSH)")
def test_agrees_with_openssh_itself(tmp_path: Path):
    """Сверка с настоящим OpenSSH: отпечаток как у `ssh-keygen -lf`, а имена, которые
    захешировал `ssh-keygen -H` (так их пишет ssh с HashKnownHosts), находятся."""

    def keygen(*args: str) -> str:
        try:
            done = subprocess.run(
                ["ssh-keygen", *args], capture_output=True, text=True, timeout=60, check=True
            )
        except (OSError, subprocess.SubprocessError) as e:
            pytest.skip(f"ssh-keygen does not work here: {e}")
        return done.stdout

    keys = {}
    for kind in ("ed25519", "ecdsa", "rsa"):
        keygen("-q", "-t", kind, "-N", "", "-C", "test", "-f", str(tmp_path / kind))
        key_type, encoded = (tmp_path / f"{kind}.pub").read_text().split()[:2]
        printed = keygen("-l", "-f", str(tmp_path / f"{kind}.pub")).split()[1]
        assert fingerprint_of(base64.b64decode(encoded)) == printed
        keys[kind] = (key_type, encoded, printed)

    file = tmp_path / "known_hosts"
    file.write_text(
        f"example.com,10.0.0.1 {keys['ed25519'][0]} {keys['ed25519'][1]}\n"
        f"[example.com]:2222 {keys['ecdsa'][0]} {keys['ecdsa'][1]}\n"
        f"2001:db8::1 {keys['rsa'][0]} {keys['rsa'][1]}\n",
        encoding="utf-8",
        newline="\n",  # и на Windows: ssh-keygen не читает строки с \r\n
    )
    keygen("-H", "-f", str(file))  # имена становятся |1|соль|хеш
    assert "example.com" not in file.read_text(encoding="utf-8")
    assert all(line.names.startswith("|1|") for line in openssh_hosts.read_file(file))
    for host, port, kind in [
        ("example.com", 22, "ed25519"),
        ("10.0.0.1", 22, "ed25519"),
        ("example.com", 2222, "ecdsa"),
        ("2001:db8::1", 22, "rsa"),
    ]:
        key_type, _, printed = keys[kind]
        assert lookup(host, port, printed, key_type, [file]).verdict == "trusted", (host, port)
    assert lookup("example.org", 22, keys["ed25519"][2], "ssh-ed25519", [file]).verdict == "unknown"


def test_plain_names_ports_and_lists(known_hosts: Path):
    known_hosts.write_text(
        "# comment\n\n"
        + key_line("example.com,10.0.0.1", "a")
        + key_line("[example.com]:2222", "b")
        + key_line("2001:db8::1", "c")
        + key_line("[2001:db8::1]:2200", "d")
        + key_line("Mixed.Example.COM", "e"),
        encoding="utf-8",
    )
    files = [known_hosts]
    for host, port, seed in [
        ("example.com", 22, "a"),
        ("10.0.0.1", 22, "a"),
        (" EXAMPLE.com ", 22, "a"),  # регистр и пробелы не важны
        ("example.com", 2222, "b"),
        ("2001:db8::1", 22, "c"),
        ("[2001:DB8::1]", 22, "c"),
        ("2001:db8::1", 2200, "d"),
        ("mixed.example.com", 22, "e"),
    ]:
        found = lookup(host, port, fp(seed), "ssh-ed25519", files)
        assert found.verdict == "trusted", (host, port)
        assert found.line is not None and found.line.path == known_hosts
    found = lookup("example.com", 22, fp("a"), "ssh-ed25519", files)
    assert found.where == f"{known_hosts}:3"  # номер строки для сообщения

    # Файл с концами строк Windows читается так же
    known_hosts.write_bytes(key_line("example.com", "a").replace("\n", "\r\n").encode() * 2)
    assert [line.number for line in openssh_hosts.read_file(known_hosts)] == [1, 2]
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", files).verdict == "trusted"
    known_hosts.write_text(
        "# comment\n\n"
        + key_line("example.com,10.0.0.1", "a")
        + key_line("[example.com]:2222", "b"),
        encoding="utf-8",
    )

    # Другой сервер и другой ключ: OpenSSH о них не знает
    assert lookup("other.com", 22, fp("a"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("10.0.0.1", 2222, fp("b"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("example.com", 22, fp("x"), "ssh-rsa", files).verdict == "unknown"
    assert lookup("example.com", 22, fp("x"), "", files).verdict == "unknown"


def test_changed_key(known_hosts: Path):
    known_hosts.write_text(
        key_line("example.com", "old") + key_line("example.com", "rsa", "ssh-rsa"),
        encoding="utf-8",
    )
    files = [known_hosts]
    changed = lookup("example.com", 22, fp("new"), "ssh-ed25519", files)
    assert changed.verdict == "changed"
    assert changed.line is not None and changed.line.fingerprint == fp("old")
    # Тип ключа, которого в файле нет, это новый ключ, а не смена: ssh спросил бы
    assert lookup("example.com", 22, fp("new"), "ecdsa-sha2-nistp256", files).verdict == "unknown"
    # У сервера может быть записано несколько ключей одного типа (смена по плану)
    known_hosts.write_text(
        key_line("example.com", "old") + key_line("example.com", "new"), encoding="utf-8"
    )
    assert lookup("example.com", 22, fp("new"), "ssh-ed25519", files).verdict == "trusted"


def test_another_port_of_a_known_server(known_hosts: Path):
    """Как ssh: для нестандартного порта годится запись сервера без порта с тем же
    ключом. Но запись именно этого порта с другим ключом значит, что ключ сменился."""
    known_hosts.write_text(key_line("example.com", "a"), encoding="utf-8")
    files = [known_hosts]
    assert lookup("example.com", 2222, fp("a"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("example.com", 2222, fp("other"), "ssh-ed25519", files).verdict == "unknown"
    known_hosts.write_text(
        key_line("example.com", "a") + key_line("[example.com]:2222", "b"), encoding="utf-8"
    )
    assert lookup("example.com", 2222, fp("a"), "ssh-ed25519", files).verdict == "changed"
    # Запись порта 2222 ничего не говорит о порте 22
    assert lookup("example.com", 22, fp("b"), "ssh-ed25519", files).verdict == "changed"
    assert lookup("example.com", 22, fp("b"), "ssh-rsa", files).verdict == "unknown"


def test_hashed_names(known_hosts: Path):
    """HashKnownHosts (по умолчанию в Debian и Ubuntu): вместо имени HMAC-SHA1 с солью."""
    known_hosts.write_text(
        key_line(hashed("example.com"), "a")
        + key_line(hashed("[example.com]:2222", b"another salt 12345678"), "b")
        + key_line("|1|not_base64!|x", "c")
        + key_line("|1|onlytwo", "d"),
        encoding="utf-8",
    )
    files = [known_hosts]
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("Example.com", 2222, fp("b"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("example.org", 22, fp("a"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("example.com", 22, fp("b"), "ssh-ed25519", files).verdict == "changed"
    assert lookup("example.com", 22, fp("c"), "ssh-rsa", files).verdict == "unknown"


def test_patterns_and_negation(known_hosts: Path):
    known_hosts.write_text(
        key_line("*.example.com,!bad.example.com", "wild") + key_line("192.168.1.?", "lan"),
        encoding="utf-8",
    )
    files = [known_hosts]
    assert lookup("web.example.com", 22, fp("wild"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("bad.example.com", 22, fp("wild"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("example.com", 22, fp("wild"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("192.168.1.7", 22, fp("lan"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("192.168.1.77", 22, fp("lan"), "ssh-ed25519", files).verdict == "unknown"
    # Шаблон, под который подходит сервер, с другим ключом: ключ сервера сменился
    assert lookup("web.example.com", 22, fp("lan"), "ssh-ed25519", files).verdict == "changed"

    known_hosts.write_text(key_line("!*.internal,*", "any"), encoding="utf-8")
    assert lookup("anything", 22, fp("any"), "ssh-ed25519", files).verdict == "trusted"
    assert lookup("db.internal", 22, fp("any"), "ssh-ed25519", files).verdict == "unknown"


def test_wildcards():
    assert wildcard_match("*", "") and wildcard_match("*", "anything")
    assert wildcard_match("a*c", "abc") and wildcard_match("a*c", "ac")
    assert not wildcard_match("a*c", "abd") and not wildcard_match("a?c", "ac")
    assert wildcard_match("[host]:2222", "[host]:2222")  # скобки это обычные символы
    assert not wildcard_match("[host]:2222", "h:2222")
    assert wildcard_match("*.example.*", "a.example.com")
    assert not wildcard_match("", "x") and wildcard_match("", "")
    # Шаблон, на котором перебор с возвратами работал бы минуты
    started = time.monotonic()
    assert not wildcard_match("*a" * 40 + "b", "a" * 2000)
    assert time.monotonic() - started < 2


def test_revoked_and_certificate_lines(known_hosts: Path):
    known_hosts.write_text(
        key_line("example.com", "stolen")
        + key_line("*", "stolen", marker="@revoked")
        + key_line("other.com", "limited", marker="@revoked")
        + key_line("*.example.com", "ca", marker="@cert-authority")
        + key_line("example.com", "odd", marker="@something-new"),
        encoding="utf-8",
    )
    files = [known_hosts]
    # Отозванный ключ не годится, даже если строкой выше он записан как доверенный
    assert lookup("example.com", 22, fp("stolen"), "ssh-ed25519", files).verdict == "revoked"
    assert lookup("example.com", 2222, fp("stolen"), "ssh-ed25519", files).verdict == "revoked"
    assert lookup("other.com", 22, fp("limited"), "ssh-ed25519", files).verdict == "revoked"
    assert lookup("example.com", 22, fp("limited"), "ssh-ed25519", files).verdict == "changed"
    # Ключ центра сертификации не ключ сервера; незнакомые пометки не читаются
    assert lookup("web.example.com", 22, fp("ca"), "ssh-ed25519", files).verdict == "unknown"
    assert lookup("example.com", 22, fp("odd"), "ssh-rsa", files).verdict == "unknown"


def test_broken_lines_and_files(known_hosts: Path, tmp_path: Path):
    good = key_line("example.com", "a")
    wrong_type = key_line("example.com", "b", "ssh-rsa").replace(" ssh-rsa ", " ssh-ed25519 ")
    known_hosts.write_bytes(
        b"\xff\xfe garbage \x00\n"
        + b"example.com ssh-ed25519\n"  # нет ключа
        + b"example.com ssh-ed25519 not-base64!!\n"
        + b"example.com 1024 65537 1234567890\n"  # строка SSH1
        + b"example.com ssh-ed25519 AAAA\n"  # ключ слишком короткий
        + wrong_type.encode()  # тип в строке не тот, что в самом ключе
        + good.encode()
    )
    assert [line.number for line in openssh_hosts.read_file(known_hosts)] == [7]
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", [known_hosts]).verdict == "trusted"
    assert lookup("example.com", 22, fp("b", "ssh-rsa"), "ssh-rsa", [known_hosts]).verdict == (
        "unknown"
    )

    missing, folder = tmp_path / "no-such-file", tmp_path / "folder"
    folder.mkdir()
    assert openssh_hosts.read_file(missing) == [] and openssh_hosts.read_file(folder) == []
    # Несколько файлов: читаются все, которые есть
    files = [missing, folder, known_hosts]
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", files).verdict == "trusted"

    huge = tmp_path / "huge"
    with huge.open("wb") as file:
        file.write(good.encode())
        file.truncate(openssh_hosts.MAX_FILE_BYTES + 1)
    assert openssh_hosts.read_file(huge) == []


def test_lines_and_fields_are_split_as_ssh_splits_them(known_hosts: Path):
    """Строку заканчивает только перевод строки, поля делят только пробел и табуляция.
    Иначе хвост комментария после редкого разделителя (в комментарий попадает и то, что
    прислал сервер: ssh-keyscan пишет туда его баннер) читался бы как ещё одна строка с
    ключом, которой в файле для ssh нет."""
    hidden = key_line("victim.example", "evil").rstrip("\n")
    for separator in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
        known_hosts.write_text(
            f"# example.com:22 SSH-2.0-banner{separator}{hidden}\n"
            + key_line("example.com", "a").replace(" comment", f" note{separator}{hidden}"),
            encoding="utf-8",
        )
        assert [line.number for line in openssh_hosts.read_file(known_hosts)] == [2], separator
        found = lookup("victim.example", 22, fp("evil"), "ssh-ed25519", [known_hosts])
        assert found.verdict == "unknown", separator
        assert lookup("example.com", 22, fp("a"), "ssh-ed25519", [known_hosts]).verdict == "trusted"

    # Такой символ внутри имени сервера поле не делит: строка не про example.com
    known_hosts.write_text(key_line("example.com other", "a"), encoding="utf-8")
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", [known_hosts]).verdict == "unknown"
    # Поля через табуляцию и несколько пробелов читаются
    known_hosts.write_text(
        "  " + key_line("example.com", "a").replace(" ", " \t "), encoding="utf-8"
    )
    assert lookup("example.com", 22, fp("a"), "ssh-ed25519", [known_hosts]).verdict == "trusted"


def test_which_files_are_read(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS_FILES", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    user_files = [
        tmp_path / "home" / ".ssh" / "known_hosts",
        tmp_path / "home" / ".ssh" / "known_hosts2",
    ]
    assert openssh_hosts.default_files(windows=False) == [
        *user_files,
        Path("/etc/ssh/ssh_known_hosts"),
        Path("/etc/ssh/ssh_known_hosts2"),
    ]
    assert openssh_hosts.default_files(windows=True) == [
        *user_files,
        tmp_path / "ProgramData" / "ssh" / "ssh_known_hosts",
    ]
    # Без настройки читаются стандартные файлы этой ОС
    assert openssh_hosts.known_hosts_files() == openssh_hosts.default_files()
    assert openssh_hosts.known_hosts_files()[:2] == user_files

    # Свои файлы вместо стандартных; относительный путь считается от backend/
    one, two = tmp_path / "one", tmp_path / "two"
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS_FILES", f" {one} , {two},, relative/file ")
    assert openssh_hosts.known_hosts_files() == [
        one,
        two,
        settings.BASE_DIR / "relative" / "file",
    ]
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS", False)
    assert openssh_hosts.known_hosts_files() == []


# --------------------------------------------------------------------- API


@pytest.fixture
def connect_body(tmp_path: Path) -> dict:
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    return {"host": HOST, "username": "root", "key_path": str(key)}


@pytest.fixture
async def ssh(fake_worker, missing_socket_path):
    fake_worker.host_key_mode = "required"
    client, harness = await make_api(fake_worker.port, missing_socket_path)
    async with client:
        harness.api = client
        yield harness
    await harness.disconnect()


def connects(fake_worker) -> list[dict]:
    return [
        r["payload"]["connection"] for r in fake_worker.requests if r["action"] == "ssh_connect"
    ]


def serve_key(fake_worker, seed: str, port: int = 22) -> str:
    """Сервер HOST:port показывает ключ seed. Возвращает его отпечаток."""
    fake_worker.host_keys[(HOST, port)] = fp(seed, FAKE_KEY_TYPE)
    return fp(seed, FAKE_KEY_TYPE)


async def test_server_known_to_openssh_connects_without_asking(
    ssh, fake_worker, connect_body, known_hosts: Path
):
    real = serve_key(fake_worker, "server")
    known_hosts.write_text(
        key_line("elsewhere.example", "x") + key_line(hashed(HOST), "server", FAKE_KEY_TYPE),
        encoding="utf-8",
    )

    opened = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert opened.status_code == 201, opened.text
    assert opened.json()["host_key"] == {"fingerprint": real, "key_type": FAKE_KEY_TYPE}
    # Воркер сначала показал ключ, потом получил его отпечаток: два подключения
    attempts = connects(fake_worker)
    assert len(attempts) == 2
    assert "host_key_fingerprint" not in attempts[0]
    assert attempts[1]["host_key_fingerprint"] == real

    # Ключ сохранён у backend, и видно, откуда он взялся
    [known] = (await ssh.api.get("/api/ssh/known-hosts")).json()
    assert (known["host"], known["port"], known["fingerprint"]) == (HOST, 22, real)
    assert (known["key_type"], known["source"]) == (FAKE_KEY_TYPE, "openssh")

    # Дальше файл OpenSSH не нужен: подключение одно, с сохранённым отпечатком
    known_hosts.unlink()
    again = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert again.status_code == 201, again.text
    assert len(connects(fake_worker)) == 3
    assert len((await ssh.api.get("/api/ssh/sessions")).json()) == 2

    # Пользователь подтвердил тот же ключ сам: запись становится его
    confirmed = await ssh.api.post(
        "/api/ssh/known-hosts", json={"host": HOST, "fingerprint": real, "key_type": FAKE_KEY_TYPE}
    )
    assert confirmed.status_code == 201 and confirmed.json()["changed"] is True
    assert confirmed.json()["known_host"]["source"] == "user"


async def test_the_old_connect_endpoint_and_another_port(
    ssh, fake_worker, connect_body, known_hosts: Path
):
    real = serve_key(fake_worker, "server", port=2222)
    known_hosts.write_text(key_line(f"[{HOST}]:2222", "server", FAKE_KEY_TYPE), encoding="utf-8")
    response = await ssh.api.post("/api/ssh/connect", json={**connect_body, "port": 2222})
    assert response.status_code == 201, response.text
    [known] = (await ssh.api.get("/api/ssh/known-hosts")).json()
    assert (known["port"], known["fingerprint"], known["source"]) == (2222, real, "openssh")
    # Порт 22 того же сервера OpenSSH не знает: обычное подтверждение
    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert (unknown.status_code, unknown.json()["error"]) == (409, "ssh_host_key_unknown")
    assert "source" not in unknown.json()["host_key"]


async def test_server_unknown_to_openssh_is_confirmed_by_the_user(
    ssh, fake_worker, connect_body, known_hosts: Path
):
    real = serve_key(fake_worker, "server")
    known_hosts.write_text(
        key_line("other.example", "server", FAKE_KEY_TYPE)  # тот же ключ, но другой сервер
        + key_line(HOST, "rsa", "ssh-rsa"),  # этот сервер, но ключ другого типа
        encoding="utf-8",
    )
    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert unknown.status_code == 409, unknown.text
    assert unknown.json()["error"] == "ssh_host_key_unknown"
    assert unknown.json()["host_key"] == {
        "host": HOST,
        "port": 22,
        "fingerprint": real,
        "key_type": FAKE_KEY_TYPE,
    }
    assert len(connects(fake_worker)) == 1
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []


async def test_key_that_differs_from_openssh_is_a_changed_key(
    ssh, fake_worker, connect_body, known_hosts: Path
):
    """OpenSSH знает сервер с другим ключом того же типа: ssh написал бы REMOTE HOST
    IDENTIFICATION HAS CHANGED. Backend не подключается и ничего не сохраняет."""
    shown = serve_key(fake_worker, "new server")
    known_hosts.write_text(key_line(HOST, "old server", FAKE_KEY_TYPE), encoding="utf-8")
    changed = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert changed.status_code == 409, changed.text
    body = changed.json()
    assert body["error"] == "ssh_host_key_mismatch"
    assert body["host_key"] == {
        "host": HOST,
        "port": 22,
        "fingerprint": shown,
        "key_type": FAKE_KEY_TYPE,
        "expected_fingerprint": fp("old server", FAKE_KEY_TYPE),
        "source": "openssh",
    }
    assert "man in the middle" in body["detail"] and f"{known_hosts}:1" in body["detail"]
    assert len(connects(fake_worker)) == 1  # без повтора
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []
    assert (await ssh.api.get("/api/ssh/sessions")).json() == []

    # Пользователь подтвердил новый ключ (кнопка «Заменить ключ» в UI): теперь решает
    # запись backend, файл OpenSSH больше не спрашивается
    replaced = await ssh.api.post(
        "/api/ssh/known-hosts",
        json={"host": HOST, "fingerprint": shown, "key_type": FAKE_KEY_TYPE, "replace": True},
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["known_host"]["source"] == "user"
    assert (await ssh.api.post("/api/ssh/sessions", json=connect_body)).status_code == 201


async def test_revoked_key_is_refused(ssh, fake_worker, connect_body, known_hosts: Path):
    shown = serve_key(fake_worker, "stolen")
    known_hosts.write_text(
        key_line(HOST, "stolen", FAKE_KEY_TYPE)
        + key_line("*", "stolen", FAKE_KEY_TYPE, marker="@revoked"),
        encoding="utf-8",
    )
    refused = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error"] == "ssh_host_key_revoked"
    assert body["host_key"]["fingerprint"] == shown and body["host_key"]["source"] == "openssh"
    assert "revoked" in body["detail"] and f"{known_hosts}:2" in body["detail"]
    assert len(connects(fake_worker)) == 1
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []


async def test_own_record_wins_over_openssh(ssh, fake_worker, connect_body, known_hosts: Path):
    """Если отпечаток сервера у backend уже есть, файл OpenSSH не спрашивается: ключ,
    который не совпал с подтверждённым, не становится доверенным из-за записи там."""
    confirmed = fp("confirmed by the user", FAKE_KEY_TYPE)
    await ssh.api.post("/api/ssh/known-hosts", json={"host": HOST, "fingerprint": confirmed})
    shown = serve_key(fake_worker, "another key")
    known_hosts.write_text(key_line(HOST, "another key", FAKE_KEY_TYPE), encoding="utf-8")
    changed = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert (changed.status_code, changed.json()["error"]) == (409, "ssh_host_key_mismatch")
    assert changed.json()["host_key"] == {
        "host": HOST,
        "port": 22,
        "fingerprint": shown,
        "key_type": FAKE_KEY_TYPE,
        "expected_fingerprint": confirmed,
    }
    assert len(connects(fake_worker)) == 1
    [known] = (await ssh.api.get("/api/ssh/known-hosts")).json()
    assert (known["fingerprint"], known["source"]) == (confirmed, "user")


async def test_lookup_can_be_switched_off(
    ssh, fake_worker, connect_body, known_hosts: Path, monkeypatch
):
    serve_key(fake_worker, "server")
    known_hosts.write_text(key_line(HOST, "server", FAKE_KEY_TYPE), encoding="utf-8")
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS", False)
    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert (unknown.status_code, unknown.json()["error"]) == (409, "ssh_host_key_unknown")
    assert len(connects(fake_worker)) == 1
    monkeypatch.setattr(settings, "OPENSSH_KNOWN_HOSTS", True)
    assert (await ssh.api.post("/api/ssh/sessions", json=connect_body)).status_code == 201


async def test_a_failed_lookup_falls_back_to_asking_the_user(
    ssh, fake_worker, connect_body, known_hosts: Path, monkeypatch
):
    """Поиск в файлах OpenSSH это удобство: если он сломался, подключение отвечает как
    без него, а не ошибкой сервера."""
    real = serve_key(fake_worker, "server")
    known_hosts.write_text(key_line(HOST, "server", FAKE_KEY_TYPE), encoding="utf-8")

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(openssh_hosts, "lookup", broken)
    unknown = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert (unknown.status_code, unknown.json()["error"]) == (409, "ssh_host_key_unknown")
    assert unknown.json()["host_key"]["fingerprint"] == real
    assert (await ssh.api.get("/api/ssh/known-hosts")).json() == []


async def test_second_attempt_that_fails_is_reported_as_is(
    ssh, fake_worker, connect_body, known_hosts: Path, monkeypatch
):
    """Ключ найден в OpenSSH, но при втором подключении сервер показал уже другой:
    вторая попытка последняя, её ошибка уходит в UI."""
    real = serve_key(fake_worker, "server")
    known_hosts.write_text(key_line(HOST, "server", FAKE_KEY_TYPE), encoding="utf-8")
    store = get_known_hosts()
    save = store.trust

    async def save_then_swap_the_key(*args: object, **kwargs: object) -> object:
        result = await save(*args, **kwargs)  # type: ignore[arg-type]
        serve_key(fake_worker, "swapped")
        return result

    monkeypatch.setattr(store, "trust", save_then_swap_the_key)
    failed = await ssh.api.post("/api/ssh/sessions", json=connect_body)
    assert (failed.status_code, failed.json()["error"]) == (409, "ssh_host_key_mismatch")
    assert failed.json()["host_key"]["expected_fingerprint"] == real
    assert len(connects(fake_worker)) == 2
    assert (await ssh.api.get("/api/ssh/sessions")).json() == []


# ------------------------------------------------------------------- store


async def test_store_keeps_the_source(tmp_path: Path):
    path = tmp_path / "known_hosts.json"
    store = KnownHostsStore(path)
    one, two = fp("one"), fp("two")
    record, changed = await store.trust("example.com", 22, one, "ssh-ed25519", source="openssh")
    assert changed and record.source == "openssh"
    # Повторная находка в OpenSSH ничего не меняет
    assert (await store.trust("example.com", 22, one, "ssh-ed25519", source="openssh"))[1] is False
    record, changed = await store.trust("example.com", 22, one)
    assert changed and record.source == "user"
    # Запись пользователя находка в OpenSSH не понижает
    record, changed = await store.trust("example.com", 22, one, source="openssh")
    assert not changed and record.source == "user"
    record, changed = await store.trust("example.com", 22, two, replace=True, source="openssh")
    assert changed and (record.fingerprint, record.source) == (two, "openssh")
    assert json.loads(path.read_text(encoding="utf-8"))["hosts"][0]["source"] == "openssh"

    # Файл до появления поля source: все записи от пользователя
    content = json.loads(path.read_text(encoding="utf-8"))
    del content["hosts"][0]["source"]
    path.write_text(json.dumps(content), encoding="utf-8")
    [old] = await KnownHostsStore(path).list_hosts()
    assert old.source == "user"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
