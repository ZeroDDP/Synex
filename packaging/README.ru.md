# Сборка Synex

[English version](README.md)

Synex поставляется как desktop-приложение: одна папка (или `Synex.app`) с backend, UI и
`synex_worker` своей ОС. Для каждой ОС свой сборщик: он находит или собирает воркер этой
ОС, а потом выполняет общие шаги из [`build.py`](build.py).

| ОС | Сборщик | Воркер | Результат в `packaging/dist` |
|---|---|---|---|
| Windows x64 | [`windows/build.ps1`](windows/build.ps1) | `synex_worker_windows.exe` из репозитория (`-FromCore` собирает его из `core\`) | `Synex-<v>-windows-x64.zip`, `Synex-<v>-windows-x64-setup.exe` (с Inno Setup 6) |
| Linux x86_64 | [`linux/build.sh`](linux/build.sh) | `synex_worker_linux` из репозитория (`--from-core` собирает его; на arm64 всегда так) | `Synex-<v>-linux-x64.tar.gz` |
| macOS | [`macos/build.sh`](macos/build.sh) | готовый воркер под этот Mac (Apple Silicon или Intel) | `Synex-<v>-macos-<арх>.zip` с `Synex.app` |

Версия берётся из [`VERSION`](../VERSION). Рядом с архивами — `SHA256SUMS.txt`.

Воркер из репозитория собран на Ubuntu 24.04 и требует glibc 2.38 или новее. Workflow
выпуска собирает воркер для Linux из `core/` на Ubuntu 22.04, поэтому опубликованная
сборка для Linux запускается и на Ubuntu 22.04, и на Debian 12.

## Что нужно

- Python 3.12 и [uv](https://docs.astral.sh/uv/), Node.js 22 (как для разработки).
- `-FromCore` / `--from-core`: Rust ([rustup](https://rustup.rs)); на macOS ещё
  Xcode Command Line Tools.
- Установщик для Windows (по желанию): [Inno Setup 6](https://jrsoftware.org/isinfo.php).

PyInstaller (и pywebview для окна на Windows и macOS) ставится только на время сборки
(`uv run --with`), в `uv.lock` их нет.

## Как собрать

Из корня репозитория; результат в `packaging/dist`.

**PowerShell** (Windows):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1             # воркер из репозитория
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -FromCore   # воркер собирается из core\
```

**bash** (Linux, macOS):

```bash
bash packaging/linux/build.sh               # Linux: воркер из репозитория
bash packaging/linux/build.sh --from-core   # Linux: воркер собирается из core/
bash packaging/macos/build.sh               # macOS: готовый воркер для архитектуры Mac
```

В PowerShell 7 можно и `pwsh packaging\windows\build.ps1`. `-NoInstaller` пропускает
установщик для Windows.

## Что делает сборка

1. Собирает UI (`npm ci` при необходимости, `npm run build`). HTTP API и WebSocket используют адрес открытого окна.
2. Проверяет, что воркер — программа этой ОС, и кладёт его в сборку.
3. Запускает PyInstaller с [`synex.spec`](synex.spec): `Synex/` (onedir, быстрый запуск)
   или `Synex.app`. Иконки: [`assets/`](assets) (логотип из макета Figma).
4. **Проверка**: запускает сборку без окна с пустой папкой данных, ждёт `/api/health` с
   запущенным воркером из сборки и проверяет, что на `/` отдаётся UI.
   При сборке релиза Chromium открывает один и тот же UI на двух портах. Проверка ждёт ответ
   на запрос алертов и приветствие WebSocket, затем читает `/api/health` из страницы.
5. Упаковывает архив и считает его SHA-256.

Параметры `build.py` идут после параметров сборщика: `--clean` (заново `npm ci`),
`--no-window` (без pywebview: UI откроется в браузере), `--no-smoke`. Порт задаётся через `PORT` в `synex.env` при запуске.

## Как работает приложение

`Synex` (`Synex.exe`, `Synex.app`) — это [`backend/app/desktop.py`](../backend/app/desktop.py):

- читает `synex.env` из папки данных пользователя (`%LOCALAPPDATA%\Synex`,
  `~/Library/Application Support/Synex`, `~/.local/share/synex`); первый запуск создаёт
  его с примерами. Там работает любая настройка из `backend/.env.example`; переменные
  окружения важнее файла. Ключи, подключения, история терминала и правила алертов лежат в
  той же папке, поэтому переживают обновления;
- если Synex уже запущен, открывает его снова, а не вторую копию;
- запускает backend на `127.0.0.1:8000`; backend запускает воркер из сборки и отдаёт UI
  (`UI_DIR`);
- открывает окно (WebView2 на Windows 10/11, WebKit на macOS) или браузер (Linux или
  `--browser`); закрыли окно — backend и воркер останавливаются;
- у сборок для Windows и macOS нет консоли: журнал — `logs/synex.log` в папке данных
  (прошлый запуск — `synex.log.1`).

`Synex --headless` запускает backend без окна (тесты, сервер), `Synex --version` печатает
версию. Из исходников: `uv run python -m app.desktop` в папке `backend/` после
`npm run build` во `frontend/` (см.
[RUNNING.ru.md](../RUNNING.ru.md#как-приложение-одно-окно-без-dev-сервера)).

На Linux `sh install.sh` в распакованной папке добавляет Synex в меню приложений
(`--remove` убирает).

## Память

[`measure_memory.py`](measure_memory.py) запускает сборку (или backend из исходников) с
пустой папкой данных и меряет RSS и USS backend, воркера и, с `--window`, процессов
WebView:

**PowerShell** (Windows):

```powershell
uv run --project backend --with psutil python packaging/measure_memory.py `
    --app packaging\dist\Synex\Synex.exe
# со сценариями SSH (1 сессия + терминал + живой лог, потом 5 сессий):
#   --ssh user@host:22 --key $HOME\.ssh\id_ed25519
```

**bash** (Linux, macOS):

```bash
uv run --project backend --with psutil python packaging/measure_memory.py \
    --app packaging/dist/Synex/Synex      # macOS: packaging/dist/Synex.app/Contents/MacOS/Synex
# со сценариями SSH (1 сессия + терминал + живой лог, потом 5 сессий):
#   --ssh user@host:22 --key ~/.ssh/id_ed25519
```

Отчёт печатается и сохраняется в `packaging/dist/memory-<ос>-<арх>.md`. Workflow выпуска
меряет на каждой ОС и вставляет таблицы в описание выпуска. Цель (SYN-72): меньше 200 МБ
в простое без окна.

Сборка 0.1.0 для Linux (Ubuntu, x86_64, 15 с на сценарий):

| Сценарий | USS backend | USS воркер | USS всего | RSS всего |
|---|---:|---:|---:|---:|
| простой | 90,1 МБ | 2,7 МБ | 92,8 МБ | 97,7 МБ |
| подключён UI (/ws, метрики) | 91,2 МБ | 2,7 МБ | 93,9 МБ | 98,9 МБ |
| 1 SSH-сессия, терминал, живой лог | 91,9 МБ | 5,2 МБ | 97,1 МБ | 102,0 МБ |
| 5 SSH-сессий | 92,0 МБ | 5,6 МБ | 97,6 МБ | 102,5 МБ |

Окно добавляет свой WebView: замерьте его на Windows и macOS с `--window`.

## Выпуск

1. Впишите новую версию в `VERSION` и опишите её в `CHANGELOG.md` и `CHANGELOG.ru.md`
   (раздел `## [X.Y.Z] - дата`).
2. Влейте в `main` и поставьте тег на свежий `main`:

   **PowerShell** (Windows):

   ```powershell
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   **bash** (Linux, macOS):

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

3. [Workflow выпуска](../.github/workflows/release.yml) собирает Windows, Linux и macOS
   (Apple Silicon и Intel), проверяет сборки, меряет память и создаёт GitHub Release с
   архивами, `SHA256SUMS.txt` и описанием из changelog. Версии `0.x` помечаются как
   pre-release; `v1.0.0` станет первым полноценным выпуском.

Ручной запуск (Actions → release → Run workflow) только собирает и сохраняет файлы как
артефакты.

## Подпись macOS и лицензия

После PyInstaller сборщик подписывает весь `Synex.app` ad-hoc. Перед архивированием CI проверяет подпись через `codesign --verify --deep --strict`, архитектуру launcher и воркера через `lipo`, а также `Synex --version`. Эти проверки повторяются после распаковки ZIP. Для первого запуска проверенного архива очистите quarantine конкретного приложения по инструкции [RUNNING.ru.md](../RUNNING.ru.md#запуск-готовой-сборки).

Для дистрибуции с доверием Gatekeeper нужен сертификат Developer ID Application с закрытым ключом, Hardened Runtime, отправка через `notarytool` и прикрепление билета через `stapler`. Храните сертификат и пароль к нему, а также параметры API Apple в защищённом окружении релиза GitHub. Учётные данные не входят в ZIP и не нужны для ad-hoc-сборок. [Руководство Apple](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/)

Лицензия MIT лежит рядом с исполняемым файлом на Windows и Linux, в `Synex.app/Contents/Resources/LICENSE` на macOS, а также отдельным файлом в GitHub Pre-release. Эта лицензия не требует активации. Замеры доступны в логах workflow и в отчётах сборки. `N/A` означает недоступную метрику. CI требует измеренный RSS, а USS публикуется, если доступен.

## Пока нет

- Authenticode для Windows и автоматической нотаризации Apple.
- AppImage или deb/rpm для Linux, dmg для macOS.
- Автообновлений.
