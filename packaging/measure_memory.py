"""Замер памяти Synex: backend, воркер и (с окном) WebView.

    uv run --project backend --with psutil python packaging/measure_memory.py \
        [--app packaging/dist/Synex/Synex] [--ssh user@host:port --key ~/.ssh/id_ed25519]

Без --app меряется backend из исходников (python -m app.desktop --headless). Приложение
запускается с пустой папкой данных, сценарии идут по очереди, в каждом через --settle
секунд снимаются RSS и USS (память только этого процесса, без общих библиотек) всех
процессов Synex:

1. idle: приложение запущено, UI не подключён;
2. ui: подключён UI (/ws), идут метрики (WS_FAKE_METRICS: демо-сервер дважды в секунду);
3. ssh (если передан --ssh): открыта SSH-сессия, выполнена команда, открыт живой лог
   журнала и получены метрики сервера;
4. ssh x5: ещё четыре сессии к тому же серверу.

С --window приложение открывает своё окно (pywebview), и в замер попадают процессы
WebView (msedgewebview2 на Windows, WebKit на macOS): окно надо не закрывать до конца.

Результат: таблица в консоли и packaging/dist/memory-<ос>-<арх>.md (+ .json). Цель
SYN-72: до 200 МБ в простое без окна.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "packaging" / "dist"
TARGET_MB = 200
MB = 1024 * 1024


@dataclass
class Sample:
    scenario: str
    processes: list[dict[str, object]] = field(default_factory=list)
    rss_mb: float = 0.0
    uss_mb: float = 0.0

    def add(self, process: psutil.Process, role: str) -> None:
        try:
            info = process.memory_full_info()
            uss = getattr(info, "uss", info.rss)
            entry = {
                "pid": process.pid,
                "role": role,
                "name": process.name(),
                "rss_mb": round(info.rss / MB, 1),
                "uss_mb": round(uss / MB, 1),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        self.processes.append(entry)
        self.rss_mb += entry["rss_mb"]  # type: ignore[operator]
        self.uss_mb += entry["uss_mb"]  # type: ignore[operator]


def role_of(process: psutil.Process) -> str:
    name = process.name().lower()
    if "synex_worker" in name:
        return "worker"
    if "webview" in name or "webkit" in name or "edge" in name:
        return "window"
    return "backend" if "synex" in name or "python" in name else "other"


def snapshot(root: psutil.Process, scenario: str) -> Sample:
    sample = Sample(scenario)
    sample.add(root, "backend")
    for child in root.children(recursive=True):
        sample.add(child, role_of(child))
    sample.rss_mb = round(sample.rss_mb, 1)
    sample.uss_mb = round(sample.uss_mb, 1)
    return sample


def start_app(app: Path | None, port: int, data: str, *, window: bool) -> subprocess.Popen:
    env = {
        **os.environ,
        "HOME": data,
        "USERPROFILE": data,
        "LOCALAPPDATA": data,
        "XDG_DATA_HOME": data,
        "XDG_RUNTIME_DIR": data,
        "KEYSTORE_MASTER_KEY": "Zm9yLW1lbW9yeS10ZXN0LW9ubHktMDEyMzQ1Njc4OWE=",
        "PORT": str(port),
        "WS_FAKE_METRICS": "true",
        "OPENSSH_KNOWN_HOSTS": "false",
    }
    flags = [] if window else ["--headless"]
    if app is not None:
        command = [str(app), *flags]
        cwd = None
    else:
        command = [sys.executable, "-m", "app.desktop", *flags]
        cwd = ROOT / "backend"
    return subprocess.Popen(command, env=env, cwd=cwd)


def wait_ready(port: int, process: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"Synex exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                worker = json.loads(r.read())["worker"]
            if worker.get("connected") and worker.get("worker_pid"):
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("Synex did not start")


def api(port: int, method: str, path: str, body: object | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode()
    return json.loads(text) if text else {}


async def ui_client(port: int, stop: asyncio.Event, paths: list[str]) -> None:
    """Подключения UI: /ws и, если есть, живой лог. Читают всё, что приходит."""
    from websockets.asyncio.client import connect  # noqa: PLC0415

    async def reader(path: str) -> None:
        async with connect(f"ws://127.0.0.1:{port}{path}", max_size=None) as ws:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(ws.recv(), 1)
                except TimeoutError:
                    pass

    tasks = [asyncio.create_task(reader(path)) for path in paths]
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def run_with_clients(port: int, paths: list[str], seconds: float, measure) -> Sample:
    async def main() -> Sample:
        stop = asyncio.Event()
        clients = asyncio.create_task(ui_client(port, stop, paths))
        await asyncio.sleep(seconds)
        sample = measure()
        stop.set()
        await clients
        return sample

    return asyncio.run(main())


def trust_and_connect(port: int, ssh: str, key: str) -> str:
    user, _, address = ssh.partition("@")
    host, _, port_text = address.partition(":")
    body = {"host": host, "port": int(port_text or 22), "username": user, "key_path": key}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ssh/sessions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())["session_id"]
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        if data.get("error") != "ssh_host_key_unknown":
            raise SystemExit(f"SSH connect failed: {data}") from None
        api(port, "POST", "/api/ssh/known-hosts", {
            "host": host, "port": int(port_text or 22),
            "fingerprint": data["host_key"]["fingerprint"],
        })  # fmt: skip
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())["session_id"]


def report(samples: list[Sample], meta: dict[str, object]) -> str:
    lines = [
        f"# Synex memory: {meta['version']} on {meta['os']}",
        "",
        f"{meta['when']}, {meta['what']}, settle {meta['settle']} s. RSS counts shared "
        "libraries in every process, USS only the process's own memory.",
        "",
        "| Scenario | Backend USS | Worker USS | Window USS | Total USS | Total RSS |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for sample in samples:
        by_role: dict[str, float] = {}
        for item in sample.processes:
            role = str(item["role"])
            by_role[role] = by_role.get(role, 0.0) + float(item["uss_mb"])  # type: ignore[arg-type]
        lines.append(
            f"| {sample.scenario} | {by_role.get('backend', 0):.1f} MB | "
            f"{by_role.get('worker', 0):.1f} MB | {by_role.get('window', 0):.1f} MB | "
            f"{sample.uss_mb:.1f} MB | {sample.rss_mb:.1f} MB |"
        )
    idle = samples[0]
    verdict = "meets" if idle.rss_mb <= TARGET_MB else "exceeds"
    lines += [
        "",
        f"Idle total RSS {idle.rss_mb:.1f} MB {verdict} the {TARGET_MB} MB target (SYN-72).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Synex memory")
    parser.add_argument("--app", type=Path, help="built Synex executable")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--settle", type=float, default=20.0)
    parser.add_argument("--window", action="store_true", help="measure with the app window")
    parser.add_argument("--ssh", help="user@host:port for the SSH scenarios")
    parser.add_argument("--key", help="private key file for --ssh")
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text().strip()
    os_name = f"{platform.system()} {platform.release()} {platform.machine()}"
    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix="synex-memory-") as data:
        process = start_app(args.app, args.port, data, window=args.window)
        try:
            wait_ready(args.port, process)
            root = psutil.Process(process.pid)
            time.sleep(args.settle)
            samples.append(snapshot(root, "idle"))
            samples.append(
                run_with_clients(
                    args.port, ["/ws"], args.settle, lambda: snapshot(root, "ui (/ws, metrics)")
                )
            )
            if args.ssh and args.key:
                session = trust_and_connect(args.port, args.ssh, args.key)
                api(args.port, "POST", f"/api/ssh/sessions/{session}/exec", {"command": "uname -a"})
                server = api(args.port, "GET", f"/api/ssh/sessions/{session}")
                server_id = f"{server['host'].lower()}:{server['port']}"
                paths = [
                    "/ws",
                    f"/api/ssh/sessions/{session}/ws",
                    f"/api/logs/servers/{server_id}/journal/ws?lines=200",
                ]
                samples.append(
                    run_with_clients(
                        args.port, paths, args.settle,
                        lambda: snapshot(root, "ssh: 1 session, terminal, live log"),
                    )
                )  # fmt: skip
                for _ in range(4):
                    trust_and_connect(args.port, args.ssh, args.key)
                samples.append(
                    run_with_clients(
                        args.port, paths, args.settle, lambda: snapshot(root, "ssh: 5 sessions")
                    )
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()

    meta = {
        "version": version,
        "os": os_name,
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "what": f"build {args.app.name}" if args.app else "backend from sources",
        "settle": args.settle,
    }
    text = report(samples, meta)
    print(text)
    DIST.mkdir(parents=True, exist_ok=True)
    arch = platform.machine().lower()
    stem = DIST / f"memory-{platform.system().lower()}-{arch}"
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    stem.with_suffix(".json").write_text(
        json.dumps({"meta": meta, "samples": [asdict(s) for s in samples]}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
