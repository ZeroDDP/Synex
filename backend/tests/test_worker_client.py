from __future__ import annotations

import asyncio
import socket
import struct
import sys
import time

import pytest
from structlog.testing import capture_logs

from app.core.errors import WorkerCommandError, WorkerConnectionError, WorkerTimeoutError
from app.worker.client import WorkerClient
from tests.conftest import get_free_port, start_fake_worker, unique_pipe_name
from tests.fake_worker import NO_SESSION, FakeWorkerProtocol, FakeWorkerState

CONNECTION = {"connection": {"host": "10.0.0.1", "port": 22, "username": "root", "key_path": "/k"}}


async def make_client(port: int, missing_socket_path: str, timeout: float = 2.0) -> WorkerClient:
    client = WorkerClient(
        pipe_name=unique_pipe_name(),
        socket_path=missing_socket_path,
        tcp_port=port,
        request_timeout=timeout,
    )
    await client.connect(timeout=3.0, prefer="tcp")
    return client


async def test_protocol_session_is_request_id(fake_worker, missing_socket_path, tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("key")
    connection = {"connection": {**CONNECTION["connection"], "key_path": str(key)}}
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerCommandError) as excinfo:
            await client.request("ssh_exec", {"command": "ls"}, request_id="s1")
        assert excinfo.value.stderr == NO_SESSION
        assert excinfo.value.exit_code == 1

        result = await client.request("ssh_connect", connection, request_id="s1")
        assert result["stdout"] == "Connected successfully, id: s1"

        result = await client.request("ssh_exec", {"command": "ls -la /tmp"}, request_id="s1")
        assert result == {"stdout": "ran: ls -la /tmp\n", "stderr": "", "exit_code": 0}

        # Без id сессии запрос получает случайный id и сессию не находит
        with pytest.raises(WorkerCommandError, match="Session dont founded"):
            await client.request("ssh_exec", {"command": "ls"})
    finally:
        await client.disconnect()

    requests = fake_worker.requests
    assert [r["action"] for r in requests] == ["ssh_exec", "ssh_connect", "ssh_exec", "ssh_exec"]
    assert [r["id"] for r in requests[:3]] == ["s1", "s1", "s1"]
    assert len(requests[3]["id"]) == 32  # uuid4().hex
    assert requests[1]["payload"] == connection


async def test_replies_with_same_id_match_in_order(fake_worker, missing_socket_path):
    """Два запроса одной сессии в полёте: ответы с одинаковым id разбираются по порядку."""
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        results = await asyncio.gather(
            client.request("test_sleep", {"seconds": 0.2, "echo": "first"}, request_id="s1"),
            client.request("test_sleep", {"seconds": 0, "echo": "second"}, request_id="s1"),
            client.request("test_sleep", {"seconds": 0, "echo": "third"}, request_id="s1"),
        )
        assert [r["stdout"] for r in results] == ["first", "second", "third"]
        assert client.status()["pending_requests"] == 0
    finally:
        await client.disconnect()


async def test_late_reply_is_not_given_to_next_request_with_same_id(
    fake_worker, missing_socket_path
):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerTimeoutError):
            await client.request(
                "test_sleep", {"seconds": 0.4, "echo": "late"}, request_id="s1", timeout=0.1
            )
        # Ответ "late" ещё не пришёл. Следующий запрос сессии должен получить свой ответ.
        result = await client.request("test_sleep", {"seconds": 0, "echo": "mine"}, request_id="s1")
        assert result["stdout"] == "mine"
        assert client.status()["pending_requests"] == 0
    finally:
        await client.disconnect()


async def test_cancelled_request_keeps_its_place(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        task = asyncio.create_task(
            client.request("test_sleep", {"seconds": 0.3, "echo": "cancelled"}, request_id="s1")
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await client.request("test_sleep", {"seconds": 0, "echo": "next"}, request_id="s1")
        assert result["stdout"] == "next"
    finally:
        await client.disconnect()


async def test_unknown_action_is_worker_error(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerCommandError, match="Unknown action: docker_list"):
            await client.request("docker_list", {"path": "/"})
        assert client.connected
    finally:
        await client.disconnect()


async def test_concurrent_requests_complete_out_of_order(missing_socket_path):
    """Если воркер научится выполнять запросы параллельно, ответы придут вразнобой."""
    state = FakeWorkerState(sequential=False)
    server = await start_fake_worker(state)
    client = await make_client(state.port, missing_socket_path)
    try:
        results = await asyncio.gather(
            client.request("test_sleep", {"seconds": 0.3, "echo": "slow"}),
            client.request("test_sleep", {"seconds": 0.01, "echo": "fast"}),
            *[client.request("test_sleep", {"seconds": 0.05, "echo": str(i)}) for i in range(20)],
        )
        assert [r["stdout"] for r in results] == ["slow", "fast", *[str(i) for i in range(20)]]
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()


async def test_timeout_does_not_break_connection(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerTimeoutError):
            await client.request("test_sleep", {"seconds": 0.5, "echo": "late"}, timeout=0.1)
        assert client.connected

        # Поздний ответ на просроченный запрос не должен сбить разбор потока
        await asyncio.sleep(0.6)
        result = await client.request("test_sleep", {"seconds": 0, "echo": "next"})
        assert result["stdout"] == "next"
        assert client.status()["pending_requests"] == 0
    finally:
        await client.disconnect()


async def test_request_cancelled_before_sending_does_not_take_a_reply(
    fake_worker, missing_socket_path
):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        await client._write_lock.acquire()  # запись занята: запрос ждёт очереди
        waiting = asyncio.create_task(
            client.request("test_sleep", {"echo": "never sent"}, request_id="s1")
        )
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        client._write_lock.release()

        result = await client.request("test_sleep", {"echo": "mine"}, request_id="s1")
        assert result["stdout"] == "mine"
        assert [r["payload"].get("echo") for r in fake_worker.requests] == ["mine"]
    finally:
        await client.disconnect()


async def test_large_payload_framing(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path, timeout=10)
    try:
        result = await client.request("test_big", {"size": 5_000_000})
        assert len(result["stdout"]) == 5_000_000
        result = await client.request("test_sleep", {"seconds": 0, "echo": "after-big"})
        assert result["stdout"] == "after-big"
    finally:
        await client.disconnect()


async def test_connection_loss_fails_pending_immediately(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path, timeout=10)
    lost: list[str] = []
    client.add_disconnect_listener(lost.append)

    slow = asyncio.create_task(client.request("test_sleep", {"seconds": 5}))
    await asyncio.sleep(0.05)

    started = time.monotonic()
    fake_worker.close_all()  # воркер закрыл соединение, не ответив
    with pytest.raises(WorkerConnectionError) as excinfo:
        await slow
    assert not isinstance(excinfo.value, WorkerTimeoutError)
    assert time.monotonic() - started < 1.0

    assert not client.connected
    assert len(lost) == 1
    with pytest.raises(WorkerConnectionError, match="Not connected"):
        await client.request("test_sleep", {"seconds": 0})

    # Тот же объект клиента умеет переподключиться
    await client.connect(timeout=2.0, prefer="tcp")
    assert (await client.request("test_sleep", {"seconds": 0, "echo": "again"}))[
        "stdout"
    ] == "again"
    await client.disconnect()


async def test_connection_reset_by_dying_worker_is_not_an_error(fake_worker, missing_socket_path):
    """Упавший воркер часто обрывает соединение RST, а не EOF: это потеря соединения,
    а не ошибка клиента (иначе на каждую сессию в логе был бы error)."""
    client = await make_client(fake_worker.port, missing_socket_path, timeout=10)
    lost: list[str] = []
    client.add_disconnect_listener(lost.append)
    # Клиент уже подключён, а сервер мог ещё не вызвать connection_made (на macOS
    # это заметно): ждём, пока у протокола появится transport
    async with asyncio.timeout(5):
        while not fake_worker.protocols or fake_worker.protocols[0].transport is None:
            await asyncio.sleep(0.01)
    (protocol,) = fake_worker.protocols
    sock = protocol.transport.get_extra_info("socket")
    linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)  # close() шлёт RST

    with capture_logs() as logs:
        protocol.transport.abort()
        async with asyncio.timeout(5):
            while not lost:
                await asyncio.sleep(0.02)
    assert not client.connected
    assert "reset" in lost[0] or "closed" in lost[0], lost
    assert [e for e in logs if e["log_level"] == "error"] == []


async def test_manual_disconnect_is_quiet(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path, timeout=10)
    lost: list[str] = []
    client.add_disconnect_listener(lost.append)
    pending = asyncio.create_task(client.request("test_sleep", {"seconds": 5}))
    await asyncio.sleep(0.05)

    await client.disconnect()

    with pytest.raises(WorkerConnectionError):
        await pending
    assert lost == []
    assert not client.connected


async def test_non_map_frame_is_ignored(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerTimeoutError):
            await client.request("test_not_a_map", timeout=0.3)
        assert (await client.request("test_sleep", {"seconds": 0, "echo": "ok"}))["stdout"] == "ok"
    finally:
        await client.disconnect()


async def test_falls_back_to_tcp(fake_worker, missing_socket_path):
    client = WorkerClient(
        pipe_name=unique_pipe_name(),
        socket_path=missing_socket_path,
        tcp_port=fake_worker.port,
    )
    await client.connect(timeout=5.0)
    try:
        assert client.transport == "tcp"
    finally:
        await client.disconnect()


async def test_prefer_is_reread_before_each_attempt(fake_worker, missing_socket_path):
    """Воркер сначала объявляет один транспорт, а потом уходит на другой."""
    first = "pipe" if sys.platform == "win32" else "unix"  # этого транспорта нет
    announced = iter([first, first, "tcp"])
    last = {"value": first}

    def prefer() -> str:
        last["value"] = next(announced, "tcp")
        return last["value"]

    client = WorkerClient(
        pipe_name=unique_pipe_name(), socket_path=missing_socket_path, tcp_port=fake_worker.port
    )
    try:
        await client.connect(timeout=5.0, retry_delay=0.05, prefer=prefer)
        assert client.transport == "tcp"
        assert last["value"] == "tcp"
    finally:
        await client.disconnect()


async def test_connect_gives_up_with_clear_error(missing_socket_path):
    client = WorkerClient(
        pipe_name=unique_pipe_name(),
        socket_path=missing_socket_path,
        tcp_port=get_free_port(),
    )
    with pytest.raises(WorkerConnectionError, match="Cannot connect to Rust worker"):
        await client.connect(timeout=1.0)
    assert not client.connected


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets: Linux/macOS")
async def test_unix_socket_transport(missing_socket_path):
    state = FakeWorkerState()
    path = missing_socket_path  # короткий путь: на macOS tmp_path длиннее лимита
    loop = asyncio.get_running_loop()
    server = await loop.create_unix_server(lambda: FakeWorkerProtocol(state), path)
    client = WorkerClient(socket_path=path, tcp_port=get_free_port())
    try:
        await client.connect(timeout=2.0)
        assert client.transport == "unix"
        result = await client.request("test_sleep", {"seconds": 0, "echo": "unix"})
        assert result["stdout"] == "unix"
    finally:
        await client.disconnect()
        state.close_all()
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(sys.platform != "win32", reason="Named pipes: Windows only")
async def test_named_pipe_transport_does_not_deadlock():
    """Регрессия: старый клиент на open() зависал, когда запрос шёл при висящем read()."""
    loop = asyncio.get_running_loop()
    if not hasattr(loop, "start_serving_pipe"):
        pytest.skip("needs ProactorEventLoop")

    name = unique_pipe_name()
    state = FakeWorkerState()
    servers = await loop.start_serving_pipe(lambda: FakeWorkerProtocol(state), name)
    client = WorkerClient(pipe_name=name, tcp_port=get_free_port(), request_timeout=5)
    try:
        await client.connect(timeout=3.0)
        assert client.transport == "pipe"

        # Читатель уже ждёт данные, и в этот момент уходит запрос
        await asyncio.sleep(0.2)
        results = await asyncio.gather(
            client.request("test_sleep", {"seconds": 0.2, "echo": "a"}),
            client.request("test_sleep", {"seconds": 0.0, "echo": "b"}),
        )
        assert [r["stdout"] for r in results] == ["a", "b"]

        big = await client.request("test_big", {"size": 2_000_000})
        assert len(big["stdout"]) == 2_000_000
    finally:
        await client.disconnect()
        state.close_all()
        for server in servers:
            server.close()
