# Synex Worker — Документация по использованию

> English version: [README.md](README.md)

## 1. Общее описание

**Synex Worker** (`synex_worker.exe` / `synex_worker`) — Rust-бинарник, SSH/SFTP-ядро приложения Synex. Он запускается как дочерний процесс Python-бэкенда и общается с ним через IPC (межпроцессное взаимодействие).

**Ключевые особенности:**

- Бинарник **ничего не делает** при старте, кроме открытия IPC-канала.
- SSH-подключение происходит **только после команды `ssh_connect`** от Python.
- Одна SSH-сессия переиспользуется для всех команд (без повторной авторизации).
- Проверка ключа сервера по схеме **TOFU** (доверяем при первом подключении, дальше сверяем отпечаток), см. раздел 5.1.
- Код возврата команды (`exit_code`) берётся из SSH-протокола, а при убийстве сигналом равен `128 + номер сигнала`.
- Все ошибки возвращаются в ответе, бинарник не должен падать (no panic).

Что ещё **не реализовано** (прогресс передачи, переиспользование SFTP-канала, передача папок, Docker), см. раздел 9.

## 2. Поддержка платформ и транспорт

Бинарник автоматически выбирает транспорт в зависимости от ОС:

| Платформа         | Основной транспорт | Переменная окружения   | Fallback                            |
| ----------------- | ------------------ | ---------------------- | ----------------------------------- |
| **Windows**       | Named Pipe         | `RUST_IPC_PIPE_NAME`   | TCP на `RUST_IPC_TCP_FALLBACK_PORT` |
| **Linux / macOS** | Unix Domain Socket | `RUST_IPC_SOCKET_PATH` | TCP на `RUST_IPC_TCP_FALLBACK_PORT` |

**Порядок инициализации:**

1. Бинарник читает переменные окружения.
2. Пытается открыть основной транспорт для текущей ОС.
3. Если не получилось — падает на TCP `127.0.0.1:<порт>`.

Unix-сокет создаётся с правами `0600`: к нему имеет доступ только владелец.

## 3. Переменные окружения

Все переменные **опциональны**. Если не заданы — используются значения по умолчанию.

| Переменная                   | Описание                       | По умолчанию                                 |
| ---------------------------- | ------------------------------ | -------------------------------------------- |
| `RUST_BRIDGE_MODE`           | Режим работы (всегда `ipc`)    | `ipc`                                        |
| `RUST_IPC_SOCKET_PATH`       | Путь к Unix-сокету (Linux/Mac) | `/tmp/synex_worker.sock`                     |
| `RUST_IPC_PIPE_NAME`         | Имя Named Pipe (Windows)       | `\\.\pipe\synex_worker`                      |
| `RUST_IPC_TCP_FALLBACK_PORT` | Порт для TCP fallback          | `9100`                                       |
| `RUST_EXECUTABLE_PATH_UNIX`  | Путь к бинарнику на Unix       | `./app/core/target/release/synex_worker`     |
| `RUST_EXECUTABLE_PATH_WIN32` | Путь к бинарнику на Windows    | `./app/core/target/release/synex_worker.exe` |

**Пример установки в Python:**

```python
import os

env = os.environ.copy()
env["RUST_IPC_PIPE_NAME"] = r"\\.\pipe\synex_worker"
env["RUST_IPC_TCP_FALLBACK_PORT"] = "9100"
env["RUST_EXECUTABLE_PATH_WIN32"] = "./app/core/target/release/synex_worker.exe"
```

## 4. Протокол IPC

### 4.1. Физический уровень: фрейминг

Все сообщения передаются в формате **Length-Prefixed**:

```
[4 байта: длина payload в Big-Endian] [N байт: MessagePack payload]
```

**Почему так:** MessagePack — бинарный формат, внутри него могут встречаться байты `0x0A` (перенос строки) и `0x00`. Делить сообщения по `\n` нельзя.

**Лимит размера кадра:** `16 MiB` (`MAX_FRAME_LEN` в `synex_worker/src/main.rs`). Если заголовок объявляет больший размер, воркер:

1. отвечает `{"id": "", "status": "error", "payload": {"stderr": "frame too large: N bytes", ...}}`;
2. закрывает IPC-соединение.

Тело кадра при этом не читается. После такого ответа соединение нужно открыть заново.

### 4.2. Формат данных: MessagePack

Используется **MessagePack в режиме map** (словарь):

- На стороне Rust: `rmp_serde::to_vec_named(&resp)` — словарь с именами полей.
- На стороне Python: `msgpack.unpackb(data, raw=False)` — `dict`, строки как `str`.

⚠️ **Если использовать `to_vec` (без `_named`)**, Python получит список вместо словаря, и `response["status"]` упадёт.

### 4.3. Схема запроса (Python → Rust)

```json
Request {
    id: String,           // ID запроса. Для ssh_connect становится ID сессии
    action: String,       // тип действия
    payload: { ... }      // зависит от action
}
```

Общие поля `payload` для всех действий: `command`, `connection`, `remote_path`, `local_path`, `destination_path`, `target_id` (только для `cancel`). Остальные поля конкретного действия — в разделе 5 и разделе 8.

### 4.4. Схема ответа (Rust → Python)

```json
Response {
    id: String,           // тот же ID, что в запросе
    status: String,       // "ok" или "error" (см. ниже)
    payload: {
        stdout: String,   // вывод действия
        stderr: String,   // текст ошибки; у ошибок начинается с кода (см. раздел 7)
        exit_code: i32    // код возврата команды (0 = успех)
    }
}
```

**Смысл `status`:**

- `"ok"` — действие выполнено. Для `ssh_exec` это значит, что SSH отработал; реальный результат команды — в `exit_code`. Ненулевой `exit_code` при `"ok"` — нормальная ситуация.
- `"error"` — действие не выполнено (нет сессии, неверный payload, ошибка SFTP, проблема с ключом сервера и т.д.). Причина — в `stderr`.
- `"cancelled"` — действие остановлено действием `cancel` (см. 5.5). Приходит только от того действия, которое отменяли, под его исходным `id`.

Единый статус успеха — `"ok"` во всех действиях (включая SFTP).

**Несколько ответов на одном соединении:** воркер читает IPC и выполняет действия независимо. Пока `ssh_exec` или SFTP-передача ещё работают, по тому же соединению уже можно отправить `cancel` (и любые другие быстрые действия). Ответы приходят по мере готовности и различаются по `id`, поэтому клиент должен уметь принимать кадры в любом порядке.

## 5. Действия (Actions)

### 5.1. `ssh_connect` — подключение к серверу

**Обязательно** отправляется первым. Без успешного `ssh_connect` остальные действия вернут ошибку «Session dont founded».

**Запрос:**

```json
{
  "id": "sess-1",
  "action": "ssh_connect",
  "payload": {
    "connection": {
      "host": "203.0.113.10",
      "port": 22,
      "username": "root",
      "password": null,
      "key_path": "C:/Users/user/.ssh/id_ed25519",
      "passphrase": null,
      "host_key_fingerprint": "SHA256:Qm9ndXNLZXlGb3JEb2NzQ29kZXhQbGFjZWhvbGRlcj0"
    }
  }
}
```

**Поля `connection`:**

| Поле                   | Тип         | Описание                                                                                           |
| ---------------------- | ----------- | -------------------------------------------------------------------------------------------------- |
| `host`                 | string      | IP или домен сервера                                                                               |
| `port`                 | int         | SSH-порт (обычно 22)                                                                               |
| `username`             | string      | Имя пользователя                                                                                   |
| `key_path`             | string/null | Путь к **приватному** ключу на машине, где запущен воркер. Если задан, используется вместо пароля  |
| `passphrase`           | string/null | Пароль от приватного ключа (только вместе с `key_path`)                                            |
| `password`             | string/null | Пароль пользователя. Используется, если `key_path` не задан. Не логируется                         |
| `host_key_fingerprint` | string/null | Доверенный отпечаток ключа сервера в формате `SHA256:<base64>`. Необязательное поле, см. TOFU ниже |

Если не задан ни `key_path`, ни `password`, возвращается `status: "error"`, а в `stderr` строка `Connection failed: SSH internal error: No authentication method provided`.

**Проверка ключа сервера (TOFU).** Воркер не хранит `known_hosts`, доверенные отпечатки хранит backend и передаёт их в `host_key_fingerprint`:

| Ситуация                          | Ответ                                                                                                                                                                              |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `host_key_fingerprint` не передан | `status: "error"`, `stderr: "host_key_unknown: unknown host key SHA256:..."`, в `payload` есть `fingerprint` и `key_type`. Подключение не продолжается                             |
| Передан и совпал с ключом сервера | Подключение продолжается, возвращается `status: "ok"`                                                                                                                              |
| Передан, но не совпал             | `status: "error"`, `stderr: "host_key_mismatch: expected X, got Y"`, в `payload` есть `fingerprint` (полученный) и `expected_fingerprint` (ожидаемый). Подключение не продолжается |

Схема для backend:

1. Первое подключение без отпечатка → получить `host_key_unknown` с `fingerprint` и `key_type`.
2. Показать отпечаток пользователю и после подтверждения сохранить его.
3. Повторить `ssh_connect` с `host_key_fingerprint`.
4. При `host_key_mismatch` подключение не продолжать, а предупредить пользователя: ключ сервера изменился (возможна атака «человек посередине» или сервер переустановлен).

**Успешный ответ:**

```json
{
  "id": "sess-1",
  "status": "ok",
  "payload": {
    "stdout": "Connected successfully, id: sess-1",
    "stderr": "",
    "exit_code": 0,
    "fingerprint": "SHA256:Qm9ndXNLZXlGb3JEb2NzQ29kZXhQbGFjZWhvbGRlcj0",
    "key_type": "ssh-ed25519"
  }
}
```

В успешном ответе `fingerprint` и `key_type` возвращаются всегда. В ответах об ошибке без проверки ключа эти поля отсутствуют.

**Ошибка аутентификации или сети:**

```json
{
  "id": "sess-1",
  "status": "error",
  "payload": {
    "stdout": "",
    "stderr": "Connection failed: SSH authentication failed: access denied",
    "exit_code": 1
  }
}
```

### 5.2. `ssh_exec` — выполнение команды

**Запрос:**

```json
{
  "id": "sess-1",
  "action": "ssh_exec",
  "payload": {
    "command": "ls -la /tmp"
  }
}
```

**Поле `command`:**

- Обычная строка с командой. Поддерживаются цепочки: `cd /tmp && ls -la`.
- Воркер не хранит `cwd` между командами. Если нужна рабочая папка, добавляйте `cd` в каждую команду или храните `cwd` на стороне Python.

**Код возврата (`exit_code`):**

- Обычный выход: `exit_code` равен коду процесса (`exit 3` → `3`, `false` → `1`).
- Процесс убит сигналом: `exit_code = 128 + номер сигнала` (`kill -9 $$` → `137`, `kill -TERM $$` → `143`). Сервер должен прислать `exit-signal`; это зависит от sshd.
- `stderr` приходит в `payload.stderr`, `stdout` — в `payload.stdout`.

**Успешный ответ:**

```json
{
  "id": "sess-1",
  "status": "ok",
  "payload": {
    "stdout": "file1.txt\nfile2.txt",
    "stderr": "",
    "exit_code": 0
  }
}
```

**Команда завершилась с ошибкой** (например, команда не найдена). Это `"ok"`, потому что SSH отработал:

```json
{
  "id": "sess-1",
  "status": "ok",
  "payload": {
    "stdout": "",
    "stderr": "bash: unknown_cmd: command not found",
    "exit_code": 127
  }
}
```

**Сессия не найдена** (нет `ssh_connect` с этим `id`):

```json
{
  "id": "sess-1",
  "status": "error",
  "payload": {
    "stdout": "",
    "stderr": "Session dont founded by key. Create session first!",
    "exit_code": 1
  }
}
```

Если `command` не передан: `stderr: "Missing 'command' in payload"`.

**Отмена:** команда выполняется в своей задаче, поэтому её можно остановить действием `cancel` (см. 5.5) с `target_id` = id этой сессии. Воркер пошлёт процессу `TERM`, через 3 с — `KILL`, и ответит по этому же `id` со `status: "cancelled"` и `exit_code: 143`.

### 5.3. `monitor_get_values` — метрики сервера

**Запрос:**

```json
{
  "id": "sess-1",
  "action": "monitor_get_values",
  "payload": {}
}
```

Воркер выполняет на сервере команду сбора метрик и разбирает её вывод. Метрики приходят **JSON-строкой** в `payload.stdout`:

```json
{
  "id": "sess-1",
  "status": "ok",
  "payload": {
    "stdout": "{\"ok\":true,\"cpu_usage_percent\":2.63,\"mem_total_kb\":2014404,\"mem_available_kb\":793976,\"mem_used_kb\":1220428,\"mem_usage_percent\":60.59,\"net_rx_bytes_total\":123456789,\"net_tx_bytes_total\":98765432,\"net_rx_bytes_per_sec\":null,\"net_tx_bytes_per_sec\":null,\"disk_total_kb\":41152736,\"disk_used_kb\":12345678,\"disk_available_kb\":26000000,\"disk_usage_percent\":32.2,\"error\":null}",
    "stderr": "",
    "exit_code": 0
  }
}
```

Поля JSON в `stdout`:

| Поле                   | Тип         | Описание                                                                                          |
| ---------------------- | ----------- | ------------------------------------------------------------------------------------------------- |
| `ok`                   | bool        | `true`, если метрики разобраны                                                                    |
| `cpu_usage_percent`    | float       | Загрузка CPU в процентах. При первом вызове в сессии `0.0`, потому что предыдущего замера ещё нет |
| `mem_total_kb`         | int         | Всего памяти, КБ                                                                                  |
| `mem_available_kb`     | int         | Доступно памяти, КБ                                                                               |
| `mem_used_kb`          | int         | Использовано памяти, КБ                                                                           |
| `mem_usage_percent`    | float       | Использование памяти в процентах                                                                  |
| `net_rx_bytes_total`   | int/null    | Принято байт по всем интерфейсам, кроме `lo` (счётчик из `/proc/net/dev`)                         |
| `net_tx_bytes_total`   | int/null    | Отправлено байт по всем интерфейсам, кроме `lo`                                                   |
| `net_rx_bytes_per_sec` | float/null  | Скорость приёма, байт/с. Дельта с прошлого вызова в этой сессии. `null` при первом вызове         |
| `net_tx_bytes_per_sec` | float/null  | Скорость отправки, байт/с. Так же, `null` при первом вызове                                       |
| `disk_total_kb`        | int/null    | Размер корневого раздела `/`, КБ (`df -P -k /`)                                                   |
| `disk_used_kb`         | int/null    | Занято на `/`, КБ                                                                                 |
| `disk_available_kb`    | int/null    | Доступно на `/`, КБ                                                                               |
| `disk_usage_percent`   | float/null  | Занятость `/` в процентах: `used / (used + available) * 100`, как колонка Capacity в `df`         |
| `error`                | string/null | Текст ошибки разбора, если `ok` = `false`                                                         |

Примечания:

- Диск считается командой `df -P -k /`, а не `df -h /`: вывод `-h` читается человеком, и размеры в нем округлены. `-P` фиксирует формат одной строкой на файловую систему.
- Сетевые счётчики суммируются по всем интерфейсам, кроме `lo`. В хосте с Docker трафик через мосты (`docker0`, `veth*`) подсчитывается дважды.
- Скорости считаются по разнице счётчиков между запросами в одной сессии. Если счётчик уменьшился (перезагрузка или сброс), скорость будет `0`.

Если метрики не удалось разобрать, `stdout` содержит JSON с `"ok": false` и `"error": "..."`. Сам `status` при этом остаётся `"ok"`.

**Сессия не найдена:** `status: "error"`, `stderr: "Session dont founded by key. Create session first!"`.

### 5.4. `ssh_disconnect` — закрытие сессии

```json
{
  "id": "sess-1",
  "action": "ssh_disconnect",
  "payload": {}
}
```

- Успех: `status: "ok"`, `stdout: "Disconnected id: sess-1"`.
- Сессии нет: `status: "error"`, `stderr: "Session not found"`.
- Если на сессии ещё идёт `ssh_exec` или SFTP-передача, она сначала отменяется (как `cancel`), а закрытие ждёт её завершения.

### 5.5. `cancel` — отмена текущей операции

Останавливает действие, которое сейчас выполняется на сессии: `ssh_exec`, `sftp_upload_file` или `sftp_download_file`. Отменять больше нечего — остальные действия короткие и в очереди на отмену не попадают.

**Запрос:**

```json
{
  "id": "cancel-1",
  "action": "cancel",
  "payload": {
    "target_id": "sess-1"
  }
}
```

`target_id` — `id` сессии (тот же, что в `ssh_connect`). На сессию одновременно идёт не больше одной операции, поэтому отдельный id операции не нужен.

**Ответ на сам `cancel`** (приходит сразу):

```json
{
  "id": "cancel-1",
  "status": "ok",
  "payload": {
    "stdout": "Cancel requested for sess-1",
    "stderr": "",
    "exit_code": 0
  }
}
```

- `target_id` не передан: `status: "error"`, `stderr: "Missing 'target_id' in payload"`.
- На этой сессии ничего не выполняется: `status: "error"`, `stderr: "No cancellable operation for id: sess-1"`. Это нормальная ситуация: операция могла успеть закончиться.

**Ответ отменённой операции** приходит отдельно, под её исходным `id`:

```json
{
  "id": "sess-1",
  "status": "cancelled",
  "payload": { "stdout": "", "stderr": "", "exit_code": 143 }
}
```

Что именно делает отмена:

| Действие                                  | Что происходит                                                               |
| ----------------------------------------- | ---------------------------------------------------------------------------- |
| `ssh_exec`                                | Удалённому процессу шлётся `TERM`, через 3 с — `KILL`. `exit_code: 143`      |
| `sftp_upload_file` / `sftp_download_file` | Копирование обрывается сразу, файлы закрываются. Частичный файл не удаляется |

Порядок важен: если отмена пришла до того, как команда начала выполняться (ждала очередь на сессию), она не запускается вовсе. Воркер обрабатывает IPC отдельно от действий, поэтому `cancel` доходит, пока команда/передача ещё идёт.

При закрытии IPC-соединения (клиент отключился) воркер отменяет все действия этого соединения и закрывает его сессии.

## 6. Примеры на Python

Полный рабочий клиент с тестами лежит в `core/rust_tests.py`. Ниже — минимум, которого достаточно для вызова воркера.

### 6.1. Клиент (рекомендуемый)

Кадр: 4 байта длины (Big-Endian) + MessagePack. Запись и чтение идут под одной блокировкой, потому что сокет общий для потоков.

```python
import socket
import struct
import threading

import msgpack

MAX_FRAME_LEN = 16 * 1024 * 1024


class SynexClient:
    def __init__(self, sock_path="/tmp/synex_worker.sock"):
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path)

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                return b""
            data += chunk
        return data

    def send(self, request):
        packed = msgpack.packb(request, use_bin_type=True)
        with self.lock:
            self.sock.sendall(struct.pack(">I", len(packed)) + packed)
            header = self._recv_exact(4)
            if len(header) < 4:
                raise RuntimeError("Worker закрыл соединение")
            payload = self._recv_exact(struct.unpack(">I", header)[0])
        return msgpack.unpackb(payload, raw=False)

    def connect_ssh(self, session_id, host, username, password=None, key_path=None,
                    passphrase=None, host_key_fingerprint=None):
        return self.send({
            "id": session_id,
            "action": "ssh_connect",
            "payload": {"connection": {
                "host": host,
                "port": 22,
                "username": username,
                "password": password,
                "key_path": key_path,
                "passphrase": passphrase,
                "host_key_fingerprint": host_key_fingerprint,
            }},
        })

    def ssh_exec(self, session_id, command):
        return self.send({"id": session_id, "action": "ssh_exec", "payload": {"command": command}})

    def disconnect_ssh(self, session_id):
        return self.send({"id": session_id, "action": "ssh_disconnect", "payload": {}})
```

### 6.2. Использование с TOFU

```python
import os

client = SynexClient()
host = os.environ["SSH_TEST_HOST"]
user = os.environ.get("SSH_TEST_USER", "root")
password = os.environ["SSH_TEST_PASS"]

# 1. Первое подключение: отпечатка ещё нет
resp = client.connect_ssh("sess-1", host, user, password=password)
if resp["status"] == "error" and resp["payload"]["stderr"].startswith("host_key_unknown"):
    fingerprint = resp["payload"]["fingerprint"]
    # Показать fingerprint пользователю и дождаться подтверждения.
    # Сохранить его в своём хранилище (в backend).
    resp = client.connect_ssh("sess-1", host, user, password=password,
                              host_key_fingerprint=fingerprint)

if resp["status"] == "ok":
    print(client.ssh_exec("sess-1", "uname -a")["payload"]["stdout"])
    client.disconnect_ssh("sess-1")
```

### 6.3. Транспорт Windows (Named Pipe)

На Windows вместо `AF_UNIX` открывается файл `\\.\pipe\synex_worker`, и `send` работает так же, но через `read`/`write` файлового объекта. В `rust_tests.py` для этого есть ветка `transport == "pipe"`.

## 7. Обработка ошибок

Все ошибки приходят в ответе с `status: "error"`, причина — в `payload.stderr`. Если у ошибки есть код, он стоит в начале строки и отделён `: `.

### Ошибки на уровне IPC

| Сообщение                         | Причина                                                      | Что делать                                 |
| --------------------------------- | ------------------------------------------------------------ | ------------------------------------------ |
| `frame too large: N bytes`        | Заголовок кадра больше `16 MiB`. Воркер закрывает соединение | Переподключиться, уменьшить размер запроса |
| `Unknown action: <name>`          | Неизвестное значение `action`                                | Проверить имя действия по разделу 5 и 8.4  |
| `Missing 'connection' in payload` | В `ssh_connect` нет `connection`                             | Передать `connection`                      |

Если кадр не распаковался в MessagePack, воркер отвечает с `id: "bad_msgpack"` и `stderr: "Failed to unpack: ..."`, соединение остаётся открытым, воркер продолжает работать со следующим кадром.

### Ошибки SSH

| Сообщение (stderr)                                                         | Причина                                                          | Что делать                                                     |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------- |
| `Session dont founded by key. Create session first!`                       | Нет сессии с таким `id` (не было `ssh_connect` или он не прошёл) | Выполнить `ssh_connect` с этим `id`                            |
| `host_key_unknown: unknown host key SHA256:...`                            | `host_key_fingerprint` не передан                                | Показать отпечаток пользователю, повторить с ним               |
| `host_key_mismatch: expected X, got Y`                                     | Ключ сервера не совпал с сохранённым                             | Не подключаться. Проверить, не сменился ли сервер или не атака |
| `Connection failed: SSH authentication failed: access denied`              | Сервер отказал в ключе или пароле                                | Проверить логин, пароль, ключ и `passphrase`                   |
| `Connection failed: SSH internal error: No authentication method provided` | Не передан ни `key_path`, ни `password`                          | Передать один из них                                           |
| `Connection failed: ...`                                                   | Сеть, порт, таймаут SSH                                          | Проверить `host`, `port`, доступность сервера                  |
| `Session not found`                                                        | `ssh_disconnect` для неизвестного `id`                           | Ничего, сессия уже закрыта                                     |

Ошибки `ssh_connect`, которых нет в таблице, приходят с префиксом `Connection failed: ` (сеть, порт, таймаут). Ошибки `ssh_exec` и SFTP приходят как `SSH protocol error: ...` (ошибка russh) или `SSH internal error: ...`. SFTP-ошибки дополнительно начинаются с кода `sftp_*`.

### Ошибки выполнения команд

- Команда завершилась с ненулевым кодом: `status: "ok"`, `exit_code` = код процесса. Это не ошибка воркера.
- Процесс убит сигналом: `exit_code = 128 + номер сигнала` (137 для `SIGKILL`, 143 для `SIGTERM`). Сервер должен прислать `exit-signal`; если sshd его не присылает, код будет `0`.
- Ошибки `ssh_exec` без сессии и без `command` описаны выше.

## 8. SFTP: структура payload и примеры

### 8.1. Структура RequestPayload

Все SFTP-действия читают поля из общего `payload`:

```rust
struct RequestPayload {
    command: Option<String>,
    connection: Option<ConnectPayload>,
    remote_path: Option<String>,
    local_path: Option<String>,
    destination_path: Option<String>,
    target_id: Option<String>, // только для cancel
}
```

`ConnectPayload` содержит `host`, `port`, `username`, `key_path`, `passphrase`, `password`, `host_key_fingerprint`. Поле `target_id` добавлено для действия `cancel` и в остальных действиях не используется.

### 8.2. Сессия = ID запроса

SFTP-действия используют ту же SSH-сессию, что и `ssh_exec`. `id` в запросе SFTP — это `id` сессии, полученный из `ssh_connect`.

Если сессии нет, ответ: `status: "error"`, `stderr: "Session dont founded by key. Create session first!"`.

Каждое SFTP-действие открывает свой SFTP-канал и закрывает его после выполнения. Переиспользование канала пока не реализовано (см. раздел 9).

### 8.3. SFTP-действия и примеры

**Список директории** (`sftp_list_dir`, payload `remote_path`):

```json
{
  "id": "sess-1",
  "action": "sftp_list_dir",
  "payload": { "remote_path": "/root/app" }
}
```

Ответ: `stdout` — JSON-массив записей (см. 8.4).

**Информация об одном пути** (`sftp_stat`, payload `remote_path`). Путь проверяется через `lstat`, симлинк не переходится. `stdout` — JSON одной записи:

```json
{
  "id": "sess-1",
  "action": "sftp_stat",
  "payload": { "remote_path": "/root/app/link" }
}
```

**Загрузка файла** (`sftp_upload_file`, payload `local_path`, `remote_path`). Если `remote_path` заканчивается на `/` или указывает на существующую папку, файл кладётся в неё под своим именем:

```json
{
  "id": "sess-1",
  "action": "sftp_upload_file",
  "payload": { "local_path": "C:/data/report.txt", "remote_path": "/root/app/" }
}
```

**Скачивание файла** (`sftp_download_file`, payload `remote_path`, `local_path`). Аналогично: если `local_path` — существующая папка, файл сохраняется в неё под своим именем.

**Перемещение** (`sftp_move_file`, payload `remote_path` как источник, `destination_path` как цель). Если цель уже существует, возвращается `sftp_exists`.

**Перемещение папки** (`sftp_move_dir`, payload `remote_path` как исходная папка, `destination_path` как новый путь). В отличие от `sftp_move_file`, источник обязан быть папкой: файл или симлинк (даже на папку) возвращают `sftp_not_a_directory`. Несуществующий источник — `sftp_not_found`, существующая цель — `sftp_exists`. Содержимое папки переносится целиком, `rename` на сервере.

Пример:

```json
{
  "id": "sess-1234",
  "action": "sftp_move_dir",
  "payload": {
    "remote_path": "/root/app/releases/tmp",
    "destination_path": "/root/app/releases/v2"
  }
}
```

**Копирование** (`sftp_copy_file`, payload `remote_path`, `destination_path`). SFTP не умеет копировать напрямую, поэтому воркер выполняет `cp -- SRC DST` на сервере. Ненулевой код `cp` возвращается как ошибка с текстом stderr.

**Создание и удаление:** `sftp_create_dir`, `sftp_remove_file`, `sftp_remove_dir` (payload `remote_path`). `sftp_remove_dir` удаляет только пустую папку.

### 8.4. Таблица SFTP-действий

| Действие             | Payload                                            | Успешный `stdout`   |
| -------------------- | -------------------------------------------------- | ------------------- |
| `sftp_list_dir`      | `remote_path`                                      | JSON-массив записей |
| `sftp_stat`          | `remote_path`                                      | JSON одной записи   |
| `sftp_create_dir`    | `remote_path`                                      | пусто               |
| `sftp_remove_file`   | `remote_path`                                      | пусто               |
| `sftp_remove_dir`    | `remote_path` (только пустая папка)                | пусто               |
| `sftp_upload_file`   | `local_path`, `remote_path`                        | пусто               |
| `sftp_download_file` | `remote_path`, `local_path`                        | пусто               |
| `sftp_move_file`     | `remote_path` (источник), `destination_path`       | пусто               |
| `sftp_move_dir`      | `remote_path` (исходная папка), `destination_path` | пусто               |
| `sftp_copy_file`     | `remote_path` (источник), `destination_path`       | пусто               |

Поля записи в `sftp_list_dir` и `sftp_stat`:

| Поле          | Тип         | Описание                                             |
| ------------- | ----------- | ---------------------------------------------------- |
| `Type`        | string      | `folder`, `file` или `symlink`                       |
| `name`        | string      | Имя файла или папки                                  |
| `size`        | int         | Размер в байтах                                      |
| `rights`      | string      | Права в формате `rwxr-xr-x` (9 символов)             |
| `mtime`       | int/null    | Время изменения, Unix-время в секундах               |
| `uid`         | int/null    | Владелец (числовой ID)                               |
| `gid`         | int/null    | Группа (числовой ID)                                 |
| `link_target` | string/null | Куда указывает симлинк, для остальных записей `null` |

Все поля присутствуют всегда, отсутствующие значения — `null`. Успешный статус у всех SFTP-действий — `"ok"`.

### 8.5. Ошибки SFTP и решения

Ошибки SFTP начинаются с кода из `SftpErrorKind`:

| Код                      | Когда возникает                                                  | Что делать                              |
| ------------------------ | ---------------------------------------------------------------- | --------------------------------------- |
| `sftp_unavailable`       | Сервер отказал в подсистеме `sftp` или не ответил на её открытие | Проверить, включён ли SFTP на сервере   |
| `sftp_not_found`         | Путь не существует                                               | Проверить путь                          |
| `sftp_permission_denied` | Нет прав на чтение, запись или удаление                          | Проверить права пользователя на сервере |
| `sftp_exists`            | Цель уже существует (`create_dir`, `move`)                       | Выбрать другое имя или удалить цель     |
| `sftp_not_a_directory`   | Ожидалась папка, а путь — файл                                   | Проверить тип пути                      |
| `sftp_failed`            | Прочие ошибки SFTP-сервера                                       | Смотреть текст после кода               |

Пример: `stderr: "sftp_not_found: Failed to read remote directory: No such file"`.

Ошибки, которые возникают до SFTP (например, не открылся локальный файл при загрузке), идут с префиксом `SSH internal error:` без кода. Сообщение `Missing file path in payload` (и подобные `Missing ... in payload`) означает, что в payload нет нужного поля.

### 8.6. Клиент из `rust_tests.py`

`core/rust_tests.py` содержит класс `SynexClient` с методами: `connect_ssh` (с `host_key_fingerprint`), `ssh_exec`, `monitor`, `cancel`, `send_only`/`recv_frame` (для параллельной отмены), `docker_info`, `docker_containers`, `docker_images`, `docker_stats`, `docker_logs`, `docker_container_action`, `docker_container_remove`, `list_dir`, `sftp_stat`, `sftp_create_dir`, `sftp_remove_file`, `sftp_remove_dir`, `sftp_download_file`, `sftp_upload_file`, `sftp_move_file`, `sftp_move_dir`, `sftp_copy_file`, `disconnect_ssh`, `close_ssh`. Клиент поддерживает Named Pipe, Unix-сокет и TCP fallback. Документация протокола: [README.ru.md](README.ru.md) (рус.), [README.md](README.md) (англ.).

Наборы проверок:

- `run_tofu_tests`: `host_key_unknown` без отпечатка, успешное подключение с отпечатком, `host_key_mismatch` с чужим отпечатком;
- `run_exec_tests`: `exit_code` (`exit 3` → 3, `false` → 1, `kill -9 $$` → 137, `kill -TERM $$` → 143), stderr, неизвестная сессия;
- `run_monitor_tests` (вызвается из `run_exec_tests`): `monitor_get_values` дважды в одной сессии. На первом вызове скорости сети `null`, на втором числа; счетчики rx не уменьшаются; диск `/` заполнен, `disk_usage_percent` в 0..100;
- `run_sftp_tests`: создание, загрузка, скачивание, перемещение файлов, перемещение папки (`sftp_move_dir`: содержимое едет вместе с папкой, коды `sftp_not_found`, `sftp_not_a_directory`, `sftp_exists`), копирование, удаление;
- `run_sftp_error_tests`: коды ошибок SFTP, `sftp_stat` (файл, папка, симлинк), поля `list_dir`;
- `run_cancel_tests`: `cancel` без `target_id` и по несуществующей цели — ошибки; `sleep 100` отменяется (`status: "cancelled"`, ответ за секунды); загрузка файла отменяется, ответ `cancelled`;
- `run_docker_tests`: `docker_info` (конверт `ok`, статус из допустимых), `docker_containers`/`docker_images` (массивы, поля `ContainerInfo`), `docker_stats`. Если Docker на сервере недоступен, остальные Docker-проверки пропускаются;
- `run_socket_permission_test`: права `0600` у Unix-сокета (только unix);
- `run_frame_limit_test`: кадр сверх `16 MiB`.

Все удалённые пути лежат внутри `/root/synex_test_<random>`. Удаление проверяет префикс, чтобы не выйти за тестовую папку.

### 8.7. Запуск тестов

1. Собрать воркер: `cargo build -p synex_worker --release` (из папки `core`).
2. Запустить воркер отдельно. В `__main__` стоит `SynexClient(spawn_worker=False)`, поэтому тесты сами его не запускают.
3. Задать переменные окружения:

| Переменная                | Обязательна  | По умолчанию | Описание                                    |
| ------------------------- | ------------ | ------------ | ------------------------------------------- |
| `SSH_TEST_HOST`           | —            | —            | Не читается: адрес зашит в `load_config()`  |
| `SSH_TEST_PORT`           | нет          | `22`         | SSH-порт                                    |
| `SSH_TEST_USER`           | нет          | `root`       | Пользователь                                |
| `SSH_TEST_PASS`           | —            | —            | Не читается: пароль зашит в `load_config()` |
| `SSH_TEST_KEY`            | одна из двух | —            | Путь к приватному ключу                     |
| `SSH_TEST_KEY_PASSPHRASE` | нет          | —            | Пароль от ключа                             |

Если в `load_config()` не задан ни пароль, ни ключ, скрипт выходит с кодом `2`.

4. Запустить из папки `core`: `python rust_tests.py`. Код выхода `0` — все проверки прошли, `1` — есть провалы.

Порядок: TOFU → exec → SFTP → SFTP-ошибки → отмена → Docker → disconnect → права сокета → лимит кадра. Проверка лимита кадра последняя, потому что после неё соединение уже закрыто.

**Важно:** тесты работают на реальном сервере и создают/удаляют файлы в `/root/synex_test_*`. Запускайте их только на тестовом сервере.

**Ручной сценарий смены host key** (на тестовом сервере, не на боевом):

1. Подключиться с отпечатком, он сохраняется.
2. На сервере заменить `/etc/ssh/ssh_host_ed25519_key` и `.pub`, перезапустить sshd.
3. Повторить подключение с тем же отпечатком: ожидается `host_key_mismatch`.

Автоматически этот сценарий тесты не проверяют.

**Что не проверено на рантайме** (зависит от сервера): `exit_code` 137/143 для `kill -9 $$` и `kill -TERM $$` (нужен `exit-signal` от sshd); `cpu_usage_percent` при первом вызове `monitor_get_values` всегда `0.0`.

## 9. Статус реализации

**Сделано:**

- `exit_code` из `ExitStatus`, сигнал → `128 + N`;
- мониторинг: сеть (`net_*`, скорости как дельта с прошлого вызова в сессии) и диск `/` (`disk_*`, из `df -P -k /`);
- TOFU для `ssh_connect` (`host_key_unknown`, `host_key_mismatch`, поля `fingerprint` и `key_type`);
- ограничение размера кадра (16 MiB);
- права `0600` у Unix-сокета;
- коды ошибок SFTP, `sftp_stat`, поля записей `list_dir`, единый статус `ok`;
- перемещение папки (`sftp_move_dir`);
- отмена действий: `cancel` с `target_id`, `status: "cancelled"`, `ssh_exec` — TERM → KILL через 3 с, SFTP-передачи обрываются сразу; IPC читается параллельно с выполнением действий (запись ответов под общим `Mutex`); при обрыве IPC все действия отменяются;
- Docker через SSH-туннель к `/var/run/docker.sock` (bollard, `direct-streamlocal@openssh.com`), действия `docker_*`, коды `docker_*`, живой лог через `docker_logs` с `follow`.

**Не сделано:**

- **Прогресс SFTP** (промежуточные кадры `status: "progress"`).
- **Переиспользование SFTP-канала** в рамках сессии.
- **Передача папок** (`sftp_upload_dir`, `sftp_download_dir`).
- **Сборки под musl и macOS**, сборка Windows-exe с SFTP.

**Вне зоны воркера:** backend пока не передаёт `host_key_fingerprint` (TOFU) и пока управляет Docker через `docker` CLI.

## 10. Docker

Docker работает на удалённом сервере. Воркер не открывает порт наружу: в той же SSH-сессии открывается канал `direct-streamlocal@openssh.com` к `/var/run/docker.sock`, и по нему идёт обычный HTTP (клиент — `bollard`, custom transport).

Как устроено:

- На каждый HTTP-запрос открывается свой канал к сокету: сессия занята только на время открытия, а не на всё действие. Поэтому долгий `docker_logs -f` не блокирует `monitor` и `ssh_exec`.
- `id` запроса — тот же `id` сессии, что в `ssh_connect`.
- Успешный `stdout` — JSON по форме backend (`backend/app/plugins/docker/schemas.py`).
- Ошибки: конверт `status: "error"`, в начале `stderr` — код `docker_*` (см. 10.5).
- Путь сокета — `/var/run/docker.sock` (константа `DEFAULT_SOCKET`).

### 10.1. Действия

| Действие                  | Payload                                | Успешный `stdout`                                                                                        |
| ------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `docker_info`             | —                                      | объект `DockerInfo`: `status`, `detail`, `client_version`, `server_version`, `api_version`, `os`, `arch` |
| `docker_containers`       | `all` (bool)                           | массив `ContainerInfo`                                                                                   |
| `docker_container_action` | `container`, `action`, `timeout`       | объект `{container, action, output}` — `container` это состояние после действия                          |
| `docker_container_remove` | `container`, `force`, `volumes`        | пусто                                                                                                    |
| `docker_logs`             | `container`, `tail`, `since`, `follow` | объект `{lines, next_since}`                                                                             |
| `docker_stats`            | `container` (опционально)              | массив `ContainerStatsInfo`                                                                              |
| `docker_images`           | —                                      | массив `ImageInfo`                                                                                       |

`action` у `docker_container_action`: `start`, `stop`, `restart`, `pause`, `unpause`, `kill`. `timeout` — это grace-период Docker для `stop`/`restart` (секунды), а не таймаут запроса: сам запрос ограничен сверху (60 с, а `docker_timeout` — когда превышен).

`docker_stats` без `container` берёт все запущенные контейнеры.

### 10.2. Поля ответов

`ContainerInfo` (как в backend):

| Поле              | Тип         | Описание                                                                   |
| ----------------- | ----------- | -------------------------------------------------------------------------- |
| `id`              | string      | Полный ID контейнера (64 символа)                                          |
| `name`            | string      | Имя без ведущего `/`                                                       |
| `names`           | string[]    | Все имена                                                                  |
| `image`           | string      | Образ                                                                      |
| `command`         | string      | Команда запуска                                                            |
| `created_at`      | string/null | RFC3339, UTC                                                               |
| `state`           | string      | `created`, `running`, `paused`, `restarting`, `removing`, `exited`, `dead` |
| `status`          | string      | Как в `docker ps`: `Up 5 minutes (healthy)`                                |
| `health`          | string/null | `healthy`, `unhealthy`, `starting`                                         |
| `exit_code`       | int/null    | Код выхода остановленного контейнера                                       |
| `ports`           | object[]    | `{container_port, protocol, host_ip, host_port}`, все строки               |
| `labels`          | object      | Метки контейнера                                                           |
| `networks`        | string[]    | Имена сетей                                                                |
| `compose_project` | string/null | Метка `com.docker.compose.project`                                         |
| `compose_service` | string/null | Метка `com.docker.compose.service`                                         |

`LogLineInfo`: `{time, stream, text}`, где `time` — RFC3339 с наносекундами (UTC), `stream` — `stdout` или `stderr`. `next_since` — время последней строки, его передают в `since` следующего запроса.

`since` принимает unix-секунды (`1700000000`), RFC3339 (`2026-11-06T12:00:00Z`) и длительность (`30s`, `10m`, `1h30m`). `tail` — число строк (по умолчанию все).

`ContainerStatsInfo`: `id`, `name`, `cpu_percent` (100 = одно ядро), `memory_used`, `memory_limit`, `memory_percent`, `net_rx`, `net_tx`, `block_read`, `block_write`, `pids`. Байты — числа.

CPU считается по двум замерам Docker подряд (около секунды): если второй замер не пришёл, `cpu_percent` будет `null`.

`ImageInfo`: `id` (без `sha256:`), `repository`, `tag`, `digest`, `created_at`, `size` (байты).

### 10.3. `docker_info` и его статусы

Это ответ на вопрос, а не сбой, поэтому конверт всегда `status: "ok"`, а результат — в `status` внутри JSON:

| `status`             | Когда                                                     |
| -------------------- | --------------------------------------------------------- |
| `ok`                 | Демон ответил, `server_version`/`api_version` заполнены   |
| `not_installed`      | Нет `/var/run/docker.sock` и нет `docker` на сервере      |
| `permission_denied`  | Сокет есть, но у пользователя SSH нет прав (`test -w`)    |
| `daemon_unavailable` | Сокет есть (или `docker` установлен), но демон не ответил |

Проверка делается через `ssh_exec` (`test -S`, `test -w`, `command -v docker`), потому что ошибка открытия канала SSH не несёт текста с errno.

### 10.4. Живой лог (`docker_logs` с `follow: true`)

- Промежуточные кадры: `status: "progress"`, `stdout` — JSON `{lines, next_since}` (только новые строки).
- Финал: `status: "cancelled"` (если остановили через `cancel` с `target_id` = id сессии) или `status: "ok"` (поток закрыл сам демон, например контейнер остановился).
- Заказчик должен уметь принимать кадры с одинаковым `id` много раз.

### 10.5. Коды ошибок

| Код                          | Когда                                                                    |
| ---------------------------- | ------------------------------------------------------------------------ |
| `docker_not_installed`       | Docker не установлен                                                     |
| `docker_permission_denied`   | Нет доступа к сокету/демону                                              |
| `docker_daemon_unavailable`  | Демон недоступен                                                         |
| `docker_container_not_found` | Нет такого контейнера                                                    |
| `docker_container_conflict`  | Действие не подходит к состоянию (`is not running`, `already paused`...) |
| `docker_timeout`             | Действие не уложилось в лимит                                            |
| `docker_command_failed`      | Прочие ошибки демона                                                     |

Пример: `stderr: "docker_container_not_found: No such container: web"`.

### 10.6. Сокращения

Пока не сделано (задача не требует): `docker_logs` с `follow` не переживает переподключение SSH-сессии, а `docker_stats` опрашивает контейнеры по одному (у Docker API нет массового `stats`).
