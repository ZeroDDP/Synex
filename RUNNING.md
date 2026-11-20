# Running Synex

**English** | [Русский](RUNNING.ru.md)

How to install and run the app on your machine: backend and UI together. Backend
details: [backend/README.md](backend/README.md).

## What it consists of

| Part | Folder | Address | What it does |
|---|---|---|---|
| backend | `backend/` | http://127.0.0.1:8000 | FastAPI: API, WebSocket `/ws`, plugins (SSH, keys, connections, monitoring, SFTP files, Docker, Git) |
| worker | `core/` | — | Rust program `synex_worker`: SSH, SFTP and server metrics. The backend starts it on its own |
| UI | `frontend/` | http://localhost:5173 | Svelte + Vite. Talks to the backend over WebSocket `/ws` and HTTP |

A prebuilt worker for Windows x64 and Linux x86_64 is in the repository. On macOS, ARM
and older Linux you have to build it (see
[Worker on macOS, ARM and older Linux](#worker-on-macos-arm-and-older-linux)).

## What to install

| Program | Needed for | Check |
|---|---|---|
| Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/) | backend | `uv --version` |
| [Node.js](https://nodejs.org/) 22 LTS (or 20.19+) with npm | UI | `node --version` |
| Git | code | `git --version` |
| [Rust](https://rustup.rs/) (cargo) | only if the prebuilt worker does not fit | `cargo --version` |

If Python 3.12 is missing, uv installs it on the first `uv sync`.

## First run (once)

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

`uv sync` installs the backend dependencies into `backend/.venv`, `npm ci` installs the UI
dependencies into `frontend/node_modules` exactly as pinned in `package-lock.json`. Neither
folder goes into git.

## Running

You need two terminals, both started from the repository root.

Terminal 1, backend:

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

The backend is ready when the log shows `Application startup complete` and
`Uvicorn running on http://127.0.0.1:8000`. Check: open
http://127.0.0.1:8000/api/health, it shows `"status": "ok"` and `"connected": true` for the worker.

Terminal 2, UI:

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

Open http://localhost:5173. The dev server must run on port 5173 exactly: the backend
accepts UI pages only from http://localhost:5173 and http://127.0.0.1:5173.

To stop: Ctrl+C in both terminals. On shutdown the backend closes the SSH sessions and
stops the worker.

### As an app (one window, no dev server)

Build the UI once and start the launcher: it runs the backend, which serves the built UI,
and opens it in a window (with pywebview) or in the browser.

**PowerShell** (Windows):

```powershell
cd frontend
npm run build
cd ..\backend
uv run python -m app.desktop             # --browser: always the browser; --headless: no UI
```

**bash** (Linux, macOS):

```bash
cd frontend
npm run build
cd ../backend
uv run python -m app.desktop             # --browser: always the browser; --headless: no UI
```

Ready-made builds for Windows, Linux and macOS and how to build them:
[packaging/README.md](packaging/README.md).

## What already works

The UI connects to the `/ws` stream and shows metrics on the Dashboard for servers with an
open SSH session. The other screens still use test data.
You can check the backend without the UI:

- http://127.0.0.1:8000/api/health: state of the backend and the worker;
- http://127.0.0.1:8000/api/plugins: which plugins are loaded. A plugin can be disabled and
  enabled without restarting the backend: `POST /api/plugins/docker/disable` and
  `POST /api/plugins/docker/enable` (from Swagger; in PowerShell
  `Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/plugins/docker/disable`, in bash
  `curl -X POST http://127.0.0.1:8000/api/plugins/docker/disable`). The choice is remembered:
  a disabled plugin stays disabled after a backend restart;
- with `DEBUG=true` in `backend/.env`, Swagger is at http://127.0.0.1:8000/api/docs. There you
  can save a connection (`POST /api/connections`), open an SSH session
  (`POST /api/connections/{id}/connect`; the first time the answer is 409 with the server key
  fingerprint: confirm it with `POST /api/ssh/known-hosts` and connect again), run a command
  (`POST /api/ssh/sessions/{id}/exec`), browse the server's files
  (`GET /api/sftp/sessions/{id}/list`) and containers
  (`GET /api/docker/sessions/{id}/containers`);
- the UI stream: in the browser console on the page http://127.0.0.1:8000/api/health

  ```js
  const ws = new WebSocket("ws://127.0.0.1:8000/ws");
  ws.onmessage = (e) => console.log(JSON.parse(e.data));
  ```

  Metrics (`metrics`) arrive every 500 ms; a server appears in them once an SSH session to
  it is open. Without an SSH server: `WS_FAKE_METRICS=true` in `backend/.env`, then the
  stream contains the test server `demo.synex.local`.

## Settings

`backend/.env` (the template with every setting described: `backend/.env.example`). An empty
value means the default. The main ones:

| Setting | Default | What it does |
|---|---|---|
| `PORT` | `8000` | Backend port |
| `PLUGINS_ENABLED` | `*` | Plugins; `*` means all. If an old `.env` still has `ssh,keys,monitor`, replace it with `*` |
| `UI_DEV_SERVER` | `true` | Accept the UI from http://localhost:5173 and http://127.0.0.1:5173 |
| `ALLOWED_ORIGINS` | empty | Other addresses the UI may call the backend from |
| `WS_FAKE_METRICS` | `false` | Test metrics without an SSH server |
| `DEBUG` | `false` | Verbose logs and Swagger at http://127.0.0.1:8000/api/docs |
| `KEYSTORE_MASTER_KEY` | empty | Only where there is no system password store (headless Linux, CI) |

Restart the backend after changing `.env`.

## Worker on macOS, ARM and older Linux

The prebuilt `synex_worker` in the repository is built for Windows x64 and for Linux x86_64
with glibc 2.38 or newer (Ubuntu 24.04, Debian 13, Fedora 39). In any other case, build it
yourself:

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

The backend finds `core/target/release/synex_worker` on its own and picks the newest build.
Which file was started is shown in the backend log (`Rust worker started`, `path`).

## Checks before a commit

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

Backend integration tests: `uv run pytest -m integration` (on Linux and macOS the tests
start a local OpenSSH server `sshd` themselves; on Windows they need an SSH server in
`SYNEX_TEST_SSH_*`, see [backend/README.md](backend/README.md#tests)). GitHub Actions runs
the backend checks on every PR by itself.

## Troubleshooting

| What you see | Cause and what to do |
|---|---|
| http://127.0.0.1:8000/api/health does not open | The backend is not running or crashed on startup: look at its terminal |
| `npm run dev` started on 5174 or another port | Port 5173 is taken by another dev server. Close it: the backend accepts the UI only from 5173 (or add the address to `ALLOWED_ORIGINS`) |
| Backend: `Invalid settings in backend/.env` | A wrong value in `.env`; the message names the setting. Fix or clear it |
| `forbidden_origin` in the backend log | The UI is not opened from http://localhost:5173 (or http://127.0.0.1:5173). Open that address or add yours to `ALLOWED_ORIGINS` |
| `/api/plugins` has no `connections`, `docker`, `git`, `sftp` | An old `PLUGINS_ENABLED` in `backend/.env`: set it to `*` |
| A plugin is in `PLUGINS_ENABLED`, but `/api/plugins` shows it as `disabled` | It was disabled with `POST /api/plugins/<name>/disable` in an earlier run. Enable it (`/enable`) or delete `plugins.json` (where it is: see the `plugin_disabled` row) |
| Connecting answers 409 `ssh_host_key_unknown` | Expected on the first connection to a server: the worker connects only to servers with a confirmed key. Servers you have already connected to with `ssh` (their key is in `~/.ssh/known_hosts`) connect without confirmation. Compare the fingerprint from the response (on the server: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) and confirm it: `POST /api/ssh/known-hosts`, then connect again |
| Connecting answers 409 `ssh_host_key_mismatch` | The server key has changed: the server was reinstalled or this is an attack. Do not connect until you know why. If the change is expected, confirm the new key: `POST /api/ssh/known-hosts` with `replace: true` |
| Connecting answers 409 `ssh_host_key_revoked` | The server key is marked as revoked (`@revoked`) in the OpenSSH `known_hosts` on this computer (the file and line are named in `detail`). Do not connect: find out why the key was revoked |
| A module answers 404 `plugin_disabled` | It was disabled at runtime, and the backend remembers that after a restart: `POST /api/plugins/<name>/enable`. To return to exactly `PLUGINS_ENABLED`: delete `%LOCALAPPDATA%\Synex\plugins.json` (Linux: `~/.local/share/synex/plugins.json`, macOS: `~/Library/Application Support/Synex/plugins.json`) and restart the backend |
| Files: 502 `sftp_unavailable` with `synex_worker build has no SFTP` in the text | An old worker build without SFTP. Update the repository (`git pull`) or build the worker (`cargo build --release` in `core/`, see [Worker on macOS, ARM and older Linux](#worker-on-macos-arm-and-older-linux)), then restart the backend |
| Files: 502 `sftp_unavailable` with `Subsystem sftp` in the text | SFTP is off on the server: add `Subsystem sftp internal-sftp` to `/etc/ssh/sshd_config` and restart sshd |
| Linux: `GLIBC_2.38 not found` | The prebuilt worker does not fit: build it (`cargo build --release` in `core/`, see [Worker on macOS, ARM and older Linux](#worker-on-macos-arm-and-older-linux)) |
| macOS: the worker exits right away with code -9 | macOS quarantine: `xattr -d com.apple.quarantine <path to synex_worker>`, or build the worker yourself |
| `keystore_unavailable` | No system password store: set `KEYSTORE_MASTER_KEY` (how to generate it is written in `backend/.env.example`) |
| Windows: the backend does not connect to the worker when started with `uvicorn --reload` | Start it only with `uv run python -m app.main`: with `--reload` and `--workers` the Named Pipe to the worker does not work |
