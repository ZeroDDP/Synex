from __future__ import annotations

import asyncio
import sys
import time

import pytest

from app.core.errors import WorkerCommandError, WorkerConnectionError, WorkerTimeoutError
from app.worker.client import WorkerClient
from tests.conftest import get_free_port, unique_pipe_name
from tests.fake_worker import FakeWorkerProtocol, FakeWorkerState

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


async def test_protocol_matches_core_docs(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerCommandError) as excinfo:
            await client.request("ssh_exec", {"command": "ls"})
        assert excinfo.value.stderr == "Not connected. Please send 'ssh_connect' first."
        assert excinfo.value.exit_code == 1

        result = await client.request("ssh_connect", CONNECTION)
        assert result["stdout"] == "Connected successfully"

        result = await client.request("ssh_exec", {"command": "ls -la /tmp"})
        assert result == {"stdout": "ran: ls -la /tmp", "stderr": "", "exit_code": 0}
    finally:
        await client.disconnect()

    actions = [r["action"] for r in fake_worker.requests]
    assert actions == ["ssh_exec", "ssh_connect", "ssh_exec"]
    assert all(isinstance(r["id"], str) and r["id"] for r in fake_worker.requests)
    assert fake_worker.requests[1]["payload"] == CONNECTION


async def test_unknown_action_is_worker_error(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        with pytest.raises(WorkerCommandError, match="Unknown action: sftp_list_dir"):
            await client.request("sftp_list_dir", {"path": "/"})
        assert client.connected
    finally:
        await client.disconnect()


async def test_concurrent_requests_complete_out_of_order(fake_worker, missing_socket_path):
    client = await make_client(fake_worker.port, missing_socket_path)
    try:
        results = await asyncio.gather(
            client.request("test_sleep", {"seconds": 0.3, "echo": "slow"}),
            client.request("test_sleep", {"seconds": 0.01, "echo": "fast"}),
            *[client.request("test_sleep", {"seconds": 0.05, "echo": str(i)}) for i in range(20)],
        )
        assert [r["stdout"] for r in results] == ["slow", "fast", *[str(i) for i in range(20)]]
    finally:
        await client.disconnect()


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
    with pytest.raises(WorkerConnectionError) as excinfo:
        await client.request("test_close")
    assert not isinstance(excinfo.value, WorkerTimeoutError)
    with pytest.raises(WorkerConnectionError):
        await slow
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
async def test_unix_socket_transport(tmp_path):
    state = FakeWorkerState()
    path = str(tmp_path / "w.sock")
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
