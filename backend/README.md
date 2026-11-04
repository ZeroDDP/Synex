# Synex backend

FastAPI-сервер между UI и Rust-ядром `synex_worker` (протокол описан в CoreDocs).
Сейчас умеет три вещи: работать с SSH-сессиями через воркер (несколько одновременно,
у каждой свой терминал по WebSocket), хранить SSH-ключи в зашифрованном виде и отдавать
UI поток данных по WebSocket (пока тестовые метрики).

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
| GET | `/api/ssh/sessions` | Открытые SSH-сессии |
| POST | `/api/ssh/sessions` | Открыть сессию: `{host, port, username, key_id \| key_path [+ passphrase] \| password}` → 201 |
| GET | `/api/ssh/sessions/{id}` | Одна сессия: хост, `cwd`, занята ли, сколько вкладок смотрят |
| DELETE | `/api/ssh/sessions/{id}` | Закрыть сессию |
| POST | `/api/ssh/sessions/{id}/exec` | Выполнить команду: `{command, timeout?, track_cwd?}` |
| GET | `/api/ssh/sessions/{id}/output` | Буфер вывода: `?since=N&limit=M` |
| WS | `/api/ssh/sessions/{id}/ws` | Терминал сессии: `?since=N` (см. ниже) |
| POST | `/api/ssh/connect` | То же, что `POST /sessions`, ответ короче |
| POST | `/api/ssh/exec` | Команда: `{command, session_id?, timeout?, track_cwd?}`, без `session_id` в последней открытой сессии |
| POST | `/api/ssh/disconnect` | Закрыть сессию: `{session_id?}` |
| GET | `/api/ssh/status` | Состояние сессии: `?session_id=` или последняя открытая |
| GET | `/api/keys` | Список ключей (без секретной части) |
| POST | `/api/keys` | Импорт: `{name, private_key \| private_key_path, passphrase?, comment?}` → 201 |
| POST | `/api/keys/generate` | Новый ключ: `{name, type: ed25519 \| rsa, bits?, comment?}` → 201 |
| GET | `/api/keys/{id}` | Один ключ |
| PATCH | `/api/keys/{id}` | Переименовать или сменить комментарий: `{name?, comment?}` |
| DELETE | `/api/keys/{id}` | Удалить → 204 |
| GET | `/api/health` | Состояние бэкенда и воркера |
| WS | `/ws` | Поток данных для UI: метрики каждые 500 мс (см. ниже) |

С `DEBUG=true` Swagger доступен на `/api/docs`.

### Пример: ключ из хранилища → подключение → команда → отключение

```powershell
$api = "http://127.0.0.1:8000/api"
$key = Invoke-RestMethod -Method Post "$api/keys" -ContentType "application/json" `
  -Body '{"name": "prod", "private_key_path": "C:/Users/me/.ssh/id_ed25519"}'
$s = Invoke-RestMethod -Method Post "$api/ssh/sessions" -ContentType "application/json" `
  -Body (@{host = "1.2.3.4"; username = "root"; key_id = $key.id} | ConvertTo-Json)
Invoke-RestMethod -Method Post "$api/ssh/sessions/$($s.session_id)/exec" `
  -ContentType "application/json" -Body '{"command": "cd /var/log && ls", "timeout": 10}'
Invoke-RestMethod "$api/ssh/sessions/$($s.session_id)/output"
Invoke-RestMethod -Method Delete "$api/ssh/sessions/$($s.session_id)"
```

### SSH-сессии

- Каждый `POST /api/ssh/sessions` (или `/api/ssh/connect`) открывает новую сессию и
  возвращает `session_id`. Уже открытые сессии продолжают работать. У каждой сессии
  своя `cwd`. Одновременно можно держать `SSH_MAX_SESSIONS` (20) сессий, дальше 429.
- У каждой сессии своё IPC-подключение к воркеру: воркер выполняет запросы одного
  подключения по очереди, а разных параллельно. Команды одной сессии идут по очереди,
  разных сессий параллельно. Если воркер не принял отдельное подключение, сессии
  работают через общее (`channel: "shared"` в описании сессии) и команды всех сессий
  идут по одной.
- Способ входа ровно один: `key_id` (ключ из хранилища), `key_path` (файл ключа на этой
  машине; если ключ зашифрован, добавьте `passphrase`) или `password`. Пароль и фраза
  никуда не сохраняются и не попадают в логи.
- `exec`, `disconnect` и `status` без `session_id` работают с последней открытой
  сессией. Так клиент, который знает только про одну сессию, работает как раньше.
- Сессия живёт внутри воркера и привязана к своему IPC-подключению. Если воркер упал
  или подключение оборвалось, сессия закрывается: вкладки получают `closed`, `exec`
  вернёт 404/409, нужно подключиться заново.
- Закрытие сессии (`DELETE`, `close` из WebSocket) прерывает и команду, которая в ней
  ещё выполняется: её запрос вернёт 409 `ssh_not_connected`.
- Всё, что происходит в сессии, пишется в её буфер вывода: `opened`, `command`
  (команда и `cwd`, откуда пришла: `api` или `ws`), `result` (`stdout`, `stderr`,
  `exit_code`, `cwd`, `timed_out`, `duration_ms`) и `error`. У записи номер `seq`, он
  растёт на 1. Буфер ограничен `SSH_OUTPUT_BUFFER_BYTES` (1 МиБ на сессию): старое
  вытесняется, у слишком большого вывода остаётся конец и `truncated: true`.
- О каждом открытии и закрытии в шину событий уходит `ssh.session`
  (`{action: opened | closed, session_id, host, port, username, reason?}`).

### WebSocket терминала

`ws://127.0.0.1:8000/api/ssh/sessions/{id}/ws?since=N`. Одно подключение смотрит одну
сессию, одну сессию могут смотреть несколько вкладок. Конверт тот же, что у `/ws`:
`{"type", "ts", "data"}`.

| type | Кто и когда шлёт | data |
|---|---|---|
| `hello` | сервер, сразу после подключения | `{protocol, session, replay: {since, count, gap, first_seq, last_seq}}` |
| `output` | сервер: сначала записи буфера после `since`, потом новые | запись буфера `{seq, ts, kind, ...}` |
| `closed` | сервер, когда сессию закрыли; затем закрывает подключение | `{reason}`: `closed`, `worker_connection_lost`, `lost_in_worker`, `shutdown` |
| `error` | сервер, если сообщение не разобрано или команда не запустилась | `{error, detail, request_id?}` |
| `pong` | сервер, в ответ на `ping` | `data` из `ping` |
| `exec` | UI | `{command, timeout?, track_cwd?, id?}`: `id` вернётся в записях как `request_id` |
| `close` | UI | закрыть сессию для всех вкладок |
| `ping` | UI | что угодно |

- Команду из одной вкладки и её результат видят все вкладки этой сессии, команды через
  REST тоже.
- Закрытие вкладки не закрывает сессию: команда продолжает выполняться, результат ждёт
  в буфере. При переподключении передайте `since` = последний полученный `seq` и
  получите только пропущенное; `gap: true` значит, что часть уже вытеснена.
- Вывод терминала не выбрасывается молча. Если UI не успевает читать, подключение
  закрывается кодом 1013: переподключитесь с `since`. Другие коды закрытия: 1000
  (сессию закрыли), 1001 (backend остановлен), 4404 (сессии нет).

```js
const ws = new WebSocket(`ws://127.0.0.1:8000/api/ssh/sessions/${id}/ws`);
ws.onmessage = (e) => console.log(JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({type: "exec", data: {command: "uptime", id: "1"}}));
```

### /api/ssh/exec

Ответ: `{session_id, seq, stdout, stderr, exit_code, cwd, timed_out, duration_ms}`
(`seq`: номер записи с результатом в буфере вывода). То же для `/sessions/{id}/exec`.

- `timeout`: лимит выполнения в секундах (по умолчанию `SSH_EXEC_TIMEOUT=30`, максимум 3600).
  Применяется **на сервере** через coreutils `timeout`. Команда и все её дочерние процессы
  получают TERM, через 3 секунды KILL. Ответ приходит с кодом 200, `timed_out: true`,
  `exit_code` 124 (TERM) или 137 (KILL) и выводом, который успел накопиться. Это важно:
  воркер выполняет запросы по одному, и `tail -f` без лимита занял бы его навсегда.
- Команды одной сессии выполняются по одной. Следующая ждёт окончания предыдущей,
  и её таймаут начинает идти, только когда она реально отправлена воркеру.
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
  PEM. Ключ с парольной фразой расшифровывается при импорте (фраза не сохраняется):
  в хранилище ключ уже защищён Fernet, и при подключении по `key_id` фраза не нужна.
- Приватный ключ API не отдаёт никогда. Для `ssh_connect` воркеру нужен путь к файлу,
  поэтому при `key_id` ключ на время подключения расшифровывается во временный файл
  (`runtime/` рядом с `keys.json`, права 0600) и сразу удаляется.

### WebSocket /ws

Поток данных для UI: `ws://127.0.0.1:8000/ws` (без префикса `/api`). В обе стороны
ходит JSON в текстовых фреймах, каждое сообщение в конверте `{"type", "ts", "data"}`,
`ts` в миллисекундах Unix.

| type | Кто и когда шлёт | data |
|---|---|---|
| `hello` | сервер, сразу после подключения | `{app, version, protocol, streams}` |
| `metrics` | сервер, каждые 500 мс | метрики, пример ниже |
| `ping` | UI | что угодно, вернётся в `pong` |
| `pong` | сервер, в ответ на `ping` | `data` из `ping` |
| `error` | сервер, если не разобрал сообщение UI | `{error, detail}`: `bad_message` или `unknown_type` |

```json
{"type": "metrics", "ts": 1792816849954, "data": {
  "source": "fake",
  "cpu": {"percent": 24.3, "per_core": [17.2, 6.3, 27.3, 43.7, 18.8, 37.0, 18.4, 25.6]},
  "memory": {"total": 34359738368, "used": 15555821878, "percent": 45.3},
  "gpus": [{"name": "Synex Fake GPU", "percent": 33.8, "memory_total": 12884901888,
            "memory_used": 4225312135, "temperature": 50.9}],
  "disks": [{"mount": "/", "total": 549755813888, "used": 225457033871, "percent": 41.0,
             "read_bytes_per_sec": 19046477, "write_bytes_per_sec": 6855608}]
}}
```

- Размеры в байтах, скорости в байтах в секунду, проценты от 0 до 100, температура в °C.
  `gpus` и `disks` всегда списки: GPU может не быть, а дисков бывает несколько.
- Сейчас метрики тестовые (`source: "fake"`): 8 ядер, 32 ГиБ RAM, одна GPU и два диска
  (`/` и `/data`), значения меняются плавно. Генератор выключается `WS_FAKE_METRICS=false`,
  период задаёт `WS_FAKE_METRICS_INTERVAL` в секундах. Настоящий мониторинг будет
  публиковать тот же формат в событие `metrics` шины, и `/ws` перешлёт его без изменений.
- У каждого клиента своя очередь на 64 сообщения. Если UI не успевает читать, теряются
  самые старые сообщения, а память не растёт.
- При остановке backend закрывает соединение с кодом 1012: UI стоит переподключиться.

Быстрая проверка: открыть http://127.0.0.1:8000/api/health и в консоли браузера выполнить

```js
const ws = new WebSocket("ws://127.0.0.1:8000/ws");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

### Ошибки

Все ошибки приходят как `{"error": "<код>", "detail": "<текст>"}`, ошибки валидации как 422:

| HTTP | error | Когда |
|---|---|---|
| 404 | `key_not_found` | Нет ключа с таким id |
| 404 | `ssh_session_not_found` | Нет сессии с таким `session_id` |
| 409 | `ssh_not_connected` | Нет открытой сессии, или воркер её уже не знает |
| 409 | `key_exists` | Имя занято или этот ключ уже сохранён |
| 429 | `ssh_too_many_sessions` | Открыто `SSH_MAX_SESSIONS` сессий |
| 422 | `invalid_key` / — | Ключ не читается, нужна парольная фраза, неверные поля |
| 500 | `keystore_corrupted` | `keys.json` повреждён или зашифрован другим мастер-ключом |
| 502 | `ssh_connect_failed` | Воркер не смог подключиться (хост, ключ, пароль, фраза) |
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
  ws/                  WebSocket /ws: hub.py (протокол, рассылка), metrics.py (метрики)
  plugins/             менеджер плагинов
    ssh/               /api/ssh/*: manager (менеджер сессий), session (сессия и её
                       подключение к воркеру), buffer (буфер вывода), ws (терминал),
                       wrapper (cwd, таймаут), routes, schemas, errors
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

`tests/test_real_worker.py` запускает настоящий `synex_worker.exe` (Windows; на Linux
можно указать команду запуска exe через wine в `SYNEX_TEST_WORKER`). Проверки с
реальным SSH-сервером включаются переменными окружения:

```powershell
$env:SYNEX_TEST_SSH_HOST="1.2.3.4"; $env:SYNEX_TEST_SSH_USER="root"
$env:SYNEX_TEST_SSH_KEY="C:/Users/me/.ssh/id_ed25519"
$env:SYNEX_TEST_SSH_PASSWORD="..."   # необязательно: проверить вход по паролю
uv run pytest tests/test_real_worker.py -v
```

`test_real_worker_raw_exit_codes` помечен `xfail`: воркер возвращает `exit_code=0` для
любой команды. Это баг на стороне Rust. Пока он не исправлен, `/api/ssh/exec` берёт
настоящий код возврата из shell (при `track_cwd=true`). Когда баг исправят, тест
покажет `XPASS`.

`tests/fake_worker.py` повторяет поведение воркера сборки 2026-11-03: сессия = `id`
запроса, сессии принадлежат подключению, запросы подключения по очереди, `exit_code`
всегда 0. `tests/ssh_helpers.py` собирает плагин ssh поверх него и запускает uvicorn
для тестов WebSocket.
