from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # .../backend


class Settings(BaseSettings):
    """Настройки backend. Читаются из переменных окружения и backend/.env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # HTTP API
    APP_NAME: str = "Synex"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Rust-воркер (имена переменных совпадают с CoreDocs, раздел 3)
    RUST_IPC_SOCKET_PATH: str = "/tmp/synex_worker.sock"
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

    # SSH: лимит выполнения команды по умолчанию, сек (можно передать свой в запросе).
    SSH_EXEC_TIMEOUT: float = 30.0
    # Сколько ещё ждать ответа воркера сверх лимита команды, сек.
    SSH_EXEC_TIMEOUT_GRACE: float = 10.0

    # Хранилище SSH-ключей. По умолчанию keys.json в папке данных пользователя:
    # Windows %LOCALAPPDATA%\Synex, macOS ~/Library/Application Support/Synex,
    # Linux $XDG_DATA_HOME/synex (~/.local/share/synex).
    KEYSTORE_PATH: Path | None = None
    # Мастер-ключ Fernet. Если не задан, хранится в системном хранилище учётных
    # данных (Windows Credential Manager, macOS Keychain, Secret Service).
    # Задавайте явно только там, где системного хранилища нет (CI, headless Linux).
    KEYSTORE_MASTER_KEY: SecretStr | None = None

    # Плагины через запятую.
    PLUGINS_ENABLED: str = "ssh,keys"

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
    def rust_executable_path(self) -> str:
        if sys.platform == "win32":
            return self.RUST_EXECUTABLE_PATH_WIN32
        return self.RUST_EXECUTABLE_PATH_UNIX


def user_data_dir() -> Path:
    """Папка данных приложения в профиле пользователя."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Synex"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Synex"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "synex"


settings = Settings()
