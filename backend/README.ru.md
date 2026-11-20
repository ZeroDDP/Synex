# Synex backend

[English](README.md) | **Русский**

FastAPI-сервер между UI и Rust-воркером `synex_worker` (протокол воркера описан в
[core/README.md](../core/README.md)). Работает с SSH-сессиями через воркер (несколько
одновременно, у каждой свой терминал по WebSocket), хранит SSH-ключи в зашифрованном
виде и сохранённые подключения, передаёт UI метрики серверов (CPU, RAM, сеть, диск) по
WebSocket каждые 500 мс, управляет Docker, git и файлами (SFTP) на серверах. Каждая
функция — отдельный плагин.

## Запуск

Нужны Python 3.12 и [uv](https://docs.astral.sh/uv/).

**PowerShell** (Windows):

```powershell
cd backend
uv sync                      # зависимости, включая dev-группу (pytest, ruff)
uv run python -m app.main    # http://127.0.0.1:8000
```

Бэкенд сам запускает `app/core/target/release/synex_worker.exe` как дочерний процесс,
подключается к нему по Named Pipe (Linux/macOS: Unix socket, запасной вариант TCP)
и перезапускает воркер, если тот упал. Настройки лежат в `.env`, образец: `.env.example`.
Пустое значение (`DEBUG=`) означает значение по умолчанию. Если значение неверное,
бэкенд не запустится и напишет, какая настройка и что с ней не так.

Не запускайте uvicorn с `--reload` или `--workers` на Windows: тогда uvicorn
использует SelectorEventLoop, на котором Named Pipe не работает.

### Linux и macOS

```bash
cd backend
uv sync
uv run python -m app.main
```

В репозитории лежит `app/core/target/release/synex_worker` для Linux x86_64. Ему нужна
glibc 2.38 или новее (Ubuntu 24.04, Debian 13, Fedora 39 и новее). На более старом
дистрибутиве воркер сразу завершается с «version `GLIBC_2.38' not found», и бэкенд
пишет в лог подсказку: собрать воркер на этой машине (`cargo build --release`) или
статически (`cargo build --release --target x86_64-unknown-linux-musl`). Для macOS
и ARM воркер нужно собрать самому. Бэкенд ищет воркер так:

1. `RUST_EXECUTABLE_PATH_UNIX` (относительно `backend/`, затем текущей папки);
2. сборку под конкретную платформу (`cargo build --release --target <triple>`):
   `app/core/target/<triple>/release/synex_worker`, где `<triple>` —
   `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`, `aarch64-apple-darwin`
   (Apple Silicon) или `x86_64-apple-darwin`;
3. сборку из исходников воркера в этом репозитории: `core/target/release/synex_worker`
   (и `core/target/<triple>/release/`) после `cargo build --release` в папке `core/`.
   На Windows так же ищется `core\target\release\synex_worker.exe`.

Из всех подходящих файлов берётся самый свежий: только что собранный в `core/` воркер
новее бинарника из репозитория, а после `git pull` с новым бинарником — наоборот. Какой
файл запущен, видно в логе (`Rust worker started`, `path`). Если путь в
`RUST_EXECUTABLE_PATH_*` изменён (не значение по умолчанию), используется только он.

Файл проверяется по заголовку: `synex_worker.exe` или сборка под другой процессор
пропускается, а в логе написано, для чего она собрана (вместо «Exec format error»).
Скрипт-обёртка (`#!/bin/sh`) тоже подходит. Право на запуск (`chmod +x`) бэкенд ставит
сам. macOS может заблокировать неподписанный бинарник, скачанный из интернета (воркер
умирает сразу с кодом -9): `xattr -d com.apple.quarantine <путь к synex_worker>`.

Воркер слушает Unix socket. По умолчанию (`RUST_IPC_SOCKET_PATH` пустой) он лежит в
личной папке пользователя, а не в общем `/tmp`: `$XDG_RUNTIME_DIR/synex/worker.sock`
(Linux, обычно `/run/user/<uid>`), без `XDG_RUNTIME_DIR` — `/tmp/synex-<uid>/worker.sock`,
на macOS — `$TMPDIR/synex/worker.sock`. Перед запуском воркера бэкенд:

- создаёт папку с правами `0700` и проверяет, что она его и не symlink; иначе берёт
  временную папку со случайным именем;
- удаляет файл сокета, который никто не слушает (остался после падения воркера):
  иначе воркер не смог бы занять путь и ушёл бы на TCP;
- проверяет длину пути: не больше 103 байт на macOS и 107 на Linux.

Чтобы воркер не оставался работать без бэкенда (на Windows это делает Job Object):

- воркер запускается в своей сессии: Ctrl+C в терминале останавливает только бэкенд,
  а тот штатно закрывает SSH-сессии и затем воркер;
- Linux: если бэкенд убит (`kill -9`, падение, IDE), ядро сразу посылает воркеру SIGTERM
  (`PR_SET_PDEATHSIG`);
- macOS такого механизма не имеет. PID воркера записывается в `worker.sock.pid` рядом
  с сокетом, и при следующем запуске бэкенд останавливает воркер, оставшийся от
  прошлого раза: только если тот бэкенд уже не работает и это тот же процесс (совпадают
  PID и время запуска, так повторно выданный PID не спутать с воркером).

### Доступ к API

Авторизации у API нет: он слушает только `127.0.0.1` и доверяет программам на этой
машине. Браузер тоже программа на этой машине, поэтому запросы от веб-страниц
ограничены. Без этого любая открытая страница могла бы через `fetch` и WebSocket
читать сессии и выполнять команды на серверах.

- Запрос с заголовком `Origin` (его шлёт браузер) проходит, только если origin — сам
  backend (`http://127.0.0.1:8000`, `http://localhost:8000`), dev-сервер UI
  (`http://localhost:5173` и `http://127.0.0.1:5173`, `npm run dev` в `frontend/`) или
  указан в `ALLOWED_ORIGINS`. Иначе 403 `forbidden_origin`, WebSocket получает отказ при
  подключении. Dev-сервер UI разрешён без настройки: открыть порт 5173 на этой машине
  может только программа на ней же, а ей API доступен и без браузера.
  `UI_DEV_SERVER=false` это выключает (например, в собранном приложении).
- Запросы без `Origin` (curl, PowerShell, настольное приложение, тесты) проходят.
- Заголовок `Host` должен быть `127.0.0.1`, `localhost`, `[::1]` или из `ALLOWED_HOSTS`.
  Иначе 403 `forbidden_host`: это защита от DNS rebinding.
- `HOST=0.0.0.0` открывает API в сеть без авторизации. При старте backend пишет
  об этом предупреждение.

## API

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/api/ssh/sessions` | Открытые SSH-сессии |
| POST | `/api/ssh/sessions` | Открыть сессию: `{host, port, username, key_id \| key_path [+ passphrase] \| password}` → 201 |
| GET | `/api/ssh/sessions/{id}` | Одна сессия: хост, `cwd`, занята ли, сколько вкладок смотрят |
| DELETE | `/api/ssh/sessions/{id}` | Закрыть сессию |
| POST | `/api/ssh/sessions/{id}/exec` | Выполнить команду: `{command, timeout?, track_cwd?, allow_interactive?}` |
| POST | `/api/ssh/sessions/{id}/interrupt` | Прервать идущую команду (Ctrl+C) |
| GET / DELETE | `/api/ssh/sessions/{id}/history` | История команд сервера сессии (`?limit=`), очистить её |
| GET | `/api/ssh/sessions/{id}/complete` | Дополнение по Tab: `?line=&cursor=` |
| GET | `/api/ssh/sessions/{id}/output` | Буфер вывода: `?since=N&limit=M` |
| WS | `/api/ssh/sessions/{id}/ws` | Терминал сессии: `?since=N` (см. ниже) |
| POST | `/api/ssh/connect` | То же, что `POST /sessions`, ответ короче |
| POST | `/api/ssh/exec` | Команда: `{command, session_id?, timeout?, track_cwd?}`, без `session_id` в последней открытой сессии |
| POST | `/api/ssh/disconnect` | Закрыть сессию: `{session_id?}` |
| GET | `/api/ssh/status` | Состояние сессии: `?session_id=` или последняя открытая |
| GET | `/api/ssh/known-hosts` | Доверенные ключи серверов: отпечатки по `host:port`, `source`: кто подтвердил (`user`, `openssh`) |
| POST | `/api/ssh/known-hosts` | Доверять ключу сервера после подтверждения пользователем: `{host, port?, fingerprint, key_type?, replace?}` → 201 |
| DELETE | `/api/ssh/known-hosts` | Забыть ключ сервера: `?host=&port=22` |
| GET | `/api/keys` | Список ключей (без секретной части) |
| POST | `/api/keys` | Импорт: `{name, private_key \| private_key_path, passphrase?, comment?}` → 201 |
| POST | `/api/keys/generate` | Новый ключ: `{name, type: ed25519 \| rsa, bits?, comment?}` → 201 |
| GET | `/api/keys/{id}` | Один ключ |
| PATCH | `/api/keys/{id}` | Переименовать или сменить комментарий: `{name?, comment?}` |
| DELETE | `/api/keys/{id}` | Удалить → 204 |
| GET | `/api/connections` | Сохранённые подключения (без паролей) |
| POST | `/api/connections` | Сохранить: `{name, host, port?, username, group?, auth, key_id \| key_path [+ passphrase] \| password?}` → 201 |
| GET | `/api/connections/{id}` | Одно подключение |
| PATCH | `/api/connections/{id}` | Изменить переданные поля (см. ниже) |
| DELETE | `/api/connections/{id}` | Удалить → 204 |
| POST | `/api/connections/{id}/connect` | Открыть SSH-сессию по подключению: `{password?, passphrase?}` → 201, описание сессии |
| GET | `/api/docker/sessions/{id}/info` | Есть ли Docker на сервере сессии, версии |
| GET | `/api/docker/sessions/{id}/containers` | Контейнеры: `?all=false` только запущенные |
| POST | `/api/docker/sessions/{id}/containers/{c}/{action}` | `start`, `stop`, `restart`, `pause`, `unpause`, `kill` (`?timeout=N` для stop/restart) |
| DELETE | `/api/docker/sessions/{id}/containers/{c}` | Удалить контейнер: `?force=true&volumes=true` |
| GET | `/api/docker/sessions/{id}/containers/{c}/logs` | Логи: `?tail=200&since=<next_since>` |
| WS | `/api/docker/sessions/{id}/containers/{c}/logs/ws` | Живой лог контейнера: `?tail=200&since=` (см. ниже) |
| GET | `/api/docker/sessions/{id}/stats` | CPU, память, сеть и диск запущенных контейнеров |
| GET | `/api/docker/sessions/{id}/images` | Образы на сервере |
| GET | `/api/git/sessions/{id}/status` | Репозиторий на сервере: `?path=~/app`, ветка, отставание, изменения |
| GET | `/api/git/sessions/{id}/log` | Коммиты: `?path=~/app&limit=20` |
| POST | `/api/git/sessions/{id}/fetch` | `git fetch`: `{path}` → состояние со свежим `behind` |
| POST | `/api/git/sessions/{id}/pull` | `git pull --ff-only`: `{path}` → пришедшие коммиты |
| POST | `/api/git/sessions/{id}/deploy` | pull и команда в папке репозитория: `{path, command?, only_if_updated?, timeout?}` |
| GET | `/api/sftp/sessions/{id}/info` | Файлы на сервере: домашняя папка, пользователь |
| GET | `/api/sftp/sessions/{id}/list` | Содержимое папки: `?path=/var/log` (без `path` домашняя) |
| GET | `/api/sftp/sessions/{id}/stat` | Сведения о файле или папке: `?path=` |
| POST | `/api/sftp/sessions/{id}/mkdir` | Создать папку: `{path, parents?}` → 201 |
| POST | `/api/sftp/sessions/{id}/move` | Переименовать или переместить: `{source, target, overwrite?}` |
| POST | `/api/sftp/sessions/{id}/copy` | Скопировать: `{source, target, overwrite?}` → 201 |
| DELETE | `/api/sftp/sessions/{id}/entry` | Удалить: `?path=&recursive=true` |
| POST | `/api/sftp/sessions/{id}/upload` | Загрузить файл телом запроса: `?path=&name=&overwrite=false` → 201 |
| GET | `/api/sftp/sessions/{id}/download` | Скачать файл телом ответа: `?path=` |
| POST | `/api/sftp/sessions/{id}/transfers` | Передать файл или папку по пути на этом компьютере, в фоне → 202 |
| GET | `/api/sftp/transfers` | Передачи: `?session_id=&active=true` |
| GET | `/api/sftp/transfers/{id}` | Состояние передачи |
| DELETE | `/api/sftp/transfers/{id}` | Отменить передачу или убрать завершённую из списка |
| GET | `/api/sftp/local/list` | Папка этого компьютера (вторая панель): `?path=` |
| GET | `/api/sftp/sessions/{id}/search` | Найти файлы и папки на сервере: `?path=&name=&contains=&type=&max_depth=&hidden=&limit=` |
| GET | `/api/sftp/local/search` | То же на этом компьютере |
| GET | `/api/monitor/metrics` | Метрики серверов сейчас (то же, что `metrics` в `/ws`) |
| GET | `/api/alerts` | Алерты всех серверов: `?status=active\|resolved\|all&server=host:port` |
| GET | `/api/alerts/{id}` | Один алерт |
| POST | `/api/alerts/{id}/ack` | Отметить алерт как увиденный; `POST /api/alerts/ack-all` для всех активных |
| DELETE | `/api/alerts/{id}` | Убрать погасший алерт из истории; `DELETE /api/alerts` очищает историю |
| GET / PUT / DELETE | `/api/alerts/rules` | Правила алертов (пороги): прочитать, заменить, вернуть по умолчанию |
| GET | `/api/logs/servers/{server}/sources` | Какие логи есть на сервере: журнал systemd (и доступ к нему), файлы в `/var/log` |
| GET | `/api/logs/servers/{server}/journal` | Записи журнала: `?unit=&priority=&grep=&since=&until=&boot=&lines=200&before=` |
| GET | `/api/logs/servers/{server}/journal/units` | Юниты, которые писали в журнал (для фильтра) |
| WS | `/api/logs/servers/{server}/journal/ws` | Живой журнал: те же фильтры, `?lines=200&after=<cursor>` |
| GET | `/api/logs/servers/{server}/files` | Файлы логов в папке: `?dir=/var/log` |
| GET | `/api/logs/servers/{server}/file` | Последние строки файла: `?path=&lines=200&grep=` |
| WS | `/api/logs/servers/{server}/file/ws` | Живой файл лога: `?path=&lines=200&grep=&offset=&inode=` |
| GET | `/api/health` | Состояние бэкенда и воркера, загруженные плагины |
| GET | `/api/plugins` | Плагины: версия, описание, зависимости, `status` и причина сбоя (см. ниже) |
| POST | `/api/plugins/{name}/enable` | Включить плагин без перезапуска (вместе с зависимостями); выбор запоминается |
| POST | `/api/plugins/{name}/disable` | Выключить плагин без перезапуска: `?cascade=true` вместе с зависящими; выбор запоминается |
| WS | `/ws` | Поток данных для UI: метрики серверов каждые 500 мс (см. ниже) |

С `DEBUG=true` Swagger доступен на `/api/docs`.

### Пример: ключ из хранилища → подключение → команда → отключение

**PowerShell** (Windows):

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

**bash** (Linux, macOS):

```bash
api=http://127.0.0.1:8000/api
key=$(curl -s -X POST "$api/keys" -H "Content-Type: application/json" \
  -d '{"name": "prod", "private_key_path": "/home/me/.ssh/id_ed25519"}' | jq -r .id)
s=$(curl -s -X POST "$api/ssh/sessions" -H "Content-Type: application/json" \
  -d "{\"host\": \"1.2.3.4\", \"username\": \"root\", \"key_id\": \"$key\"}" | jq -r .session_id)
curl -s -X POST "$api/ssh/sessions/$s/exec" -H "Content-Type: application/json" \
  -d '{"command": "cd /var/log && ls", "timeout": 10}'
curl -s "$api/ssh/sessions/$s/output"
curl -s -X DELETE "$api/ssh/sessions/$s"
```

Вариант для bash читает JSON-ответы через [jq](https://jqlang.org/).

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
  (`{action: opened | closed, session_id, host, port, username, connection_id, reason?}`).
- В описании сессии `host_key: {fingerprint, key_type}`: ключ сервера, с которым воркер
  подключился (`null` у сборок воркера без проверки ключа). Как подтверждается ключ,
  см. «Проверка ключа сервера».
- Плагины (docker, git) выполняют в сессии свои служебные команды
  (`ssh_manager.run_service`). Они не попадают в буфер вывода и терминал, не меняют
  `cwd` сессии и выполняются из домашней папки, с лимитом на сервере и точным кодом
  возврата. Если в сессии идёт команда пользователя, служебная ждёт её до
  `SSH_SERVICE_WAIT` (10 с), потом 409 `ssh_session_busy`. Для долгих команд плагина
  (деплой, логи) UI лучше открыть отдельную сессию к тому же серверу.

### Проверка ключа сервера (TOFU)

Воркер (сборки с 2026-11-14) подключается только к серверу, ключ которого подтвердил
пользователь: так подмена сервера («человек посередине») не проходит незаметно, а
пароль не уходит на чужой сервер. Отпечатки хранит backend в `known_hosts.json` рядом с
`keys.json` (`KNOWN_HOSTS_PATH`), по `host:port`.

1. Первое подключение к серверу (`POST /api/ssh/sessions`, `/api/ssh/connect` или
   `/api/connections/{id}/connect`) отвечает 409 `ssh_host_key_unknown`:

   ```json
   {"error": "ssh_host_key_unknown", "detail": "The key of 1.2.3.4:22 is not trusted yet: ...",
    "host_key": {"host": "1.2.3.4", "port": 22,
                 "fingerprint": "SHA256:TUKuyjD5oX42qzSLLzmaPDg4iVmiU6ThNtZwYmGiVek",
                 "key_type": "ssh-ed25519"}}
   ```

2. UI показывает отпечаток и тип ключа и спрашивает пользователя. Сверить отпечаток можно
   на сервере: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
3. Только после подтверждения UI сохраняет его: `POST /api/ssh/known-hosts` с `host`,
   `port`, `fingerprint` и `key_type` из `host_key`, и повторяет подключение. Дальше
   backend передаёт отпечаток воркеру при каждом подключении к этому `host:port`
   (служебные сессии, например для передачи файлов, идут с ключом своей сессии).
4. Если ключ сервера сменился, подключение отвечает 409 `ssh_host_key_mismatch`, в
   `host_key` полученный `fingerprint` и сохранённый `expected_fingerprint`. Backend сам
   подключение не повторяет и сохранённый отпечаток не меняет. UI предупреждает: это
   может быть атака или переустановленный сервер. Заменить отпечаток можно только явным
   действием пользователя: `POST /api/ssh/known-hosts` с новым `fingerprint` и
   `replace: true` (без `replace` 409 `ssh_known_host_conflict`).

`DELETE /api/ssh/known-hosts?host=&port=` забывает ключ: следующее подключение снова
спросит подтверждение. Сборки воркера без проверки ключа (до 2026-11-14) подключаются
как раньше, отпечаток им передаётся, но не проверяется.

Серверы, к которым уже подключались через `ssh`, подтверждать второй раз не нужно: их
ключи записаны в `known_hosts` OpenSSH на этом компьютере. Когда сервера в
`known_hosts.json` ещё нет, backend ищет ключ, который показал сервер, в этих файлах
(`~/.ssh/known_hosts`, `~/.ssh/known_hosts2` и общий для компьютера
`/etc/ssh/ssh_known_hosts`, на Windows `%PROGRAMDATA%\ssh\ssh_known_hosts`):

- ключ записан там для этого сервера: backend сохраняет отпечаток у себя (`source:
  "openssh"` в `GET /api/ssh/known-hosts`) и подключается, ответ обычный 201. Для сервера
  на нестандартном порту, как и у `ssh`, годится запись `[host]:port` или запись того
  же сервера без порта с тем же ключом;
- для сервера там записан другой ключ того же типа (`ssh` в этом случае пишет REMOTE
  HOST IDENTIFICATION HAS CHANGED): 409 `ssh_host_key_mismatch`, в `host_key` ещё
  `source: "openssh"`, а `expected_fingerprint` это отпечаток из файла. Подключения нет;
  пользователь может подтвердить новый ключ, как в пункте 4;
- ключ помечен там как отозванный (`@revoked`): 409 `ssh_host_key_revoked`,
  подключения нет;
- сервера там нет или записаны ключи только других типов: обычное подтверждение
  (пункт 1).

Понимаются хешированные имена (`HashKnownHosts`, по умолчанию в Debian и Ubuntu),
шаблоны `*` и `?` и отрицание `!`; строки `@cert-authority` не учитываются. Файлы
OpenSSH backend только читает. Запись в `known_hosts.json` важнее: если отпечаток
сервера там есть, файлы OpenSSH не спрашиваются. `OPENSSH_KNOWN_HOSTS=false` выключает
поиск, `OPENSSH_KNOWN_HOSTS_FILES` задаёт свои файлы через запятую.

### WebSocket терминала

`ws://127.0.0.1:8000/api/ssh/sessions/{id}/ws?since=N`. Одно подключение смотрит одну
сессию, одну сессию могут смотреть несколько вкладок. Конверт тот же, что у `/ws`:
`{"type", "ts", "data"}`.

| type | Кто и когда шлёт | data |
|---|---|---|
| `hello` | сервер, сразу после подключения | `{protocol, session, replay: {since, count, gap, first_seq, last_seq}}` |
| `output` | сервер: сначала записи буфера после `since`, потом новые | запись буфера `{seq, ts, kind, ...}` |
| `closed` | сервер, когда сессию закрыли; затем закрывает подключение | `{reason}`: `closed`, `worker_connection_lost`, `lost_in_worker`, `exec_timeout`, `shutdown` |
| `error` | сервер, если сообщение не разобрано или команда не запустилась | `{error, detail, request_id?}`; у `ssh_interactive_command` ещё `program` и `hint` |
| `interrupt` | сервер, в ответ на `interrupt` | `{status, signal, command}`, как `POST .../interrupt` |
| `pong` | сервер, в ответ на `ping` | `data` из `ping` |
| `exec` | UI | `{command, timeout?, track_cwd?, allow_interactive?, id?}`: `id` вернётся в записях как `request_id` |
| `interrupt` | UI | прервать идущую команду (Ctrl+C) |
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

Ответ: `{session_id, seq, stdout, stderr, exit_code, cwd, timed_out, interrupted,
duration_ms}`
(`seq`: номер записи с результатом в буфере вывода). То же для `/sessions/{id}/exec`.

- `timeout`: лимит выполнения в секундах (по умолчанию `SSH_EXEC_TIMEOUT=30`, максимум 3600).
  Применяется **на сервере** через coreutils `timeout`. Команда и все её дочерние процессы
  получают TERM, через 3 секунды KILL; после прерывания обёртка добивает KILL всю группу
  процессов команды, чтобы процесс, игнорирующий TERM, не держал канал (так ведёт себя
  и GNU `timeout`, и `timeout` из uutils в Ubuntu 26.04). Ответ приходит с кодом 200, `timed_out: true`,
  `exit_code` 124 (TERM) или 137 (KILL) и выводом, который успел накопиться. Это важно:
  воркер выполняет запросы по одному, и `tail -f` без лимита занял бы его навсегда.
- Команды одной сессии выполняются по одной. Следующая ждёт окончания предыдущей,
  и её таймаут начинает идти, только когда она реально отправлена воркеру.
- `cwd` сохраняется между командами: после `cd /var/log` следующая команда стартует там.
  Если директорию удалили, команда не выполняется (`exit_code` 1, `cwd` = домашняя папка).
- `track_cwd: false` отправляет команду воркеру как есть: без cwd, без таймаута на
  сервере и с `exit_code` от воркера (настоящий код с воркера 2026-11-14, у старых
  сборок всегда 0). Нужно для не-POSIX оболочек вроде fish.
- Переводы строк приводятся к `\n`: `\r\n` из Windows-текста и одиночный `\r` (Enter в
  терминале) на сервере стали бы частью команды (`cd /tmp\r` → "can't cd to /tmp").
- Ввод команды (stdin) — `/dev/null`: передать его через API нельзя, поэтому `cat`,
  `read`, `sudo` с паролем или вопрос `apt install` [Y/n] сразу получают конец ввода и
  завершаются, а не ждут до таймаута. Для `sudo` нужен NOPASSWD, для apt — `-y`.
- 504 `ssh_exec_timeout` означает, что воркер не вернул результат даже через
  `timeout + 3 + SSH_EXEC_TIMEOUT_GRACE` секунд. Например, на сервере нет coreutils
  `timeout` (macOS, старый busybox) или команда без `track_cwd`. Воркер выполняет
  запросы сессии по очереди и отменять команду не умеет, поэтому все следующие команды
  встали бы за зависшей: сессия закрывается (`ssh.session` с `reason: exec_timeout`,
  терминал получает `closed`), UI открывает новую.

### Терминал: прерывание, программы, которым нужен терминал, история, дополнение

- **Лимит времени**: команда из WebSocket терминала без своего `timeout` получает
  `TERMINAL_TIMEOUT` (600 с) вместо `SSH_EXEC_TIMEOUT`: `apt upgrade` или сборка идут
  минутами, а остановить команду раньше можно Ctrl+C.
- **Прерывание** (Ctrl+C): `POST .../interrupt` или `{"type": "interrupt"}` в WebSocket
  терминала. Воркер не умеет отменять команду, поэтому backend открывает второе
  SSH-подключение к тому же серверу, находит процессы идущей команды по её маркеру и шлёт
  им INT, как Ctrl+C; если команда через 2 с жива, TERM, потом KILL. Ответ приходит, когда
  команда остановилась: `{status, signal, command}`, `status` — `interrupted`, `idle`
  (ничего не выполнялось), `finished` (закончилась сама раньше сигнала) или `alive` (не
  помог даже KILL, например процесс другого пользователя после `sudo`). Результат самой
  команды приходит как обычно, с `interrupted: true`, `timed_out: false` и кодом 130
  (INT), 143 (TERM) или 137 (KILL). Без второго подключения (воркер его не даёт) или для
  команды с `track_cwd: false`: 409 `ssh_interrupt_unavailable`.
- **Программы, которым нужен терминал.** Команды идут без TTY, поэтому полноэкранные
  программы работать не могут: `vim` висит до таймаута, и его escape-коды ломают
  терминал в UI, `top` завершается с ошибкой. Такая команда не отправляется: 422
  `ssh_interactive_command` с `program` и `hint` (чем заменить: `top -b -n 1`, вкладка
  Files для редакторов, `git commit -m`). Редакторы, `top`/`htop`/`btop`, `watch`,
  `tmux`/`screen`, `mc`, `ncdu` и команды, которые открывают редактор (`git commit` без
  `-m`, `git rebase -i`, `crontab -e`, `systemctl edit`), проверяются в каждой части
  строки, в том числе после `sudo`, `env` и `VAR=value`. `allow_interactive: true`
  отправляет команду всё равно. `less`, `more` и `man` без терминала работают как `cat` и
  не отклоняются.
- **История**: `GET .../history?limit=` отдаёт команды сервера сессии (`user@host:port`,
  общая для всех его сессий, старые первыми); она переживает перезапуск в
  `terminal_history.json` рядом с `keys.json` (`TERMINAL_HISTORY_PATH`). Повтор подряд
  хранится один раз, команда с пробелом в начале не запоминается (как
  `HISTCONTROL=ignorespace` в bash: так вводят команды с паролями). `TERMINAL_HISTORY`
  (500) команд на сервер, `0` выключает историю. `DELETE .../history` очищает её.
- **Дополнение**: `GET .../complete?line=cat /var/lo&cursor=15` →
  `{word, start, kind, candidates, insert, common, truncated}`. Первое слово команды
  дополняется командами из `PATH`, остальные — файлами и папками от текущей папки сессии
  (у папок `/` в конце). Замените строку от `start` до курсора на `insert[i]` (пробелы и
  спецсимволы экранированы) или на `common`, если вариантов несколько. Это служебная
  команда (`compgen` из bash, без bash — шаблон shell); пока в сессии идёт команда, ответ
  409 `ssh_session_busy`.

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

### Сохранённые подключения (плагин `connections`)

Список серверов для UI: имя, адрес, пользователь, папка (`group`) и способ входа.

```json
POST /api/connections
{"name": "prod", "host": "10.0.0.1", "port": 22, "username": "deploy", "group": "Work",
 "auth": "key_id", "key_id": "3f2a..."}
```

- `auth`: `key_id` (ключ из хранилища), `key_path` (файл ключа на этой машине,
  + `passphrase`, если он зашифрован) или `password`. Пароль можно не передавать:
  тогда его спрашивают при каждом подключении.
- Файл: `connections.json` рядом с `keys.json` (`CONNECTIONS_PATH`). Пароли и фразы в
  нём зашифрованы тем же мастер-ключом, что и ключи; API их никогда не отдаёт, только
  `has_password` и `has_passphrase`.
- `PATCH` меняет только переданные поля. Новый способ входа передаётся целиком (`auth`
  и его поля); без `auth` можно сменить ключ, пароль или фразу текущего способа, а
  `"password": null` забывает сохранённый пароль.
- Имена уникальны без учёта регистра (409 `connection_exists`). Несогласованный способ
  входа (например, `auth: key_id` без `key_id` или с паролем) это 422
  `invalid_connection`.
- `POST /api/connections/{id}/connect` открывает новую SSH-сессию, как
  `POST /api/ssh/sessions`, и отвечает её описанием. Несохранённый пароль или фразу
  передайте в теле (`{"password": "..."}`), иначе 422 `connection_secret_required`.
  У сессии (и в событии `ssh.session`) есть `connection_id`: UI видит, какие сессии
  открыты из какого подключения. Время подключения сохраняется в `last_connected_at`.

### Docker на серверах (плагин `docker`)

Плагин выполняет на сервере обычный `docker` через открытую SSH-сессию: это служебные
команды сессии, в её терминал они не попадают. Все пути начинаются с
`/api/docker/sessions/{session_id}/`. Нужен Docker 20.10 или новее.

- `GET .../info` отвечает всегда 200: `status` `ok`, `not_installed` (нет docker),
  `permission_denied` (пользователь SSH не в группе docker) или `daemon_unavailable`
  (демон не запущен), текст ошибки в `detail`, версии клиента и сервера.
- `GET .../containers` (`?all=false`: только запущенные) отдаёт контейнеры:

```json
{"id": "6de6...", "name": "web", "names": ["web"], "image": "nginx:1.27",
 "command": "nginx -g 'daemon off;'", "created_at": "2026-11-06T22:42:33+07:00",
 "state": "running", "status": "Up 17 minutes (healthy)", "health": "healthy",
 "exit_code": null, "networks": ["bridge"],
 "ports": [{"container_port": "80", "protocol": "tcp", "host_ip": "0.0.0.0", "host_port": "8080"}],
 "labels": {"com.docker.compose.project": "shop"}, "compose_project": "shop",
 "compose_service": "web"}
```

- `POST .../containers/{имя или ID}/{action}`: `start`, `stop`, `restart`, `pause`,
  `unpause`, `kill`; для `stop` и `restart` можно `?timeout=N` (секунд до KILL).
  `DELETE .../containers/{имя или ID}` удаляет (`?force=true` и запущенный,
  `?volumes=true` вместе с анонимными томами). Ответ `{container, action, output}`.
  Действие, которое не подходит к состоянию (пауза остановленного, удаление
  запущенного без `force`), это 409 `docker_container_conflict`.
- `GET .../containers/{имя или ID}/logs?tail=200` отдаёт последние строки stdout и
  stderr контейнера по времени: `{container, lines: [{time, stream, text}],
  next_since}`. `since` принимает `next_since` прошлого ответа (уже показанные строки
  не повторяются), RFC3339, Unix-время или длительность (`10m`, `1h`).
- Живой лог: WebSocket `.../containers/{имя или ID}/logs/ws?tail=200`. Сервер шлёт
  `hello`, затем `logs` `{lines: [{time, stream, text}], next_since, truncated}`: сразу
  последние `tail` строк, потом новые по мере появления. После переподключения UI
  передаёт `?since=<последний next_since>` и не получает строки повторно. Сообщения в
  том же конверте `{type, ts, data}`, что у `/ws`:

  | type | data |
  |---|---|
  | `hello` | `{protocol, session_id, container, tail, interval_ms}` |
  | `logs` | `{lines, next_since, truncated}`; `truncated: true`, если строк между опросами было больше 10 000 и старые пропущены |
  | `error` | `{error, detail, fatal}`. `fatal: false`: временная помеха (сессия занята командой пользователя, демон не отвечает), поток продолжится сам. `fatal: true`: контейнера или Docker нет, подключение закрывается (код 4404 или 4403) |
  | `closed` | `{reason}`: `session_closed` (SSH-сессию закрыли, код 1000) или `shutdown` (плагин или backend останавливается, код 1001) |
  | `pong` | `data` из `ping` UI |

  Docker в Rust-ядре ещё нет, а воркер отдаёт вывод команды только целиком, поэтому
  поток собирается опросом `docker logs --since` раз в `DOCKER_LOGS_INTERVAL` (1 с).
  Когда логи будет отдавать ядро, протокол для UI не изменится.
- `GET .../stats`: один замер `docker stats` запущенных контейнеров (пара секунд):
  `cpu_percent` (100 = одно ядро), `memory_used`, `memory_limit`, `memory_percent`,
  `net_rx`, `net_tx`, `block_read`, `block_write` (байты), `pids`.
- `GET .../images`: образы (`repository` и `tag` = `null` у образов без имени,
  `size` в байтах).
- Если пользователь SSH не в группе docker, можно задать `DOCKER_COMMAND=sudo -n docker`
  (нужен sudo без пароля). Лимит одной команды `DOCKER_TIMEOUT` (60 с).
- Ошибки: 404 `docker_not_installed`, 403 `docker_permission_denied`, 503
  `docker_daemon_unavailable`, 404 `docker_container_not_found`, 409
  `docker_container_conflict`, 504 `docker_timeout`,
  502 `docker_command_failed` (текст docker в `detail`). Сессия, занятая командой
  пользователя, даёт 409 `ssh_session_busy`: для панели Docker UI лучше открыть
  отдельную сессию.

### Логи серверов (плагин `logs`)

Логи сервера, выбранного на Dashboard: журнал systemd и файлы логов. Пути начинаются с
`/api/logs/servers/{server}/`, где `{server}` — `host:port`, как `server` в `metrics` из
`/ws` и в алертах (в URL: `encodeURIComponent`). UI не думает о сессиях: backend берёт
открытую SSH-сессию к этому серверу, свободную от команды пользователя, если такая есть;
`?session_id=` выбирает конкретную. Нет открытой сессии к серверу: 404
`logs_server_not_connected`. Команды идут служебными командами сессии и в её терминал не
попадают.

- `GET .../sources`: `{journal: {available, version, access, detail}, directory, files,
  files_detail}`. `access`: `all` (виден весь журнал), `user` (только сообщения пользователя
  SSH: добавьте его в группу `systemd-journal` или `adm`), `none`. `files` — то же, что
  `GET .../files` для `/var/log`; если папку не прочитать, `files` пустой, причина в
  `files_detail`.
- `GET .../journal?lines=200`: последние записи, старые первыми:

```json
{"server": "10.0.0.5:22", "session_id": "...", "more": true,
 "before": "s=...;i=1a2", "cursor": "s=...;i=1b7",
 "entries": [{"cursor": "s=...;i=1b7", "time": 1795150389396, "priority": 3, "level": "err",
              "severity": "error", "unit": "nginx.service", "identifier": "nginx",
              "pid": 812, "hostname": "web-1", "boot_id": "...",
              "message": "upstream timed out"}]}
```

  `severity` — вкладка уровня в UI: `error` (emerg ... err), `warn` (warning), `info`
  (notice, info), `debug`.

  Фильтры: `unit` (можно несколько и шаблон: `nginx*`), `severity` (только одна вкладка, как
  кнопки ALL / INFO / WARN / ERROR экрана Logs), `priority` (`emerg` ... `debug`: этот
  уровень и важнее; не вместе с `severity`), `grep` (текст, не регулярное выражение;
  латиница без учёта регистра), `since` и `until` (Unix-время в мс или то, что понимает
  journalctl: `2026-11-20 10:00:00`, `today`, `-1h`), `boot=true` (с последней загрузки).
  Страница старше: `?before=<before прошлой страницы>`. `more: true`: могут быть записи
  старше. Фильтр, который journalctl не принял: 422 `logs_bad_filter`, его текст в `detail`.
- `GET .../files?dir=/var/log`: файлы папки и её подпапок (на один уровень): `{path, name,
  readable, size, modified, rotated}`; сжатые (`.gz`, `.xz`) и двоичные (`wtmp`, `btmp`,
  `lastlog`, файлы журнала) не показываются. `rotated`: старая копия после logrotate
  (`syslog.1`). Файлы, которые пользователь SSH не может читать, есть в списке с
  `readable: false`.
- `GET .../file?path=/var/log/syslog&lines=200`: последние строки `{lines, size, offset,
  inode, truncated}`, строка — `{text, severity}`: уровень берётся по слову уровня в начале
  строки (`ERROR`, `[warn]`, `level=info`), без него `info`. С `grep` — последние строки с
  этим текстом, с `severity` — только строки этой вкладки. `truncated: true`: строк меньше,
  чем просили, потому что читаются только последние `LOGS_CHUNK_BYTES` (1 МиБ) файла.
- Живой лог: WebSocket `.../journal/ws` (фильтры журнала, `lines`, `after`) и `.../file/ws`
  (`path`, `lines`, `grep`, `severity`, `offset`, `inode`). Сначала последние `lines` строк,
  потом новые по мере появления. Конверт сообщений тот же, что у `/ws`, `{type, ts, data}`:

  | type | data |
  |---|---|
  | `hello` | `{protocol, server, session_id, source: journal\|file, interval_ms, lines, companion}`; `companion: true`: опрос идёт в своём SSH-подключении и не ждёт команду в терминале |
  | `entries` | журнал: `{entries, cursor, truncated}`. После переподключения UI передаёт `?after=<cursor>` и не получает записи повторно |
  | `lines` | файл: `{lines, offset, inode, size, rotated, skipped, truncated}`. После переподключения `?offset=&inode=` продолжают с того же места. `rotated: true`: logrotate заменил файл, он читается с начала; `skipped`: сколько байт пропущено, потому что лог рос быстрее `LOGS_CHUNK_BYTES` за опрос. Строка, которую ещё дописывают, приходит, когда допишут |
  | `error` | `{error, detail, fatal}`. `fatal: false`: временная помеха (сессия занята, файл пропал при ротации, journalctl не ответил), опрос продолжается, та же ошибка не повторяется. `fatal: true`: журнала нет, фильтр неверный, файла нет или его не прочитать при открытии; подключение закрывается (4404, 4422, 4403) |
  | `closed` | `{reason}`: `server_disconnected` (закрыли последнюю SSH-сессию к серверу) или `shutdown` |
  | `pong` | `data` из `ping` UI |

  Воркер отдаёт вывод команды только целиком, поэтому `journalctl -f` и `tail -f` через
  него не идут: новые строки собираются опросом раз в `LOGS_INTERVAL` (2 с). Когда ядро
  научится отдавать поток, протокол для UI не изменится.
- Для системных логов пользователю SSH обычно нужна группа `adm` (файлы) или
  `systemd-journal` (журнал): `sudo usermod -aG adm,systemd-journal <user>`, затем
  переподключиться.
- Ошибки: 404 `logs_server_not_connected`, 404 `logs_journal_unavailable` (нет journalctl, не
  systemd), 422 `logs_bad_filter`, 404 `log_file_not_found`, 403
  `log_file_permission_denied`, 422 `log_file_is_directory`, 504 `logs_timeout`
  (`LOGS_TIMEOUT`, 20 с), 502 `logs_command_failed`. Сессия занята командой пользователя
  дольше `SSH_SERVICE_WAIT`: 409 `ssh_session_busy`.

### Git на серверах (плагин `git`)

Репозиторий на сервере (например, папка проекта, который потом деплоится): плагин
выполняет `git` через SSH-сессию служебными командами. Все пути начинаются с
`/api/git/sessions/{session_id}/`, папка репозитория передаётся в `path`: абсолютный
путь, `~/app` или путь от домашней папки.

- `GET .../status?path=~/app`: `head`, `branch` (`null` и `detached: true` при detached
  HEAD), `upstream`, `ahead` и `behind` (по последнему fetch), `clean`, `changes`
  (`[{path, kind: changed|renamed|conflict|untracked, index, worktree, orig_path}]`) и
  `last_commit`.
- `GET .../log?path=~/app&limit=20`: коммиты текущей ветки, новые первыми
  (`{hash, short_hash, author_name, author_email, date, subject}`).
- `POST .../fetch` `{"path": "~/app"}`: `git fetch --prune`, ответ как у `status` со
  свежими `ahead` и `behind` (сколько коммитов придёт с pull).
- `POST .../pull` `{"path": "~/app"}`: `git pull --ff-only`, только перемотка вперёд,
  без merge-коммитов. Ответ `{path, before, after, updated, commits, output}`:
  `commits` это пришедшие коммиты (новые первыми). Если обновиться нельзя, файлы не
  меняются, а ответ 409 с причиной: `git_local_changes` (на сервере правки, которые
  pull перезапишет), `git_not_fast_forward` (на сервере свои коммиты),
  `git_no_upstream`, `git_detached_head`. Недоступный remote (сеть, ключ, логин):
  502 `git_remote_unavailable`.
- `POST .../deploy` `{"path": "~/app", "command": "docker compose up -d --build",
  "only_if_updated": false, "timeout": 600}`: pull (ошибки как у `/pull`, тогда команда
  не выполняется), затем `command` в папке репозитория с лимитом `timeout` секунд.
  Ответ 200 `{path, ok, pull, command: {command, exit_code, stdout, stderr, timed_out,
  duration_ms}}`, `ok: false`, если команда упала или не уложилась в лимит. С
  `only_if_updated: true` команда выполняется, только если pull принёс коммиты. Деплой
  занимает сессию до конца команды: для него UI лучше открыть отдельную сессию.
- Сообщения git на английском (`LC_ALL=C`), вопросы логина и пароля отключены
  (`GIT_TERMINAL_PROMPT=0`): приватный remote должен открываться ключом сервера.
- Ошибки: 404 `git_not_installed`, `git_path_not_found`, `git_not_a_repository`, 504
  `git_timeout` (`GIT_TIMEOUT`, 120 с), 502 `git_command_failed` (текст git в `detail`).

### Файлы на серверах (плагин `sftp`)

Файловый менеджер: файлы и папки сервера, файлы этого компьютера (вторая панель) и
передача между ними с прогрессом. Работает через SFTP Rust-воркера (действия `sftp_*`;
сборки 2026-11-12 и 2026-11-14). Пути сервера начинаются с `/api/sftp/sessions/{session_id}/`.

- Путь на сервере: абсолютный (`/var/log`), от домашней папки (`docs/a.txt`) или
  `~/docs`. В ответах пути всегда абсолютные, их и передавайте дальше.
- `GET .../info`: `{engine: "core", home, user, checksum}`; `checksum`: можно ли сверить
  SHA-256 на сервере (`null`, пока не проверяли).
- `GET .../list?path=/var/log` (без `path` домашняя папка):

```json
{"path": "/var/log", "parent": "/var", "home": "/home/deploy", "entries": [
  {"name": "nginx", "path": "/var/log/nginx", "type": "dir", "size": 4096,
   "is_dir": true, "hidden": false, "permissions": "drwxr-xr-x", "mode": 493,
   "target_type": null, "modified_at": "2026-11-14T09:30:12Z", "link_target": null,
   "uid": 0, "gid": 0},
  {"name": "syslog", "path": "/var/log/syslog", "type": "file", "size": 182734, "...": "..."}
]}
```

  Сначала папки, внутри по имени. `type`: `file`, `dir`, `link`, `other`; у ссылки на
  папку `target_type: "dir"` и `is_dir: true` (открывается по клику), в `link_target`
  путь, записанный в ссылке. `modified_at`, `uid`, `gid` и `link_target` у файлов сервера
  отдаёт воркер с 2026-11-14; со старой сборкой они `null`.
- `GET .../stat?path=`, `POST .../mkdir` `{path, parents?}` → 201, `POST .../move` и
  `POST .../copy` `{source, target, overwrite?}` (`target` это новый полный путь; папка
  копируется целиком), `DELETE .../entry?path=&recursive=true`. Ответ: сведения о файле
  (как элемент `entries`). Корень и домашнюю папку удалить или переместить нельзя (403
  `sftp_protected_path`); непустая папка удаляется только с `recursive=true`.
- Передача файла или папки. Два способа, оба видны в `/api/sftp/transfers` и в `/ws`:
  - по пути на этом компьютере, в фоне: `POST .../transfers` `{direction: upload |
    download, local_path, remote_path, overwrite?}` → 202 и описание передачи. Папка
    (или путь с разделителем в конце) в «куда» значит «положить под тем же именем».
    Передачи одной сессии идут по очереди. Так передаются и папки целиком (ниже);
  - телом запроса: `POST .../upload?path=<куда>&name=<имя, если path это папка>`
    (тело это содержимое файла, ответ 201 после записи и сверки) и
    `GET .../download?path=` (тело ответа это файл, id передачи в заголовке
    `X-Synex-Transfer-Id`).
- `GET /api/sftp/transfers?session_id=&active=true`, `GET /api/sftp/transfers/{id}`,
  `DELETE /api/sftp/transfers/{id}`: идущую передачу отменяет (недописанный файл
  удаляется), завершённую убирает из списка.
- Состояние передачи (ответы API и событие `sftp.transfer` в `/ws`):

```json
{"id": "5f0c2a9e41b7", "session_id": "3f9c0a7b12d4e5f6", "kind": "file",
 "direction": "upload", "source": "path", "name": "backup.tar.gz",
 "remote_path": "/srv/backup.tar.gz", "local_path": "C:\\Users\\me\\backup.tar.gz",
 "status": "running", "bytes_done": 41943040, "bytes_total": 104857600, "percent": 40.0,
 "speed": 13981013, "eta_seconds": 4, "files_total": null, "files_done": 0,
 "current": null, "skipped": [], "skipped_count": 0, "error": null, "detail": null,
 "checksum": null, "verified": null, "created_at": "...", "started_at": "...",
 "finished_at": null}
```

  `status`: `queued` → `running` → `verifying` → `completed`, либо `failed` (`error` и
  `detail`), либо `cancelled`; у папки после `queued` идёт `scanning`. Событие приходит
  при каждой смене статуса и не чаще раза в `SFTP_PROGRESS_INTERVAL` (0,25 с), пока идут
  данные. `speed` в байтах в секунду.
- Файл передаёт воркер в служебной SSH-сессии к тому же серверу (своё IPC-подключение),
  поэтому терминал, метрики, docker и git этой сессии во время передачи не ждут.
  Прогресс: при скачивании backend смотрит, как растёт временный файл на этом
  компьютере, при загрузке спрашивает размер временного файла на сервере. Если
  служебную сессию открыть нельзя (воркер не принимает дополнительные подключения),
  передача идёт в основной сессии, а прогресс загрузки появляется только в конце.
- Файл не появляется на месте наполовину: данные пишутся во временный файл
  (`.synex-<id>.part/` рядом с целевым на сервере, `<имя>.synex-part` на компьютере),
  после передачи сверяются размер и SHA-256 (`checksum`, `verified: true`; если на
  сервере нет `sha256sum`, `shasum` и `openssl`, сверяется только размер), и только
  потом временный файл заменяет целевой. При ошибке или отмене он удаляется.
- Заменить файл (`overwrite: true`): старый сначала уходит во временную папку и
  удаляется, только когда новый уже на месте. Если поставить новый не вышло, старый
  возвращается.
- Отмена идущей передачи сразу отвечает `cancelled`, закрывает её служебную сессию и
  убирает временные файлы; следующая передача сессии начинается сразу. Прерывать
  передачу воркер не умеет: он дочитывает файл до конца в фоне, результат никуда не
  попадает. Если отмена пришла в самом начале файла, воркер создаёт его уже после
  уборки: такой файл backend убирает следом (проверяет временные пути ещё 15 секунд).
- Папка целиком: тот же `POST .../transfers`, в `local_path` (загрузка) или в
  `remote_path` (скачивание) папка. Это одна передача с `kind: "dir"`:

```json
{"id": "9b1d77c0a2e4", "kind": "dir", "direction": "upload", "name": "site",
 "remote_path": "/srv/site", "local_path": "C:\\work\\site", "status": "running",
 "bytes_done": 7340032, "bytes_total": 52428800, "percent": 14.0,
 "files_total": 318, "files_done": 41, "current": "img/photos/2026/cover.jpg",
 "skipped": [{"path": "node_modules/.bin/tsc", "reason": "Links are not transferred"}],
 "skipped_count": 1, "verified": null, "...": "..."}
```

  - Воркер передаёт только файлы, поэтому папку backend обходит сам и передаёт по
    одному файлу, все в одной служебной сессии (терминал свободен). Сначала обход
    (`scanning`, `bytes_total` и `files_total` пока `null`), потом файлы: `files_done`
    из `files_total`, `current` это файл, который идёт сейчас (путь от корня папки).
  - Куда: существующая папка в «куда» значит «в неё под тем же именем» (`site` в
    `/srv` → `/srv/site`); путь, которого ещё нет, это имя новой папки.
  - Новая папка появляется на месте только готовой: она собирается во временной
    (`.synex-<id>.part/` рядом на сервере, `<имя>.<id>.synex-part` на компьютере) и
    встаёт на место переименованием. При ошибке и отмене временная папка удаляется.
  - Папка уже есть: 409 `sftp_target_exists` (на компьютере `sftp_local_exists`). С
    `overwrite: true` папки объединяются: файлы с теми же именами заменяются (каждый
    идёт под временным именем и встаёт на место после проверки), остальное в папке не
    трогается. При ошибке и отмене уже переданные файлы остаются.
  - Проверка: SHA-256 каждого файла, одной командой на сервере на пачку файлов (без
    `sha256sum`, `shasum` и `openssl` сверяется размер; `verified: true`, если SHA-256
    сверен у всех). Файл, который не сошёлся, передаётся ещё раз: обычно его меняли во
    время передачи (лог, в который пишут). Не сошёлся снова: пропускается.
  - Что не передаётся, попадает в `skipped` (первые 50 с причиной, полный счёт в
    `skipped_count`), а передача идёт дальше и заканчивается `completed`: ссылки (по
    ним backend не ходит: ссылка может вести наружу или по кругу), сокеты и устройства,
    то, на что нет прав, файл на месте папки и папка на месте файла при объединении,
    имена, которые не годятся этому компьютеру (на Windows `a:b.txt`, `con`, а ещё
    `Readme` рядом с `readme`). Передачу целиком останавливают обрыв связи, отказ
    сервера без причины (например, кончилось место) и отмена.
  - Права и время изменения файлов не переносятся. В папке не больше
    `SFTP_FOLDER_LIMIT` файлов и папок (100 000), иначе `sftp_folder_too_large`. Телом
    запроса папка не передаётся (`GET .../download` папки: 409 `sftp_is_a_directory`).
- `GET /api/sftp/local/list?path=C:\Users\me` (без `path` домашняя папка): папка этого
  компьютера для второй панели: `{path, parent, home, roots, separator, entries}`,
  `roots` это диски на Windows и `["/"]` на Linux и macOS. UI работает в окне браузера и
  диск не видит, поэтому папки ему показывает backend.
- Поиск: `GET .../search` (сервер) и `GET /api/sftp/local/search` (этот компьютер) ищут в
  папке и её подпапках (`path`, без него домашняя папка). Фильтр открытой папки по имени
  делает UI: список у него уже есть. Параметры: `name` (текст в имени без учёта регистра
  или шаблон с `*?[`: `*.log`), `contains` (текст внутри файлов; двоичные и файлы больше
  `SFTP_SEARCH_MAX_FILE`, 10 МБ, пропускаются), `type` (`any`, `file`, `dir`), `max_depth`
  (10), `hidden=true` (и внутри `.git`, `.cache` и других скрытых папок), `limit` (до
  `SFTP_SEARCH_LIMIT`, 1000). Нужен хотя бы `name` или `contains`, иначе 422
  `sftp_bad_search`. Ответ `{path, entries: [{name, path, parent, type, is_dir, size,
  modified_at}], truncated, timed_out, denied, elapsed_ms}`, ближние уровни первыми;
  `parent` открывает папку найденного. На сервере это одна команда `find` в своём
  SSH-подключении (терминал её не ждёт), `/proc`, `/sys`, `/dev` и `/run` пропускаются;
  без GNU find (BSD, busybox) приходят только пути (`type` и `size` равны `null`). Через
  `SFTP_SEARCH_TIMEOUT` (30 с) поиск останавливается и отдаёт найденное с
  `timed_out: true`; `denied` — сколько папок не открылось.
- Ошибки: 404 `sftp_not_found`, 403 `sftp_permission_denied`, `sftp_protected_path`, 409
  `sftp_target_exists`, `sftp_is_a_directory`, `sftp_not_a_directory`, `sftp_not_a_file`,
  `sftp_directory_not_empty`, 422 `sftp_bad_search`, 422 `sftp_bad_path` (в том числе имя
  файла с сервера, которое нельзя дать файлу на этом компьютере, например `a:b.txt` на
  Windows), 502 `sftp_failed` (текст воркера в `detail`), `sftp_unavailable` (на сервере
  нет SFTP: в его `sshd_config` нужна строка `Subsystem sftp ...`; или сборка воркера
  старая и не знает действий `sftp_*`, тогда воркер нужно обновить),
  `sftp_transfer_corrupted`, 504
  `sftp_timeout`, у передачи папки ещё 422 `sftp_folder_too_large`; для этого компьютера
  `sftp_local_not_found` (404), `sftp_local_permission_denied` (403), `sftp_local_exists` и
  `sftp_local_not_a_directory` (409), `sftp_local_error` (422). Сессия, занятая командой
  пользователя дольше `SSH_SERVICE_WAIT`: 409 `ssh_session_busy`. Настройки `SFTP_*`
  описаны в `.env.example`.

### WebSocket /ws

Поток данных для UI: `ws://127.0.0.1:8000/ws` (без префикса `/api`). В обе стороны
ходит JSON в текстовых фреймах, каждое сообщение в конверте `{"type", "ts", "data"}`,
`ts` в миллисекундах Unix.

| type | Кто и когда шлёт | data |
|---|---|---|
| `hello` | сервер, сразу после подключения | `{app, version, protocol, streams}` |
| `metrics` | сервер, каждые 500 мс | метрики серверов, пример ниже |
| `ssh.session` | сервер, при открытии и закрытии SSH-сессии | `{action: opened\|closed, session_id, host, port, username, connection_id, reason?}` |
| `worker.status` | сервер, когда меняется состояние воркера | `{status, ...}`: `connected`, `connection_lost`, `worker_exited`, `worker_started` |
| `sftp.transfer` | сервер, пока идёт передача файла | состояние передачи: `{id, status, bytes_done, bytes_total, percent, speed, ...}` (см. «Файлы на серверах») |
| `plugin.status` | сервер, когда плагин включили, выключили или он упал | `{name, status, version, error}` (см. «Плагины на ходу») |
| `alert` | сервер, когда алерт загорелся, сменил уровень, погас или его отметили | `{action: fired\|updated\|resolved\|acknowledged, alert}` (см. «Алерты») |
| `ping` | UI | что угодно, вернётся в `pong` |
| `pong` | сервер, в ответ на `ping` | `data` из `ping` |
| `error` | сервер, если не разобрал сообщение UI | `{error, detail}`: `bad_message` или `unknown_type` |

```json
{"type": "metrics", "ts": 1793779200000, "data": {
  "source": "ssh", "interval_ms": 500, "window_s": 30.0,
  "servers": [{
    "server": "178.250.187.252:22", "host": "178.250.187.252", "port": 22,
    "session_ids": ["3f9c0a7b12d4e5f6", "8a1b2c3d4e5f6a7b"],
    "status": "ok", "busy": false, "updated_at": 1793779199760, "age_ms": 240,
    "cpu": {"percent": 4.48, "avg": 3.9, "min": 2.05, "max": 7.65},
    "memory": {"total": 2062749696, "used": 1272385536, "available": 790364160,
               "percent": 61.71, "avg": 61.2, "min": 59.89, "max": 61.85},
    "network": {
      "rx": {"total": 123456789, "speed": 15234.5, "avg": 12100.0, "min": 0.0, "max": 90211.4},
      "tx": {"total": 98765432, "speed": 2048.0, "avg": 1800.25, "min": 0.0, "max": 5120.0}},
    "disk": {"total": 41152736256, "used": 21070249984, "available": 18906300416,
             "percent": 52.71},
    "disks": [{"mount": "/", "filesystem": "/dev/vda1", "total": 41152736256,
               "used": 21070200832, "available": 18906349568, "percent": 52.71}],
    "gpus": [],
    "hardware_updated_at": 1762329600123,
    "error": null
  }]
}}
```

`ssh.session` приходит и тогда, когда сессию закрыл другой клиент или она потеряна:
`reason` — `closed` (закрыли через API), `worker_connection_lost` (воркер упал или
оборвалось его подключение), `lost_in_worker`, `exec_timeout` (команда не ответила и
после лимита, см. 504 ниже), `shutdown` (бэкенд останавливается).
UI обновляет по нему список сессий, даже если терминал сессии не открыт.
`worker.status` показывает состояние ядра: например, после `worker_exited` бэкенд
перезапускает воркер (`worker_started` с `restarts`), затем `connected`.

#### Мониторинг (плагин `monitor`)

- `servers`: один элемент на сервер (`host:port`) с открытыми SSH-сессиями, сколько бы
  сессий к нему ни было. Метрики машины от сессии не зависят, поэтому сервер
  опрашивается один раз за период (`MONITOR_POLL_INTERVAL`, 0.5 с) действием воркера
  `monitor_get_values` через одну из своих сессий. Разные серверы опрашиваются
  параллельно.
- `cpu.percent`: загрузка с прошлого замера; `memory`: байты и проценты. `avg`, `min`,
  `max` в процентах за окно `window_s` (`MONITOR_WINDOW`, 30 с).
- `network`: сеть сервера, сумма по всем интерфейсам, кроме `lo`. `rx` приём, `tx`
  отправка. `total`: счётчик байт с загрузки сервера; `speed`: байт в секунду с прошлого
  замера (`null`, пока замер один); `avg`, `min`, `max`: `speed` за окно. Мегабиты в
  секунду: `speed * 8 / 1e6`. На сервере с Docker трафик контейнеров считается дважды
  (мост `docker0` и `veth*`). `network: null`: у сервера только `lo` или сборка воркера
  старше 2026-11-17.
- `disk`: корневой раздел `/` (байты, `percent` как в `df`), одно значение для карточки
  «Диск». Приходит с каждым замером, вместе с CPU и памятью. Со сборкой воркера старше
  2026-11-17 берётся из `disks` и обновляется вместе с ними.
- `disks` и `gpus` обновляются реже, раз в `MONITOR_HARDWARE_INTERVAL` (10 с): воркер их
  не отдаёт, бэкенд выполняет на сервере `df -kP` и `nvidia-smi` через ту же свободную
  сессию (в терминал сессии это не попадает). `disks`: реальные устройства `/dev/...`
  (без loop-образов snap) и корень `/` (он идёт первым), одно устройство один раз;
  `percent` как в `df`. `gpus`: только NVIDIA, значения, которые карта не сообщает,
  приходят как `null`; `[]` значит карт нет. До первого замера оба поля `null`, время
  замера в `hardware_updated_at`.
- `status`:
  - `waiting`: замеров ещё нет;
  - `warming_up`: память уже есть, CPU появится со второго замера. Первый замер в
    сессии воркер считает от загрузки сервера, он отбрасывается;
  - `ok`: свежие данные;
  - `stale`: свежих данных нет дольше `MONITOR_STALE_AFTER` (3 с), значения последние
    известные;
  - `error`: последний опрос не удался, текст в `error` (например, на сервере нет
    `/proc`: метрики работают только на Linux).
- Опрос не ждёт команды пользователя: воркер выполняет запросы одной сессии по очереди,
  поэтому сервер опрашивается через свободную сессию. Если все сессии сервера заняты
  командами, в сообщении `busy: true`, а через 3 с статус становится `stale`. Откройте
  к серверу ещё одну сессию, чтобы метрики шли и во время долгих команд.
- Серверы опрашиваются, только пока метрики кому-то нужны: к `/ws` подключён UI или в
  последние `MONITOR_DEMAND_TIMEOUT` (10) секунд был `GET /api/monitor/metrics`.
- `WS_FAKE_METRICS=true` (по умолчанию `false`) отправляет в `/ws` тестовые метрики в
  том же формате (`source: "fake"`, один сервер `demo.synex.local:22`), чтобы делать UI
  без SSH-сервера. Настоящие метрики тогда доступны только через REST.
- У каждого клиента своя очередь на 64 сообщения. Если UI не успевает читать, теряются
  самые старые сообщения, а память не растёт.
- При остановке backend закрывает соединение с кодом 1012: UI стоит переподключиться.

#### Алерты (плагин `alerts`)

Алерты всех серверов с открытыми SSH-сессиями, по тем же метрикам, что видит UI. Правила
общие для всех серверов:

| metric | по умолчанию | что проверяется |
|---|---|---|
| `cpu` | warning 90 %, critical 95 %, 60 с | загрузка CPU |
| `memory` | warning 90 %, critical 95 %, 60 с | занятость памяти |
| `disk` | warning 90 %, critical 95 %, сразу | каждый раздел из `disks` отдельно (`target` — точка монтирования) |
| `gpu_temperature` | warning 80 °C, critical 90 °C, 60 с | каждая карта NVIDIA (`target` — её номер) |
| `server` | 30 с | сервер не отвечает: опрос с ошибкой или нет свежих данных, хотя его сессии свободны |

- Значение должно держаться выше порога `duration_s` секунд: короткий всплеск CPU алерта
  не даёт. Алерт гаснет, когда значение опустилось на 5 пунктов ниже порога warning
  (иначе значение около порога мигало бы); critical сменяется на warning на 5 пунктов ниже
  порога critical. Пока сервер не отвечает, его остальные метрики не проверяются
  (значения в них старые).
- `GET /api/alerts`: `{counts: {active, critical, warning, unacknowledged, resolved},
  alerts: [...]}`, сначала активные (critical выше), потом погасшие, новые выше. Алерт:
  `{id, server, host, port, metric, target, severity, status, value, peak, threshold, title,
  message, started_at, updated_at, resolved_at, resolved_reason, acknowledged}`; `title` —
  заголовок карточки (`CPU load above 95%`, `Disk /data above 90%`,
  `Server does not respond`), `message` — подробности под ним; `resolved_reason`:
  `back_to_normal`, `server_disconnected` (закрыли последнюю сессию к серверу),
  `rule_changed` (его правило выключили).
- В `/ws` приходит `alert` `{action, alert}`, когда алерт загорелся, сменил уровень, погас
  или его отметили. UI берёт список из `GET /api/alerts` и обновляет его по этим событиям.
- `POST /api/alerts/{id}/ack` отмечает алерт увиденным (для значка); при смене уровня
  отметка снимается. Погасшие алерты хранятся в памяти (`ALERTS_HISTORY`, 200) до
  перезапуска.
- `PUT /api/alerts/rules` `{"rules": [{"metric": "cpu", "warning": 80, "critical": 95,
  "duration_s": 30, "enabled": true}]}` заменяет правила (метрика, которой нет в списке,
  остаётся по умолчанию) и сохраняет их в `alerts.json` рядом с `keys.json`
  (`ALERTS_PATH`); `DELETE /api/alerts/rules` возвращает правила по умолчанию.
- Монитор опрашивает серверы, только пока метрики кто-то смотрит. Чтобы алерты работали и
  при закрытом Dashboard, плагин держит опрос включённым (`ALERTS_KEEP_POLLING=true`).

Быстрая проверка: открыть http://127.0.0.1:8000/api/health и в консоли браузера выполнить

```js
const ws = new WebSocket("ws://127.0.0.1:8000/ws");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

### Ошибки

Все ошибки приходят как `{"error": "<код>", "detail": "<текст>"}` (у некоторых есть
поля с подробностями, например `host_key`), ошибки валидации как 422:

| HTTP | error | Когда |
|---|---|---|
| 404 | `key_not_found` | Нет ключа с таким id |
| 404 | `connection_not_found` | Нет сохранённого подключения с таким id |
| 409 | `connection_exists` | Подключение с таким именем уже есть |
| 422 | `invalid_connection` | Способ входа подключения не согласован |
| 422 | `connection_secret_required` | Пароль подключения не сохранён: передайте его в `/connect` |
| 500 | `connections_corrupted` | `connections.json` повреждён |
| 404 | `ssh_session_not_found` | Нет сессии с таким `session_id` |
| 409 | `ssh_not_connected` | Нет открытой сессии, или воркер её уже не знает |
| 403 | `forbidden_origin` / `forbidden_host` | Запрос со страницы не из `ALLOWED_ORIGINS` или с чужим `Host` |
| 409 | `key_exists` | Имя занято или этот ключ уже сохранён |
| 409 | `ssh_session_busy` | Команда плагина не дождалась, пока сессия освободится (`SSH_SERVICE_WAIT`) |
| 422 | `ssh_interactive_command` | Программе нужен терминал (vim, top, tmux): `program` и `hint`; `allow_interactive: true` отправит всё равно |
| 409 | `ssh_interrupt_unavailable` | Команду не прервать: воркер не даёт второе подключение, или `track_cwd: false` |
| 429 | `ssh_too_many_sessions` | Открыто `SSH_MAX_SESSIONS` сессий |
| 422 | `invalid_key` / — | Ключ не читается, нужна парольная фраза, неверные поля |
| 500 | `keystore_corrupted` | `keys.json` повреждён или зашифрован другим мастер-ключом |
| 502 | `ssh_connect_failed` | Воркер не смог подключиться (хост, ключ, пароль, фраза) |
| 409 | `ssh_host_key_unknown` | Ключ сервера ещё не подтверждён: отпечаток в `host_key` (см. «Проверка ключа сервера») |
| 409 | `ssh_host_key_mismatch` | Ключ сервера сменился: `host_key.fingerprint` и `expected_fingerprint`, подключения нет |
| 409 | `ssh_host_key_revoked` | Ключ сервера отозван (`@revoked`) в `known_hosts` OpenSSH на этом компьютере, подключения нет |
| 409 | `ssh_known_host_conflict` | Для сервера сохранён другой отпечаток: заменить только с `replace: true` |
| 404 | `ssh_known_host_not_found` | Для сервера нет сохранённого ключа |
| 500 | `known_hosts_corrupted` | `known_hosts.json` повреждён |
| 504 | `ssh_connect_timeout` | Сервер не ответил за `SSH_CONNECT_TIMEOUT` (20 с): адрес, порт, файрвол |
| 502 | `worker_command_failed` | Воркер вернул ошибку |
| 503 | `plugin_unavailable` | Плагин включён, но не загрузился (причина в `/api/plugins`) |
| 404 | `plugin_disabled` | Плагин выключен: его эндпоинты закрыты до `POST /api/plugins/{name}/enable` |
| 404 | `plugin_not_found` | Такого плагина нет или он не импортируется |
| 409 | `plugin_in_use` | Выключить нельзя: от плагина зависят включённые (`?cascade=true` выключит и их) |
| 503 | `worker_unavailable` | Воркер не запущен или IPC недоступен |
| 503 | `keystore_unavailable` | Нет доступа к Credential Manager / неверный `KEYSTORE_MASTER_KEY` |
| 504 | `ssh_exec_timeout` | Воркер не вернул результат команды |
| 504 | `worker_timeout` | Воркер не ответил на другой запрос |
| 403/404/502/503/504 | `docker_*` | Ошибки Docker на сервере (см. «Docker на серверах») |
| 404/409/502/504 | `git_*` | Ошибки git на сервере (см. «Git на серверах») |
| 404/409/500 | `alert_*` | `alert_not_found`, `alert_active` (активный алерт нельзя удалить), `alert_rules_corrupted` (`alerts.json` повреждён) |
| 403/404/422/502/504 | `logs_*`, `log_file_*` | Ошибки логов сервера (см. «Логи серверов») |
| 403/404/409/422/5xx | `sftp_*` | Ошибки файлов на сервере и на этом компьютере (см. «Файлы на серверах») |

## Структура

```
app/
  main.py              точка входа (uvicorn)
  core/                платформа: config, app factory, ошибки, шина событий, логи,
                       files (атомарная запись файлов с секретами)
    target/release/    synex_worker.exe (Windows), synex_worker (Linux x86_64)
  worker/              Rust-ядро: client.py (IPC-протокол), manager.py (процесс),
                       executable.py (поиск бинарника для ОС и процессора),
                       unix_socket.py (путь и папка сокета на Linux/macOS),
                       process.py (процесс воркера на Linux/macOS)
  keystore/            хранилище ключей: store.py, ssh_keys.py, master_key.py
  api/                 корневой роутер /api, /api/health, зависимости
  ws/                  WebSocket /ws: hub.py (протокол, рассылка), metrics.py (формат
                       метрик и тестовый источник)
  plugins/             менеджер плагинов (manager), выбор пользователя между
                       запусками (choices)
    ssh/               /api/ssh/*: manager (менеджер сессий), session (сессия и её
                       подключение к воркеру), buffer (буфер вывода), ws (терминал),
                       wrapper (cwd, таймаут), known_hosts (доверенные ключи серверов),
                       openssh_hosts (ключи из known_hosts OpenSSH), interrupt (Ctrl+C
                       через второе подключение), terminal (программы, которым нужен
                       терминал, история, дополнение), routes, schemas, errors
    monitor/           /api/monitor/*: service (опрос и публикация), aggregator
                       (окно, статусы), values (ответ воркера), hardware (диски, GPU)
    logs/              /api/logs/*: journal (команды journalctl и разбор JSON), files
                       (скрипты файлов логов), service (выбор сессии, ошибки), stream
                       (WebSocket живого лога), routes
    alerts/            /api/alerts/*: rules (пороги, alerts.json), engine (когда алерт
                       загорается и гаснет), service (история, события в /ws), routes
    keys/              /api/keys/*
    connections/       /api/connections/*: store (connections.json), routes, schemas
    docker/            /api/docker/*: commands (команды docker и разбор вывода),
                       service (выполнение в сессии, ошибки), logs (живой лог по
                       WebSocket), routes, schemas
    git/               /api/git/*: commands (команды git и разбор вывода), service,
                       routes, schemas
    sftp/              /api/sftp/*: remote (файлы сервера через SFTP воркера), transfers
                       (передачи, прогресс, сверка), folders (папка целиком), tree (обход
                       папки), local (файлы этого компьютера), search (find на сервере,
                       обход папок здесь), paths, routes, schemas
tests/                 pytest; fake_worker.py имитирует synex_worker
  integration/         полный цикл UI → FastAPI → воркер → SSH-сервер
```

Новый функционал добавляется плагином: `app/plugins/<name>/plugin.py` с подклассом
`BasePlugin` и `router`. По умолчанию (`PLUGINS_ENABLED=*`) включены все установленные
плагины; список через запятую включает только перечисленные, остальные видны в
`/api/plugins` со статусом `disabled`.

* `requires = ("ssh",)`: плагины, без которых этот не работает. Они включаются сами
  (даже если их нет в `PLUGINS_ENABLED`) и загружаются раньше.
* Плагин пропускается с предупреждением в логе, если он не установлен, не
  импортируется или его зависимость недоступна; остальные работают.
* Если `on_load` плагина упал, он и зависящие от него плагины не загружаются, а
  backend продолжает работать без них. Их эндпоинты (и WebSocket) отвечают 503
  `plugin_unavailable`.

`GET /api/plugins` показывает UI, что работает:

```json
[
  {"name": "ssh", "version": "0.6.0", "description": "SSH-сессии и терминал через Rust-воркер",
   "requires": [], "status": "loaded", "error": null},
  {"name": "monitor", "version": "0.2.0", "description": "Метрики серверов: CPU, память, сеть, диски, видеокарты",
   "requires": ["ssh"], "status": "loaded", "error": null},
  {"name": "sftp", "version": null, "description": "", "requires": [],
   "status": "skipped", "error": "not installed"}
]
```

`status`: `loaded` работает; `failed` включён, но `on_load` упал или не загрузилась
зависимость; `skipped` не установлен, не импортируется или нет зависимости;
`disabled` установлен, но выключен (не в `PLUGINS_ENABLED` или через `/disable`, в том
числе в прошлый запуск);
`registered` ещё не загружен (приложение стартует).

### Плагины на ходу (шина данных)

Модули включаются и выключаются на работающем backend, без перезапуска:

**PowerShell** (Windows):

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/plugins/docker/disable
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/plugins/docker/enable
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/plugins/ssh/disable?cascade=true"
```

**bash** (Linux, macOS):

```bash
curl -X POST http://127.0.0.1:8000/api/plugins/docker/disable
curl -X POST http://127.0.0.1:8000/api/plugins/docker/enable
curl -X POST "http://127.0.0.1:8000/api/plugins/ssh/disable?cascade=true"
```

Ответ: `{"changed": ["docker"], "plugins": [...]}` (`changed`: что включилось или
выключилось сейчас, `plugins`: как в `GET /api/plugins`).

- Включение вызывает `on_load` плагина и его зависимостей и открывает его эндпоинты.
  Подхватывается и пакет, который появился в `app/plugins/` после старта backend.
  Если `on_load` упал, ответ 503 `plugin_unavailable` с причиной, остальные работают.
- Выключение вызывает `on_unload`: плагин останавливает свою работу (`ssh` закрывает
  все сессии, `sftp` отменяет передачи, `docker` закрывает живые логи, `monitor`
  перестаёт опрашивать серверы). Его эндпоинты (HTTP и WebSocket) после этого отвечают
  404 `plugin_disabled` и пропадают из `/api/docs`.
- Если от плагина зависят включённые (от `ssh` зависят все модули серверов), ответ 409
  `plugin_in_use`; `?cascade=true` выключает сначала их, потом его. Включение плагина
  включает и то, от чего он зависит.
- Каждая смена состояния уходит в шину событием `plugin.status`, хаб пересылает его в
  `/ws`: `{"name": "docker", "status": "disabled", "version": "0.2.0", "error": null}`.
  События, которые плагин объявил в `BasePlugin.events` (`ssh.session`,
  `sftp.transfer`), идут в `/ws`, пока плагин включён.
- `on_load` и `on_unload` плагина должны выдерживать повторный вызов: плагин можно
  выключать и включать сколько угодно раз.

Набор плагинов переживает перезапуск backend. Что пользователь включил и выключил на
ходу, записывается в `plugins.json` рядом с `keys.json` (`PLUGINS_STATE_PATH`) и при
следующем запуске накладывается на `PLUGINS_ENABLED`:

```json
{"version": 1, "plugins": {"docker": false, "extra": true}}
```

- В файле только отличия от настроек: `false` у плагина, который есть в
  `PLUGINS_ENABLED`, но выключен пользователем; `true` у плагина, которого там нет, но
  пользователь его включил. Вернул как в настройках: запись пропадает. Поэтому
  изменение `PLUGINS_ENABLED` действует на всё, что пользователь сам не переключал.
- Плагин, который нужен включённому, включается как зависимость, даже если раньше его
  выключали. Плагин, который пользователь включил, а он не запустился, после перезапуска
  пробует запуститься снова (и снова показывает причину в `/api/plugins`).
- Файла нет или он испорчен: работает набор из `PLUGINS_ENABLED`, о причине строка в
  логе. Не получилось записать файл: выбор действует до перезапуска.
- `PLUGINS_REMEMBER=false` выключает запоминание: после перезапуска включено ровно то,
  что в `PLUGINS_ENABLED`. Вернуть набор из настроек можно и удалив `plugins.json`.

## Тесты

**PowerShell** (Windows):

```powershell
uv run pytest
```

**bash** (Linux, macOS):

```bash
uv run pytest
```

Тесты используют временное хранилище и свой мастер-ключ: настоящий `keys.json` и
Credential Manager они не трогают. От `backend/.env` они тоже не зависят: набор плагинов
и источник метрик тесты задают сами.

Тесты идут на Windows, Linux и macOS. Команды SSH фейковый воркер на Linux и macOS
выполняет настоящим `sh -c`, а на Windows — эмулятором shell (`tests/fake_shell.py`):
виртуальная файловая система с `/tmp` и `/home/synex` и ровно те команды, что нужны
тестам. Программы сервера вроде `docker` и `git` тесты подставляют в эмулятор сами
(`FakeShell.programs`). Что эмулятор отвечает так же, как настоящий sh, проверяет
`tests/test_fake_shell.py`. Эмулятор можно включить явно на любой ОС (на Windows он
включён всегда), например чтобы прогнать на Linux/macOS вариант тестов для Windows:

**PowerShell** (Windows):

```powershell
$env:SYNEX_TEST_SHELL = "emulated"
uv run pytest
Remove-Item Env:SYNEX_TEST_SHELL
```

**bash** (Linux, macOS):

```bash
SYNEX_TEST_SHELL=emulated uv run pytest
```

Пропускаются только тесты, которым нужна конкретная ОС: обёртка команды в настоящих
sh/bash/dash/zsh и права файлов (Linux/macOS), Unix socket и процесс воркера
(Linux/macOS), таймаут через coreutils `timeout` (его нет на macOS).

GitHub Actions (`.github/workflows/backend.yml`) на каждый push в `main` и pull request
с изменениями в `backend/` запускает `ruff check`, `ruff format --check` и pytest на
Windows, Linux и macOS, а на Linux ещё раз с `SYNEX_TEST_SHELL=emulated`. Перед push
то же самое локально:

**PowerShell** (Windows):

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

**bash** (Linux, macOS):

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

`tests/test_real_worker.py` запускает настоящий воркер: `synex_worker.exe` на Windows,
сборку `synex_worker` под Linux/macOS (ищется так же, как при запуске бэкенда) или
команду из `SYNEX_TEST_WORKER` (например, скрипт, который запускает exe через wine).
Проверки с реальным SSH-сервером включаются переменными окружения:

**PowerShell** (Windows):

```powershell
$env:SYNEX_TEST_SSH_HOST = "1.2.3.4"
$env:SYNEX_TEST_SSH_USER = "root"
$env:SYNEX_TEST_SSH_KEY = "C:/Users/me/.ssh/id_ed25519"
$env:SYNEX_TEST_SSH_PASSWORD = "..."   # необязательно: проверить вход по паролю
uv run pytest tests/test_real_worker.py -v
```

**bash** (Linux, macOS):

```bash
export SYNEX_TEST_SSH_HOST=1.2.3.4
export SYNEX_TEST_SSH_USER=root
export SYNEX_TEST_SSH_KEY=~/.ssh/id_ed25519
export SYNEX_TEST_SSH_PASSWORD=...      # необязательно: проверить вход по паролю
uv run pytest tests/test_real_worker.py -v
```

### Интеграционные тесты: полный цикл

`tests/integration/` проверяет цепочку **UI → FastAPI → synex_worker → SSH-сервер →
ответ в UI** целиком: бэкенд запущен на uvicorn с настоящим воркером, а клиент ведёт
себя как UI — REST и оба WebSocket (`/ws` и терминал сессии) с заголовком `Origin`
страницы UI. Что проверяется:

- терминал: команда через WebSocket, вывод, stderr, код возврата, cwd, таймаут на
  сервере, закрытие сессии доходит до терминала и до `/ws`;
- метрики сервера в `/ws`: один сервер на все его сессии, пропадает с последней; сеть и
  раздел `/` от воркера, счётчик принятых байт не убывает;
- две сессии выполняют команды параллельно;
- вход по ключу с парольной фразой, по ключу из хранилища, по паролю; ошибки входа и
  подключения доходят до UI как 502, молчащий сервер — как 504 `ssh_connect_timeout`;
- команда, которая читает ввод (`cat`, `read`), не ждёт до таймаута;
- удалённая папка сессии: команда не выполняется в домашней папке;
- переподключение терминала с `since`: UI получает пропущенный вывод по порядку;
- падение воркера: терминал и `/ws` узнают о потере сессии, бэкенд перезапускает
  воркер, новая сессия работает;
- ключ сервера (TOFU): первое подключение отвечает 409 с отпечатком, который воркер
  посчитал так же, как `ssh-keygen -lf` по ключу sshd; после подтверждения сессия
  открывается; чужой отпечаток даёт 409 `ssh_host_key_mismatch` без повтора, замена
  только с `replace`. Остальные тесты подтверждают ключ своего sshd заранее (фикстура
  `ui`), у внешнего сервера отпечаток показывает первое подключение;
- сервер, записанный в `known_hosts` OpenSSH, подключается без подтверждения, а другой
  ключ того же типа в этом файле даёт 409 `ssh_host_key_mismatch`;
- все плагины загружаются (`PLUGINS_ENABLED=*`); сохранённое подключение открывает
  сессию, а `connection_id` доходит до `/ws`;
- критерий вехи B4: SSH, SFTP, мониторинг и Docker выключаются и включаются на
  работающем backend, UI видит это в `/ws`, API выключенного модуля отвечает 404, после
  включения всё снова работает (выключение `ssh` закрывает сессии, новая открывается);
- сценарий MVP целиком (`test_mvp_scenario.py`): «подключился — передал файл — смотрит
  метрики — перезапустил контейнер» с проверкой критериев устава: SSH-подключение не
  дольше 3 с, файл 100 МБ без ошибок, метрики не реже 1 раза в секунду и не старше
  500 мс, перезапуск, остановка, запуск и логи трёх контейнеров. С `-s` тест печатает
  сводку с замерами;
- git на сервере: репозиторий отстаёт от origin, `status`, `fetch` и деплой (`pull` и
  команда в папке репозитория), терминал сессии служебных команд не видит;
- docker на сервере: контейнер из образа, собранного из пустого архива (без скачивания),
  список, логи, ошибка запуска и удаление. Нужен работающий Docker, иначе тест
  пропускается;
- критерий MVP по Docker: три запущенных контейнера (образ собирается из `sh` и `sleep`
  этой машины, без сети): логи каждого, живой лог по WebSocket без повторов и с
  переподключением, `restart`, `stop`, `start`, `stats`;
- файлы на сервере (SFTP воркера): папки, имена с пробелами, кавычками и кириллицей,
  загрузка и скачивание телом запроса, копирование и удаление папок, список на полторы
  тысячи файлов с русскими именами;
- критерий MVP по SFTP: файл 100 МБ уходит на сервер и возвращается, SHA-256 совпадает,
  прогресс идёт в `/ws`, а команда терминала проходит во время передачи. Размер задаёт
  `SYNEX_TEST_SFTP_MB`; тест печатает время и скорость (`-s`). На локальном `sshd`:
  около 1с в каждую сторону;
- папка целиком: больше ста файлов, вложенные и пустая папки, имена с пробелами,
  кавычками и кириллицей уходят на сервер и возвращаются без отличий; объединение с
  существующей папкой; ссылка на сервере пропускается; отмена посреди большого файла не
  оставляет ни папки, ни временных файлов на сервере и на этом компьютере.

Нужны воркер (как для `test_real_worker.py`) и SSH-сервер. Если `SYNEX_TEST_SSH_*`
не заданы, на Linux и macOS тесты сами поднимают `sshd` на `127.0.0.1` со случайным
портом и своими ключами (вход под текущим пользователем, система не меняется). Без
воркера или SSH-сервера тесты пропускаются.

**PowerShell** (Windows):

```powershell
uv run pytest -m integration            # только интеграционные
uv run pytest -m "not integration"      # всё, кроме них
```

**bash** (Linux, macOS):

```bash
uv run pytest -m integration            # только интеграционные
uv run pytest -m "not integration"      # всё, кроме них
SYNEX_TEST_WORKER=./run_worker.sh uv run pytest -m integration   # exe через wine
```

В GitHub Actions их запускает отдельный джоб `integration`, дважды: с Linux-воркером из
репозитория и с воркером, собранным из `core/` (`cargo build --release`), против `sshd`
на раннере. Поэтому workflow запускается и при изменениях в `core/`: правка Rust-кода
сразу проверяется вместе с бэкендом. Обычные джобы `test` идут с `-m "not integration"`.

`test_real_worker_raw_exit_codes` проверяет коды возврата самого воркера (исправлены в
сборке 2026-11-14); со старой сборкой, где они всегда 0, тест `xfail`. `/api/ssh/exec`
при `track_cwd=true` и так берёт код возврата из shell.

Воркер с 2026-11-14 подключается только с подтверждённым ключом сервера: тесты с
`SYNEX_TEST_SSH_*` делают как UI — первое подключение узнаёт отпечаток
(`host_key_unknown`), второе идёт с ним. Проверку в воркере тесты не выключают.

`tests/fake_worker.py` повторяет поведение воркера сборок 2026-11-03 … 2026-11-17:
сессия = `id` запроса, сессии принадлежат подключению, запросы подключения по очереди,
`exit_code` всегда 0, проверка ключа сервера (`host_key_mode`), SFTP двух сборок
(`sftp_build`), сеть и раздел `/` в метриках (`monitor_build`). `tests/ssh_helpers.py` собирает плагин ssh поверх него и запускает uvicorn
для тестов WebSocket. `tests/worker_host.py` — отдельный процесс-«бэкенд» для тестов,
где его убивают через `kill -9`.
