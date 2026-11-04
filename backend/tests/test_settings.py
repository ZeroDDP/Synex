"""Настройки из .env: пустые значения, BOM и понятная ошибка при неверном значении."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.core.config import BASE_DIR, Settings
from app.main import describe_settings_error


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DEBUG", "PORT", "KEYSTORE_MASTER_KEY", "KEYSTORE_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_empty_values_mean_defaults(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DEBUG=\nPORT=\nKEYSTORE_MASTER_KEY=\nKEYSTORE_PATH=\n", encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.DEBUG is False
    assert settings.PORT == 8000
    assert settings.KEYSTORE_MASTER_KEY is None
    assert settings.KEYSTORE_PATH is None


def test_env_file_with_bom(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_bytes("DEBUG=true\n".encode("utf-8-sig"))
    assert Settings(_env_file=env).DEBUG is True


def test_error_message_names_the_setting_and_hides_secrets():
    class Example(BaseModel):
        PORT: int
        API_TOKEN: int

    with pytest.raises(ValidationError) as caught:
        Example(PORT="abc", API_TOKEN="s3cr3t")
    text = describe_settings_error(caught.value)
    assert "PORT='abc': Input should be a valid integer" in text
    assert "API_TOKEN=***" in text
    assert "s3cr3t" not in text


def test_invalid_setting_stops_backend_with_readable_message():
    env = {**os.environ, "PORT": "abc"}
    result = subprocess.run(
        [sys.executable, "-m", "app.main"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "PORT='abc'" in result.stderr
    assert "Traceback" not in result.stderr
