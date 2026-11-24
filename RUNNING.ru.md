# Запуск Synex

[English](RUNNING.md) | **Русский**

Как поставить и запустить приложение на своей машине: backend и UI вместе. Подробности
по backend: [backend/README.ru.md](backend/README.ru.md).

## Из чего состоит

| Часть | Папка | Адрес | Что делает |
|---|---|---|---|
| backend | `backend/` | http://127.0.0.1:8000 | FastAPI: API, WebSocket `/ws`, плагины (SSH, ключи, подключения, мониторинг, файлы SFTP, Docker, Git) |
| воркер | `core/` | — | Rust-программа `synex_worker`: SSH, SFTP и метрики серверов. Backend запускает её сам |
| UI | `frontend/` | http://localhost:5173 | Svelte + Vite. Ходит в backend по WebSocket `/ws` и HTTP |

Готовый воркер лежит в репозитории для Windows x64 и Linux x86_64. На macOS, ARM и
старом Linux его нужно собрать (см.
[Воркер на macOS, ARM и старом Linux](#воркер-на-macos-arm-и-старом-linux)).

## Что установить

| Программа | Зачем | Проверка |
|---|---|---|
| Python 3.12 и [uv](https://docs.astral.sh/uv/getting-started/installation/) | backend | `uv --version` |
| [Node.js](https://nodejs.org/) 22 LTS (или 20.19+) с npm | UI | `node --version` |
| Git | код | `git --version` |
| [Rust](https://rustup.rs/) (cargo) | только если готовый воркер не подходит | `cargo --version` |

Python 3.12 uv поставит сам при первом `uv sync`, если его нет.

## Первый запуск (один раз)

**PowerShell** (Windows):

```powershell
git clone https://github.com/ZeroDDP/Synex.git
cd Synex\backend
uv sync
Copy-Item .env.example .env
cd ..\frontend
npm ci
cd ..
```

**bash** (Linux, macOS):

```bash
git clone https://github.com/ZeroDDP/Synex.git
cd Synex/backend
uv sync
cp .env.example .env
cd ../frontend
npm ci
cd ..
```

`uv sync` ставит зависимости backend в `backend/.venv`, `npm ci` ставит зависимости UI
в `frontend/node_modules` строго по `package-lock.json`. Обе папки в git не попадают.

## Запуск

Нужны два терминала, оба из корня репозитория.

Терминал 1, backend:

**PowerShell** (Windows):

```powershell
cd backend
uv run python -m app.main
```

**bash** (Linux, macOS):

```bash
cd backend
uv run python -m app.main
```

Backend готов, когда в логе появилось `Application startup complete` и
`Uvicorn running on http://127.0.0.1:8000`. Проверка: откройте
http://127.0.0.1:8000/api/health, там `"status": "ok"` и `"connected": true` у воркера.

Терминал 2, UI:

**PowerShell** (Windows):

```powershell
cd frontend
npm run dev
```

**bash** (Linux, macOS):

```bash
cd frontend
npm run dev
```

Откройте http://localhost:5173. Dev-сервер должен работать именно на порту 5173:
backend пускает страницы UI только с http://localhost:5173 и http://127.0.0.1:5173.

Остановка: Ctrl+C в обоих терминалах. Backend при остановке закрывает SSH-сессии и
останавливает воркер.

### Как приложение (одно окно, без dev-сервера)

Соберите UI один раз и запустите лаунчер: он запускает backend, backend отдаёт собранный
UI, и тот открывается в окне (с pywebview) или в браузере.

**PowerShell** (Windows):

```powershell
cd frontend
npm run build
cd ..\backend
uv run python -m app.desktop             # --browser: всегда браузер; --headless: без UI
```

**bash** (Linux, macOS):

```bash
cd frontend
npm run build
cd ../backend
uv run python -m app.desktop             # --browser: всегда браузер; --headless: без UI
```

Готовые сборки для Windows, Linux и macOS и как их собрать:
[packaging/README.ru.md](packaging/README.ru.md).

## Что уже работает

UI подключается к потоку `/ws` и показывает на Dashboard метрики серверов с открытой
SSH-сессией. Остальные экраны пока на тестовых данных.
Backend проверяется и без UI:

- http://127.0.0.1:8000/api/health: состояние backend и воркера;
- http://127.0.0.1:8000/api/plugins: какие плагины загружены. Плагин выключается и
  включается без перезапуска backend: `POST /api/plugins/docker/disable` и
  `POST /api/plugins/docker/enable` (из Swagger; в PowerShell
  `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/plugins/docker/disable`, в bash
  `curl -X POST http://127.0.0.1:8000/api/plugins/docker/disable`). Выбор запоминается:
  после перезапуска backend выключенный плагин остаётся выключенным;
- с `DEBUG=true` в `backend/.env` на http://127.0.0.1:8000/api/docs есть Swagger: там
  можно сохранить подключение (`POST /api/connections`), открыть SSH-сессию
  (`POST /api/connections/{id}/connect`; первый раз ответ 409 с отпечатком ключа
  сервера: подтвердите его через `POST /api/ssh/known-hosts` и подключитесь снова),
  выполнить команду
  (`POST /api/ssh/sessions/{id}/exec`), посмотреть файлы сервера
  (`GET /api/sftp/sessions/{id}/list`) и контейнеры
  (`GET /api/docker/sessions/{id}/containers`);
- поток для UI: в консоли браузера на странице http://127.0.0.1:8000/api/health

  ```js
  const ws = new WebSocket("ws://127.0.0.1:8000/ws");
  ws.onmessage = (e) => console.log(JSON.parse(e.data));
  ```

  Метрики (`metrics`) идут каждые 500 мс; серверы в них появляются, когда к ним
  открыта SSH-сессия. Без SSH-сервера: `WS_FAKE_METRICS=true` в `backend/.env`,
  тогда в потоке есть тестовый сервер `demo.synex.local`.

## Настройки

`backend/.env` (образец и описание всех настроек: `backend/.env.example`). Пустое
значение означает значение по умолчанию. Главное:

| Настройка | По умолчанию | Что делает |
|---|---|---|
| `PORT` | `8000` | Порт backend |
| `PLUGINS_ENABLED` | `*` | Плагины; `*` значит все. Если в старом `.env` осталось `ssh,keys,monitor`, замените на `*` |
| `UI_DEV_SERVER` | `true` | Пускать UI с http://localhost:5173 и http://127.0.0.1:5173 |
| `ALLOWED_ORIGINS` | пусто | Другие адреса, с которых UI ходит в backend |
| `WS_FAKE_METRICS` | `false` | Тестовые метрики без SSH-сервера |
| `DEBUG` | `false` | Подробные логи и Swagger на http://127.0.0.1:8000/api/docs |
| `KEYSTORE_MASTER_KEY` | пусто | Только где нет системного хранилища паролей (headless Linux, CI) |

После изменения `.env` перезапустите backend.

## Воркер на macOS, ARM и старом Linux

Backend выбирает бинарник из `backend/app/core/target/release/` (Windows x64, Linux x64, macOS Intel и Apple Silicon). Для других платформ соберите воркер из исходников.

**PowerShell** (Windows):

```powershell
cd core
cargo build --release
```

**bash** (Linux, macOS):

```bash
cd core
cargo build --release
```

Backend сам находит `core/target/release/synex_worker` и берёт самую свежую сборку.
Какой файл запущен, видно в логе backend (`Rust worker started`, `path`).

## Проверки перед коммитом

**PowerShell** (Windows):

```powershell
cd backend
uv run ruff check .
uv run ruff format --check .
uv run pytest
cd ..\frontend
npm run build
```

**bash** (Linux, macOS):

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run pytest
cd ../frontend
npm run build
```

Интеграционные тесты backend: `uv run pytest -m integration` (на Linux и macOS тесты сами
запускают локальный OpenSSH-сервер `sshd`; на Windows нужен SSH-сервер в
`SYNEX_TEST_SSH_*`, см. [backend/README.ru.md](backend/README.ru.md#тесты)). Проверки
backend в GitHub Actions идут сами на каждый PR.

## Если что-то не работает

| Что видно | Причина и что делать |
|---|---|
| http://127.0.0.1:8000/api/health не открывается | Backend не запущен или упал при старте: смотрите его терминал |
| `npm run dev` запустился на 5174 или другом порту | Порт 5173 занят другим dev-сервером. Закройте его: backend пускает UI только с 5173 (или добавьте адрес в `ALLOWED_ORIGINS`) |
| Backend: `Invalid settings in backend/.env` | В `.env` неверное значение; в сообщении названа настройка. Исправьте или очистите её |
| В логе backend `forbidden_origin` | UI открыт не с http://localhost:5173 (или http://127.0.0.1:5173). Откройте этот адрес или добавьте свой в `ALLOWED_ORIGINS` |
| В `/api/plugins` нет `connections`, `docker`, `git`, `sftp` | В `backend/.env` старый `PLUGINS_ENABLED`: поставьте `*` |
| Плагин есть в `PLUGINS_ENABLED`, но в `/api/plugins` он `disabled` | Его выключили через `POST /api/plugins/<имя>/disable` в один из прошлых запусков. Включите его (`/enable`) или удалите `plugins.json` (где он лежит, см. строку про `plugin_disabled`) |
| Подключение отвечает 409 `ssh_host_key_unknown` | Так и должно быть при первом подключении к серверу: воркер подключается только к серверу с подтверждённым ключом. Серверы, к которым вы уже подключались через `ssh` (их ключ есть в `~/.ssh/known_hosts`), подключаются без подтверждения. Сверьте отпечаток из ответа (на сервере: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) и подтвердите его: `POST /api/ssh/known-hosts`, затем подключитесь снова |
| Подключение отвечает 409 `ssh_host_key_mismatch` | Ключ сервера сменился: сервер переустановили или это подмена. Не подключайтесь, пока не выясните причину. Если смена ожидаемая, подтвердите новый ключ: `POST /api/ssh/known-hosts` с `replace: true` |
| Подключение отвечает 409 `ssh_host_key_revoked` | Ключ сервера помечен как отозванный (`@revoked`) в `known_hosts` OpenSSH на этом компьютере (файл и строка названы в `detail`). Не подключайтесь: выясните, почему ключ отозван |
| Модуль отвечает 404 `plugin_disabled` | Его выключили на ходу, и backend это помнит после перезапуска: `POST /api/plugins/<имя>/enable`. Вернуть набор из `PLUGINS_ENABLED` целиком: удалите `%LOCALAPPDATA%\Synex\plugins.json` (Linux: `~/.local/share/synex/plugins.json`, macOS: `~/Library/Application Support/Synex/plugins.json`) и перезапустите backend |
| Файлы: 502 `sftp_unavailable` и в тексте `synex_worker build has no SFTP` | Старая сборка воркера без SFTP. Обновите репозиторий (`git pull`) или соберите воркер (`cargo build --release` в папке `core/`, см. [Воркер на macOS, ARM и старом Linux](#воркер-на-macos-arm-и-старом-linux)), затем перезапустите backend |
| Файлы: 502 `sftp_unavailable` и в тексте `Subsystem sftp` | На сервере выключен SFTP: добавьте в `/etc/ssh/sshd_config` строку `Subsystem sftp internal-sftp` и перезапустите sshd |
| Linux: `GLIBC_2.38 not found` | Готовый воркер не подходит: соберите его (`cargo build --release` в папке `core/`, см. [Воркер на macOS, ARM и старом Linux](#воркер-на-macos-arm-и-старом-linux)) |
| macOS: воркер сразу завершается с кодом -9 | Карантин macOS: `xattr -d com.apple.quarantine <путь к synex_worker>` или соберите воркер сами |
| `keystore_unavailable` | Нет системного хранилища паролей: задайте `KEYSTORE_MASTER_KEY` (как сгенерировать, написано в `backend/.env.example`) |
| Windows: backend не подключается к воркеру после запуска через `uvicorn --reload` | Запускайте только `uv run python -m app.main`: с `--reload` и `--workers` Named Pipe к воркеру не работает |
