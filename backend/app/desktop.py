"""Запуск Synex как приложения: backend, воркер и UI в одном окне.

    uv run python -m app.desktop            # из папки backend, UI из ../frontend/dist
    Synex.exe / ./Synex / Synex.app          # собранное приложение (packaging/)

Что делает:
1. Находит свои данные: собранный UI и воркер этой ОС. В сборке они лежат рядом с
   программой (PyInstaller кладёт их в папку _internal), при запуске из исходников —
   frontend/dist и воркер из backend/app/core/target.
2. Читает настройки пользователя: synex.env в папке данных (там же keys.json,
   connections.json, история терминала), при первом запуске создаёт его с подсказками.
   Переменные окружения важнее файла.
3. Если Synex уже запущен на этом порту, просто открывает его окно ещё раз.
4. Запускает backend (он запускает воркер) и открывает UI: в своём окне (pywebview:
   WebView2 на Windows, WebKit на macOS), а если окна нет — в браузере. Закрыли окно:
   backend и воркер останавливаются.

Окно без консоли (сборка для Windows и macOS): вывод идёт в logs/synex.log в папке
данных.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Synex"
DEFAULT_PORT = 8000
WINDOW_SIZE = (1360, 860)
MIN_SIZE = (1024, 640)
START_TIMEOUT = 30.0
ENV_FILE = "synex.env"
ENV_TEMPLATE = """\
# Настройки Synex. Каждая описана в backend/.env.example в репозитории
# (https://github.com/ZeroDDP/Synex). Переменные окружения важнее этого файла.
# Примеры:
# DEBUG=true                 # подробный лог и Swagger UI на /api/docs
# SSH_EXEC_TIMEOUT=30
# TERMINAL_TIMEOUT=600
# PLUGINS_ENABLED=*
# WS_FAKE_METRICS=true       # демо-сервер на Dashboard без SSH
"""


@dataclass(frozen=True)
class Layout:
    """Где лежат данные программы."""

    frozen: bool  # собранное приложение (PyInstaller)
    root: Path  # папка с данными сборки или папка backend
    ui: Path | None
    worker: Path | None
    version: str


def layout() -> Layout:
    worker_name = "synex_worker.exe" if sys.platform == "win32" else "synex_worker"
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        ui = root / "ui"
        worker = root / "worker" / worker_name
        version_file = root / "VERSION"
    else:
        root = Path(__file__).resolve().parents[1]  # backend
        ui = root.parent / "frontend" / "dist"
        worker = None  # обычный поиск воркера (RUST_EXECUTABLE_PATH_*)
        version_file = root.parent / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    return Layout(
        frozen=bool(getattr(sys, "frozen", False)),
        root=root,
        ui=ui if (ui / "index.html").is_file() else None,
        worker=worker if worker is not None and worker.is_file() else None,
        version=version or "dev",
    )


def prepare_environment(where: Layout, port: int) -> Path:
    """Переменные окружения для backend до импорта настроек. Возвращает файл настроек."""
    from dotenv import dotenv_values  # noqa: PLC0415

    folder = _user_data_dir_early()
    folder.mkdir(parents=True, exist_ok=True)
    env_file = folder / ENV_FILE
    if not env_file.exists():
        try:
            env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    for key, value in dotenv_values(env_file).items():
        if value is not None:
            os.environ.setdefault(key, value)
    os.environ.setdefault("PORT", str(port))
    os.environ.setdefault("HOST", "127.0.0.1")
    if where.ui is not None:
        os.environ.setdefault("UI_DIR", str(where.ui))
    if where.worker is not None:
        os.environ.setdefault("RUST_EXECUTABLE_PATH_WIN32", str(where.worker))
        os.environ.setdefault("RUST_EXECUTABLE_PATH_UNIX", str(where.worker))
    if where.frozen:
        # В сборке нет dev-сервера UI и нет backend/.env рядом с исходниками
        os.environ.setdefault("UI_DEV_SERVER", "false")
    os.environ.setdefault("SYNEX_VERSION", where.version)
    return env_file


def _user_data_dir_early() -> Path:
    """Папка данных до импорта настроек (то же, что app.core.config.user_data_dir)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Synex"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Synex"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "synex"


def redirect_output(folder: Path) -> None:
    """Окно без консоли: sys.stdout и sys.stderr равны None, а логи пишутся в них.
    Направляем вывод в файл (последние два запуска: synex.log и synex.log.1)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    logs = folder / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "synex.log"
    if log.exists():
        try:
            log.replace(logs / "synex.log.1")
        except OSError:
            pass
    stream = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (до выхода)
    sys.stdout = stream
    sys.stderr = stream


def running_synex(url: str) -> bool:
    """На порту уже отвечает Synex (второй запуск: просто открыть окно)."""
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(data, dict) and data.get("status") == "ok"


def port_busy(host: str, port: int) -> bool:
    import socket  # noqa: PLC0415

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


class Backend:
    """uvicorn в отдельном потоке: главный поток нужен окну."""

    def __init__(self, host: str, port: int) -> None:
        import uvicorn  # noqa: PLC0415

        config = uvicorn.Config(
            "app.core.app:create_app",
            factory=True,
            host=host,
            port=port,
            log_level="info",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="synex-backend", daemon=True)

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 15.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def open_window(url: str, title: str, *, prefer_browser: bool) -> bool:
    """Окно приложения (pywebview). False: окна нет (не установлен pywebview или нет
    графической среды), UI открыт в браузере. Возвращается, когда окно закрыли."""
    if not prefer_browser:
        try:
            import webview  # noqa: PLC0415
        except ImportError:
            webview = None
        if webview is not None:
            try:
                webview.create_window(
                    title, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE
                )
                webview.start()
                return True
            except Exception as e:  # нет WebView2, GTK, дисплея: браузер
                print(f"Synex window is unavailable ({e!r}), opening the browser", flush=True)
    webbrowser.open(url)
    return False


def show_error(title: str, message: str) -> None:
    """Ошибка запуска в окне: у сборки для Windows и macOS нет консоли."""
    try:
        import webview  # noqa: PLC0415
    except ImportError:
        return
    page = (
        "<body style='font-family:sans-serif;background:#0e1117;color:#e6e6e6;padding:32px'>"
        f"<h2 style='color:#ff7b00'>{html.escape(title)} cannot start</h2>"
        f"<p>{html.escape(message)}</p></body>"
    )
    try:
        webview.create_window(title, html=page, width=640, height=260)
        webview.start()
    except Exception:  # нет окна: сообщение уже в журнале
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="synex", description="Synex desktop app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SYNEX_PORT", 0) or 0))
    parser.add_argument("--browser", action="store_true", help="open the UI in the browser")
    parser.add_argument(
        "--headless", action="store_true", help="backend only, no window (for tests)"
    )
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    where = layout()
    if args.version:
        print(f"{APP_NAME} {where.version}")
        return 0
    port = args.port or DEFAULT_PORT
    folder = _user_data_dir_early()
    redirect_output(folder)
    env_file = prepare_environment(where, port)
    host = os.environ["HOST"]
    port = int(os.environ["PORT"])
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    title = f"{APP_NAME} {where.version}"
    print(f"{title}: data {folder}, settings {env_file}, UI {where.ui}", flush=True)

    if running_synex(url):
        print(f"{APP_NAME} is already running at {url}: opening it", flush=True)
        if not args.headless:
            open_window(url, title, prefer_browser=args.browser)
        return 0
    if port_busy("127.0.0.1", port):
        message = f"Port {port} is taken by another program. Close it or set PORT in {env_file}"
        print(message, file=sys.stderr, flush=True)
        if not args.headless:
            show_error(title, message)
        return 2

    backend = Backend(host, port)
    if not backend.start():
        print("The backend did not start: see the log above", file=sys.stderr, flush=True)
        backend.stop()
        if not args.headless:
            show_error(title, f"The backend did not start. Details: {folder / 'logs'}")
        return 1
    try:
        if args.headless:
            while backend.thread.is_alive():
                time.sleep(0.5)
            return 0
        if open_window(url, title, prefer_browser=args.browser):
            return 0  # окно закрыли
        # Браузер: работаем, пока не остановят (Ctrl+C) или backend не завершится
        while backend.thread.is_alive():
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
