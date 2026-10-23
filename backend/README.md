# Synex backend

FastAPI-сервер между UI и Rust-ядром `synex_worker` (протокол описан в CoreDocs).
Сейчас умеет две вещи: работать с SSH-сессией через воркер и хранить SSH-ключи
в зашифрованном виде.

## Запуск

Нужны Python 3.12 и [uv](https://docs.astral.sh/uv/).

```powershell
cd backend
uv sync                      # зависимости, включая dev-группу (pytest, ruff)
uv run python -m app.main    # http://127.0.0.1:8000
```

Бэкенд сам запускает `app/core/target/release/synex_worker.exe` как дочерний процесс,
подключается к нему по Named Pipe (Linux/macOS: Unix socket, запасной вариант TCP)
и перезапускает воркер, если тот упал. Настройки лежат в `.env`, образец: `.env.example`.

Не запускайте uvicorn с `--reload` или `--workers` на Windows: тогда uvicorn
использует SelectorEventLoop, на котором Named Pipe не работает.

## API

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/api/ssh/connect` | Открыть SSH-сессию: `{host, port, username, key_id \| key_path}` → 201 |
| POST | `/api/ssh/exec` | Выполнить команду: `{command, timeout?, track_cwd?}` |
| GET | `/api/ssh/status` | Состояние сессии |
| GET | `/api/keys` | Список ключей (без секретной части) |
| POST | `/api/keys` | Импорт: `{name, private_key \| private_key_path, passphrase?, comment?}` → 201 |
| POST | `/api/keys/generate` | Новый ключ: `{name, type: ed25519 \| rsa, bits?, comment?}` → 201 |
| GET | `/api/keys/{id}` | Один ключ |
| PATCH | `/api/keys/{id}` | Переименовать или сменить комментарий: `{name?, comment?}` |
| DELETE | `/api/keys/{id}` | Удалить → 204 |
| GET | `/api/health` | Состояние бэкенда и воркера |

С `DEBUG=true` Swagger доступен на `/api/docs`.

### Пример: ключ из хранилища → подключение → команда

```powershell
$api = "http://127.0.0.1:8000/api"
$key = Invoke-RestMethod -Method Post "$api/keys" -ContentType "application/json" `
  -Body '{"name": "prod", "private_key_path": "C:/Users/me/.ssh/id_ed25519"}'
Invoke-RestMethod -Method Post "$api/ssh/connect" -ContentType "application/json" `
  -Body (@{host = "1.2.3.4"; username = "root"; key_id = $key.id} | ConvertTo-Json)
Invoke-RestMethod -Method Post "$api/ssh/exec" -ContentType "application/json" `
  -Body '{"command": "cd /var/log && ls", "timeout": 10}'
```

### /api/ssh/exec

Ответ: `{stdout, stderr, exit_code, cwd, timed_out, duration_ms}`.

- `timeout`: лимит выполнения в секундах (по умолчанию `SSH_EXEC_TIMEOUT=30`, максимум 3600).
  Применяется **на сервере** через coreutils `timeout`. Команда и все её дочерние процессы
  получают TERM, через 3 секунды KILL. Ответ приходит с кодом 200, `timed_out: true`,
  `exit_code` 124 (TERM) или 137 (KILL) и выводом, который успел накопиться. Это важно:
  воркер выполняет запросы по одному, и `tail -f` без лимита занял бы его навсегда.
- Команды выполняются по одной. Следующая ждёт окончания предыдущей, и её таймаут
  начинает идти, только когда она реально отправлена воркеру.
- `cwd` сохраняется между командами: после `cd /var/log` следующая команда стартует там.
  Если директорию удалили, команда не выполняется (`exit_code` 1, `cwd` = домашняя папка).
- `track_cwd: false` отправляет команду воркеру как есть: без cwd, без таймаута на
  сервере и с `exit_code` от воркера (сейчас всегда 0, см. ниже). Нужно для
  не-POSIX оболочек вроде fish.
- 504 `ssh_exec_timeout` означает, что воркер не вернул результат даже через
  `timeout + 3 + SSH_EXEC_TIMEOUT_GRACE` секунд. Например, на сервере нет coreutils
  `timeout` (macOS, старый busybox): тогда команда могла продолжить работать.

### Хранилище ключей

- Файл: `%LOCALAPPDATA%\Synex\keys.json` (macOS `~/Library/Application Support/Synex`,
  Linux `~/.local/share/synex`), путь меняется через `KEYSTORE_PATH`.
- Приватные ключи зашифрованы Fernet (AES-128-CBC + HMAC-SHA256). В открытом виде лежат
  только имя, тип, отпечаток и публичный ключ. Запись атомарная; на Linux/macOS права
  файла 0600.
- Мастер-ключ Fernet создаётся при первом сохранении ключа и хранится в Windows
  Credential Manager (`synex` / `keystore-master-key`), а не рядом с файлом. Копия
  `keys.json` без учётной записи пользователя бесполезна. Если удалить запись из
  Credential Manager, сохранённые ключи будет не расшифровать: API вернёт
  `keystore_corrupted`, новый мастер-ключ молча не создастся. Там, где системного
  хранилища нет (CI, headless Linux), мастер-ключ задаётся в `KEYSTORE_MASTER_KEY`.
- Поддерживаются Ed25519, RSA от 2048 бит и ECDSA (P-256/384/521) в форматах OpenSSH и
  PEM. Ключ с парольной фразой расшифровывается при импорте (фраза не сохраняется),
  потому что воркер парольные фразы не принимает.
- Приватный ключ API не отдаёт никогда. Для `ssh_connect` воркеру нужен путь к файлу,
  поэтому при `key_id` ключ на время подключения расшифровывается во временный файл
  (`runtime/` рядом с `keys.json`, права 0600) и сразу удаляется.

### Ошибки

Все ошибки приходят как `{"error": "<код>", "detail": "<текст>"}`, ошибки валидации как 422:

| HTTP | error | Когда |
|---|---|---|
| 404 | `key_not_found` | Нет ключа с таким id |
| 409 | `ssh_not_connected` | `exec` без открытой сессии |
| 409 | `key_exists` | Имя занято или этот ключ уже сохранён |
| 422 | `invalid_key` / — | Ключ не читается, нужна парольная фраза, неверные поля |
| 500 | `keystore_corrupted` | `keys.json` повреждён или зашифрован другим мастер-ключом |
| 502 | `ssh_connect_failed` | Воркер не смог подключиться (хост, ключ, пользователь) |
| 502 | `worker_command_failed` | Воркер вернул ошибку |
| 503 | `worker_unavailable` | Воркер не запущен или IPC недоступен |
| 503 | `keystore_unavailable` | Нет доступа к Credential Manager / неверный `KEYSTORE_MASTER_KEY` |
| 504 | `ssh_exec_timeout` | Воркер не вернул результат команды |
| 504 | `worker_timeout` | Воркер не ответил на другой запрос |

## Структура

```
app/
  main.py              точка входа (uvicorn)
  core/                платформа: config, app factory, ошибки, шина событий, логи
    target/release/    synex_worker.exe
  worker/              Rust-ядро: client.py (IPC-протокол), manager.py (процесс)
  keystore/            хранилище ключей: store.py, ssh_keys.py, master_key.py
  api/                 корневой роутер /api, /api/health, зависимости
  plugins/             менеджер плагинов
    ssh/               /api/ssh/*: routes, schemas, сессия, wrapper (cwd, таймаут)
    keys/              /api/keys/*
tests/                 pytest; fake_worker.py имитирует synex_worker
```

Новый функционал добавляется плагином: `app/plugins/<name>/plugin.py` с подклассом
`BasePlugin` и `router`, затем имя добавляется в `PLUGINS_ENABLED`.

## Тесты

```powershell
uv run pytest
```

Тесты используют временное хранилище и свой мастер-ключ: настоящий `keys.json` и
Credential Manager они не трогают.

`tests/test_real_worker.py` запускает настоящий `synex_worker.exe` (только Windows).
Проверки с реальным SSH-сервером включаются переменными окружения:

```powershell
$env:SYNEX_TEST_SSH_HOST="1.2.3.4"; $env:SYNEX_TEST_SSH_USER="root"
$env:SYNEX_TEST_SSH_KEY="C:/Users/me/.ssh/id_ed25519"
uv run pytest tests/test_real_worker.py -v
```

`test_real_worker_raw_exit_codes` падает, пока воркер возвращает `exit_code=0` для
любой команды. Это баг на стороне Rust. Пока он не исправлен, `/api/ssh/exec` берёт
настоящий код возврата из shell (при `track_cwd=true`).
