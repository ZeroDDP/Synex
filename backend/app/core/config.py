from __future__ import annotations

import os
import posixpath
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # .../backend


class Settings(BaseSettings):
    """Настройки backend. Читаются из переменных окружения и backend/.env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        # utf-8-sig: .env, сохранённый с BOM (PowerShell 5, старый Блокнот), тоже читается
        env_file_encoding="utf-8-sig",
        # Пустое значение (DEBUG=) значит «по умолчанию», а не ошибку валидации
        env_ignore_empty=True,
        extra="ignore",
    )

    # HTTP API
    APP_NAME: str = "Synex"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    # Веб-страницы (Origin), которым можно обращаться к API и WebSocket, через запятую.
    # Сам backend разрешён всегда, запросы без Origin (curl, PowerShell, приложение) тоже.
    # См. app/core/security.py.
    ALLOWED_ORIGINS: str = ""
    # Dev-сервер UI (frontend/, `npm run dev`): http://localhost:5173 и
    # http://127.0.0.1:5173 разрешены без настройки. false: только ALLOWED_ORIGINS.
    UI_DEV_SERVER: bool = True
    # Дополнительные имена в заголовке Host (127.0.0.1, localhost и HOST разрешены).
    ALLOWED_HOSTS: str = ""

    # Rust-воркер (имена переменных совпадают с CoreDocs, раздел 3)
    # Unix socket воркера на Linux/macOS. Пусто: личная папка пользователя
    # ($XDG_RUNTIME_DIR/synex, /tmp/synex-<uid> или $TMPDIR/synex на macOS), см.
    # app/worker/unix_socket.py и свойство ipc_socket_path.
    RUST_IPC_SOCKET_PATH: str = ""
    RUST_IPC_PIPE_NAME: str = r"\\.\pipe\synex_worker"
    RUST_IPC_TCP_FALLBACK_PORT: int = 9100
    RUST_EXECUTABLE_PATH_UNIX: str = "./app/core/target/release/synex_worker"
    RUST_EXECUTABLE_PATH_WIN32: str = "./app/core/target/release/synex_worker.exe"
    # Таймаут ответа воркера по умолчанию, сек. Для ssh_exec можно передать свой.
    RUST_IPC_REQUEST_TIMEOUT: float = 30.0
    # Сколько ждать, пока воркер поднимет pipe/socket при старте, сек.
    RUST_IPC_CONNECT_TIMEOUT: float = 10.0
    # Перезапускать воркер, если он упал.
    RUST_WORKER_AUTORESTART: bool = True

    # SSH: сколько ждать подключения к серверу (сеть, рукопожатие, вход), сек.
    SSH_CONNECT_TIMEOUT: float = Field(default=20.0, gt=0)
    # SSH: лимит выполнения команды по умолчанию, сек (можно передать свой в запросе).
    SSH_EXEC_TIMEOUT: float = 30.0
    # Сколько ещё ждать ответа воркера сверх лимита команды, сек.
    SSH_EXEC_TIMEOUT_GRACE: float = 10.0
    # Сколько SSH-сессий можно держать открытыми одновременно.
    SSH_MAX_SESSIONS: int = Field(default=20, ge=1)
    # Буфер вывода одной сессии (команды и их результаты), байт. Старое вытесняется.
    SSH_OUTPUT_BUFFER_BYTES: int = Field(default=1024 * 1024, ge=1024)
    # Сколько служебная команда плагина (docker, git) ждёт, пока сессия освободится от
    # команды пользователя, сек. Дольше: 409 ssh_session_busy.
    SSH_SERVICE_WAIT: float = Field(default=10.0, ge=0)
    # Лимит команды из терминала UI (WebSocket), если UI не передал свой, сек. Больше
    # SSH_EXEC_TIMEOUT: apt upgrade или сборка идут минутами, а остановить команду
    # теперь можно Ctrl+C.
    TERMINAL_TIMEOUT: float = Field(default=600.0, gt=0, le=3600)

    # Хранилище SSH-ключей. По умолчанию keys.json в папке данных пользователя:
    # Windows %LOCALAPPDATA%\Synex, macOS ~/Library/Application Support/Synex,
    # Linux $XDG_DATA_HOME/synex (~/.local/share/synex).
    KEYSTORE_PATH: Path | None = None
    # Мастер-ключ Fernet. Если не задан, хранится в системном хранилище учётных
    # данных (Windows Credential Manager, macOS Keychain, Secret Service).
    # Задавайте явно только там, где системного хранилища нет (CI, headless Linux).
    KEYSTORE_MASTER_KEY: SecretStr | None = None

    # Сохранённые подключения к серверам (плагин connections). По умолчанию
    # connections.json рядом с keys.json; пароли в нём зашифрованы тем же мастер-ключом.
    CONNECTIONS_PATH: Path | None = None
    # Доверенные ключи SSH-серверов (отпечатки по host:port, схема TOFU). По умолчанию
    # known_hosts.json рядом с keys.json.
    KNOWN_HOSTS_PATH: Path | None = None
    # Доверять серверам, ключи которых уже записаны в known_hosts OpenSSH на этом
    # компьютере (пользователь подключался к ним через ssh): их ключ не нужно подтверждать
    # ещё раз. false: каждый новый сервер подтверждается в Synex.
    OPENSSH_KNOWN_HOSTS: bool = True
    # Свои файлы known_hosts через запятую вместо стандартных (~/.ssh/known_hosts,
    # ~/.ssh/known_hosts2, /etc/ssh/ssh_known_hosts; на Windows
    # %USERPROFILE%\.ssh\known_hosts и %PROGRAMDATA%\ssh\ssh_known_hosts).
    OPENSSH_KNOWN_HOSTS_FILES: str = ""

    # Плагины через запятую; * значит все установленные (app/plugins/<name>/plugin.py).
    # Нужные плагинам зависимости включаются сами.
    PLUGINS_ENABLED: str = "*"
    # Помнить, что пользователь включил и выключил на ходу (POST /api/plugins/{name}/enable
    # и /disable): после перезапуска его выбор накладывается на PLUGINS_ENABLED. false:
    # после перезапуска включено ровно то, что в PLUGINS_ENABLED.
    PLUGINS_REMEMBER: bool = True
    # Где хранится этот выбор. По умолчанию plugins.json рядом с keys.json.
    PLUGINS_STATE_PATH: Path | None = None

    # Мониторинг серверов (плагин monitor). Как часто опрашивать сервер и как часто
    # отправлять метрики в /ws, сек.
    MONITOR_POLL_INTERVAL: float = Field(default=0.5, gt=0)
    MONITOR_PUBLISH_INTERVAL: float = Field(default=0.5, gt=0)
    # Окно, за которое считаются avg/min/max, сек.
    MONITOR_WINDOW: float = Field(default=30.0, gt=0)
    # Через сколько секунд без свежего замера метрики сервера считаются устаревшими.
    MONITOR_STALE_AFTER: float = Field(default=3.0, gt=0)
    # Сколько ждать ответа воркера на один опрос, сек.
    MONITOR_POLL_TIMEOUT: float = Field(default=5.0, gt=0)
    # Серверы опрашиваются, пока к /ws подключён UI или столько секунд после
    # последнего GET /api/monitor/metrics.
    MONITOR_DEMAND_TIMEOUT: float = Field(default=10.0, gt=0)
    # Как часто обновлять диски и видеокарты сервера (df, nvidia-smi), сек.
    MONITOR_HARDWARE_INTERVAL: float = Field(default=10.0, gt=0)

    # Алерты всех серверов (плагин alerts). Правила (пороги): alerts.json рядом с
    # keys.json. Сколько погасших алертов помнить. Опрашивать серверы и тогда, когда
    # метрики никто не смотрит: иначе алерты при закрытом Dashboard не загораются.
    ALERTS_PATH: Path | None = None
    ALERTS_HISTORY: int = Field(default=200, ge=1)
    ALERTS_KEEP_POLLING: bool = True

    # Логи серверов (плагин logs): журнал systemd и файлы логов. Лимит команды
    # journalctl или чтения файла на сервере, сек. Сколько строк отдаётся за один раз
    # (больше не попросить). Как часто живой лог спрашивает новые строки, сек. Сколько
    # байт файла читается за раз (последние строки файла ищутся в этом окне). Живой лог в
    # своей служебной SSH-сессии: команда в терминале его не задерживает.
    LOGS_TIMEOUT: float = Field(default=20.0, gt=0)
    LOGS_MAX_LINES: int = Field(default=5000, ge=1)
    LOGS_INTERVAL: float = Field(default=2.0, gt=0)
    LOGS_CHUNK_BYTES: int = Field(default=1024 * 1024, ge=4096)
    LOGS_COMPANION: bool = True

    # Docker на серверах (плагин docker). Команда docker на сервере: например
    # "sudo -n docker", если пользователь SSH не в группе docker (нужен sudo без пароля).
    DOCKER_COMMAND: str = Field(default="docker", min_length=1)
    # Сколько ждать команду docker на сервере, сек (stop и restart ждут контейнер).
    DOCKER_TIMEOUT: float = Field(default=60.0, gt=0)
    # Живой лог контейнера (WebSocket): как часто спрашивать у docker новые строки, сек.
    DOCKER_LOGS_INTERVAL: float = Field(default=1.0, gt=0)

    # Git на серверах (плагин git): сколько ждать команду git, сек (fetch и pull ходят
    # на remote).
    GIT_TIMEOUT: float = Field(default=120.0, gt=0)

    # Файлы на серверах (плагин sftp, SFTP Rust-воркера). Лимит одной операции и долгой
    # (копирование папки, удаление дерева, контрольная сумма), сек.
    SFTP_TIMEOUT: float = Field(default=30.0, gt=0)
    SFTP_LONG_TIMEOUT: float = Field(default=600.0, gt=0)
    # Лимит передачи одного файла, сек.
    SFTP_TRANSFER_TIMEOUT: float = Field(default=3600.0, gt=0)
    # Сколько передача в основной сессии (если служебную открыть нельзя) ждёт, пока
    # сессию освободит команда пользователя, сек.
    SFTP_SESSION_WAIT: float = Field(default=60.0, ge=0)
    # Сверять SHA-256 после передачи (размер сверяется всегда). Как часто слать прогресс
    # передачи в /ws, сек. Сколько завершённых передач помнить.
    SFTP_VERIFY: bool = True
    SFTP_PROGRESS_INTERVAL: float = Field(default=0.25, gt=0)
    SFTP_TRANSFER_HISTORY: int = Field(default=100, ge=1)
    # Сколько файлов и папок может быть в папке, которую передают целиком
    SFTP_FOLDER_LIMIT: int = Field(default=100_000, ge=1)
    # Поиск файлов (на сервере и на этом компьютере): сколько ищет, сек; больше скольких
    # найденных не отдаёт; файлы больше SFTP_SEARCH_MAX_FILE байт внутри не просматриваются.
    SFTP_SEARCH_TIMEOUT: float = Field(default=30.0, gt=0)
    SFTP_SEARCH_LIMIT: int = Field(default=1000, ge=1)
    SFTP_SEARCH_MAX_FILE: int = Field(default=10 * 1024 * 1024, ge=1)

    # WebSocket /ws (поток данных для UI). Тестовые метрики вместо настоящих: для
    # разработки UI без SSH-сервера; период публикации, сек.
    WS_FAKE_METRICS: bool = False
    WS_FAKE_METRICS_INTERVAL: float = Field(default=0.5, gt=0)

    @property
    def BASE_DIR(self) -> Path:  # noqa: N802
        return BASE_DIR

    @property
    def enabled_plugins(self) -> list[str]:
        return [p.strip() for p in self.PLUGINS_ENABLED.split(",") if p.strip()]

    @property
    def keystore_path(self) -> Path:
        if self.KEYSTORE_PATH is None:
            return user_data_dir() / "keys.json"
        if self.KEYSTORE_PATH.is_absolute():
            return self.KEYSTORE_PATH
        return BASE_DIR / self.KEYSTORE_PATH

    @property
    def connections_path(self) -> Path:
        if self.CONNECTIONS_PATH is None:
            return user_data_dir() / "connections.json"
        if self.CONNECTIONS_PATH.is_absolute():
            return self.CONNECTIONS_PATH
        return BASE_DIR / self.CONNECTIONS_PATH

    @property
    def known_hosts_path(self) -> Path:
        if self.KNOWN_HOSTS_PATH is None:
            return user_data_dir() / "known_hosts.json"
        if self.KNOWN_HOSTS_PATH.is_absolute():
            return self.KNOWN_HOSTS_PATH
        return BASE_DIR / self.KNOWN_HOSTS_PATH

    @property
    def alerts_path(self) -> Path:
        if self.ALERTS_PATH is None:
            return user_data_dir() / "alerts.json"
        if self.ALERTS_PATH.is_absolute():
            return self.ALERTS_PATH
        return BASE_DIR / self.ALERTS_PATH

    @property
    def plugins_state_path(self) -> Path:
        if self.PLUGINS_STATE_PATH is None:
            return user_data_dir() / "plugins.json"
        if self.PLUGINS_STATE_PATH.is_absolute():
            return self.PLUGINS_STATE_PATH
        return BASE_DIR / self.PLUGINS_STATE_PATH

    @property
    def ipc_socket_path(self) -> str:
        """RUST_IPC_SOCKET_PATH или путь по умолчанию в личной папке пользователя."""
        return self.RUST_IPC_SOCKET_PATH or str(default_socket_path())

    @property
    def rust_executable_path(self) -> str:
        if sys.platform == "win32":
            return self.RUST_EXECUTABLE_PATH_WIN32
        return self.RUST_EXECUTABLE_PATH_UNIX

    @property
    def rust_executable_path_is_default(self) -> bool:
        """Путь к воркеру не меняли: тогда ищется и сборка в core/ (executable.py)."""
        name = (
            "RUST_EXECUTABLE_PATH_WIN32" if sys.platform == "win32" else "RUST_EXECUTABLE_PATH_UNIX"
        )
        return self.rust_executable_path == type(self).model_fields[name].default


def user_data_dir() -> Path:
    """Папка данных приложения в профиле пользователя."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Synex"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Synex"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "synex"


SOCKET_DIR_NAME = "synex"
SOCKET_FILE_NAME = "worker.sock"


def default_socket_path(
    platform: str = sys.platform,
    env: Mapping[str, str] = os.environ,
    uid: int | None = None,
) -> Path:
    """Unix socket воркера в папке, доступной только текущему пользователю.

    Linux: $XDG_RUNTIME_DIR/synex (/run/user/<uid>, создаёт systemd), без него
    /tmp/synex-<uid>. macOS: $TMPDIR/synex ($TMPDIR у каждого пользователя свой).
    """
    if platform == "darwin" and env.get("TMPDIR"):
        return Path(env["TMPDIR"]) / SOCKET_DIR_NAME / SOCKET_FILE_NAME
    runtime = env.get("XDG_RUNTIME_DIR")
    # posixpath: путь Linux проверяется по правилам Linux на любой ОС (на Windows у
    # Path("/run/user/1000") нет диска, и is_absolute() дал бы False)
    if platform != "darwin" and runtime and posixpath.isabs(runtime):
        return Path(runtime) / SOCKET_DIR_NAME / SOCKET_FILE_NAME
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path("/tmp") / f"{SOCKET_DIR_NAME}-{uid}" / SOCKET_FILE_NAME


settings = Settings()
