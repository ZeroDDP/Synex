# Synex backend

**English** | [Русский](README.ru.md)

A FastAPI server between the UI and the Rust worker `synex_worker` (the worker protocol is
described in [core/README.md](../core/README.md)). It runs SSH sessions through the worker
(several at once, each with its own terminal over WebSocket), keeps SSH keys encrypted,
stores saved connections, streams server metrics (CPU, RAM, network, disk) to the UI over
WebSocket every 500 ms, and manages Docker, git and files (SFTP) on the servers. Every
feature is a plugin.

## Running

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

**PowerShell** (Windows):

```powershell
cd backend
uv sync                      # dependencies, including the dev group (pytest, ruff)
uv run python -m app.main    # http://127.0.0.1:8000
```

The backend starts `app/core/target/release/synex_worker_windows.exe` as a child process, connects
to it over a Named Pipe (Linux/macOS: Unix socket, TCP as a fallback) and restarts the
worker if it crashes. Settings live in `.env`, the template is `.env.example`. An empty
value (`DEBUG=`) means the default. If a value is wrong, the backend does not start and
says which setting is wrong and why.

Do not run uvicorn with `--reload` or `--workers` on Windows: uvicorn then uses
SelectorEventLoop, and Named Pipes do not work on it.

### Linux and macOS

```bash
cd backend
uv sync
uv run python -m app.main
```

Bundled workers (Windows x64, Linux x64, macOS Intel and Apple Silicon) — `app/core/target/release/`.


1. `RUST_EXECUTABLE_PATH_UNIX` (relative to `backend/`, then to the current folder);
2. a build for a specific platform (`cargo build --release --target <triple>`):
   `app/core/target/<triple>/release/synex_worker`, where `<triple>` is
   `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`, `aarch64-apple-darwin`
   (Apple Silicon) or `x86_64-apple-darwin`;
3. a build from the worker sources in this repository: `core/target/release/synex_worker`
   (and `core/target/<triple>/release/`) after `cargo build --release` in `core/`.
   On Windows `core\target\release\synex_worker.exe` is searched the same way.

Of all matching files the newest one wins: a worker just built in `core/` is newer than the
binary from the repository, and after a `git pull` with a new binary it is the other way
round. Which file was started is shown in the log (`Rust worker started`, `path`). If the
path in `RUST_EXECUTABLE_PATH_*` was changed (not the default), only that path is used.

The file is checked by its header: `synex_worker.exe` or a build for another CPU is skipped,
and the log says what it was built for (instead of "Exec format error"). A wrapper script
(`#!/bin/sh`) works too. The backend sets the execute permission (`chmod +x`) itself. macOS
may block an unsigned binary downloaded from the internet (the worker dies right away with
code -9): `xattr -d com.apple.quarantine <path to synex_worker>`.

The worker listens on a Unix socket. By default (`RUST_IPC_SOCKET_PATH` empty) it lives in
the user's private folder, not in the shared `/tmp`: `$XDG_RUNTIME_DIR/synex/worker.sock`
(Linux, usually `/run/user/<uid>`), without `XDG_RUNTIME_DIR` `/tmp/synex-<uid>/worker.sock`,
on macOS `$TMPDIR/synex/worker.sock`. Before starting the worker the backend:

- creates the folder with mode `0700` and checks that it owns it and that it is not a
  symlink; otherwise it takes a temporary folder with a random name;
- removes a socket file nobody listens on (left after a worker crash): otherwise the worker
  could not take the path and would fall back to TCP;
- checks the path length: at most 103 bytes on macOS and 107 on Linux.

So that the worker does not keep running without the backend (on Windows a Job Object takes
care of it):

- the worker runs in its own session: Ctrl+C in the terminal stops only the backend, and the
  backend closes the SSH sessions and then the worker properly;
- Linux: if the backend is killed (`kill -9`, a crash, the IDE), the kernel immediately sends
  the worker SIGTERM (`PR_SET_PDEATHSIG`);
- macOS has no such mechanism. The worker PID is written to `worker.sock.pid` next to the
  socket, and on the next start the backend stops a worker left over from last time: only if
  that backend is no longer running and it is the same process (PID and start time match, so
  a reused PID is not mistaken for the worker).

### API access

The API has no authentication: it listens on `127.0.0.1` only and trusts programs on this
machine. A browser is also a program on this machine, so requests from web pages are
restricted. Without that, any open page could read the sessions and run commands on the
servers through `fetch` and WebSocket.

- A request with an `Origin` header (browsers send it) passes only if the origin is the
  backend itself (`http://127.0.0.1:8000`, `http://localhost:8000`), the UI dev server
  (`http://localhost:5173` and `http://127.0.0.1:5173`, `npm run dev` in `frontend/`) or is
  listed in `ALLOWED_ORIGINS`. Otherwise 403 `forbidden_origin`, and a WebSocket is refused
  on connect. The UI dev server is allowed without configuration: only a program on this
  machine can open port 5173 here, and such a program can reach the API without a browser
  anyway. `UI_DEV_SERVER=false` turns this off (for example, in the packaged app).
- Requests without `Origin` (curl, PowerShell, the desktop app, tests) pass.
- `UI_DIR` (for example `../frontend/dist` after `npm run build`): the backend serves the
  built UI on `/` for every path that is not an API route, so the app works without the dev
  server and from the same origin. `/api/...` paths that do not exist still answer JSON 404.
  The desktop launcher `python -m app.desktop` and the builds in `packaging/` set it.
- The `Host` header must be `127.0.0.1`, `localhost`, `[::1]` or from `ALLOWED_HOSTS`.
  Otherwise 403 `forbidden_host`: this protects against DNS rebinding.
- `HOST=0.0.0.0` exposes the API to the network without authentication. The backend logs a
  warning about it on startup.

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/api/ssh/sessions` | Open SSH sessions |
| POST | `/api/ssh/sessions` | Open a session: `{host, port, username, key_id \| key_path [+ passphrase] \| password}` → 201 |
| GET | `/api/ssh/sessions/{id}` | One session: host, `cwd`, whether it is busy, how many tabs watch it |
| DELETE | `/api/ssh/sessions/{id}` | Close the session |
| POST | `/api/ssh/sessions/{id}/exec` | Run a command: `{command, timeout?, track_cwd?, allow_interactive?}` |
| POST | `/api/ssh/sessions/{id}/interrupt` | Interrupt the running command (Ctrl+C) |
| GET / DELETE | `/api/ssh/sessions/{id}/history` | Command history of the session's server (`?limit=`), clear it |
| GET | `/api/ssh/sessions/{id}/complete` | Tab completion: `?line=&cursor=` |
| GET | `/api/ssh/sessions/{id}/output` | Output buffer: `?since=N&limit=M` |
| WS | `/api/ssh/sessions/{id}/ws` | Session terminal: `?since=N` (see below) |
| POST | `/api/ssh/connect` | Same as `POST /sessions`, shorter response |
| POST | `/api/ssh/exec` | Command: `{command, session_id?, timeout?, track_cwd?}`, without `session_id` in the last opened session |
| POST | `/api/ssh/disconnect` | Close a session: `{session_id?}` |
| GET | `/api/ssh/status` | Session state: `?session_id=` or the last opened one |
| GET | `/api/ssh/known-hosts` | Trusted server keys: fingerprints by `host:port`, `source`: who confirmed it (`user`, `openssh`) |
| POST | `/api/ssh/known-hosts` | Trust a server key after the user confirmed it: `{host, port?, fingerprint, key_type?, replace?}` → 201 |
| DELETE | `/api/ssh/known-hosts` | Forget a server key: `?host=&port=22` |
| GET | `/api/keys` | Key list (without the secret part) |
| POST | `/api/keys` | Import: `{name, private_key \| private_key_path, passphrase?, comment?}` → 201 |
| POST | `/api/keys/generate` | New key: `{name, type: ed25519 \| rsa, bits?, comment?}` → 201 |
| GET | `/api/keys/{id}` | One key |
| PATCH | `/api/keys/{id}` | Rename or change the comment: `{name?, comment?}` |
| DELETE | `/api/keys/{id}` | Delete → 204 |
| GET | `/api/connections` | Saved connections (without passwords) |
| POST | `/api/connections` | Save: `{name, host, port?, username, group?, auth, key_id \| key_path [+ passphrase] \| password?}` → 201 |
| GET | `/api/connections/{id}` | One connection |
| PATCH | `/api/connections/{id}` | Change the fields that are passed (see below) |
| DELETE | `/api/connections/{id}` | Delete → 204 |
| POST | `/api/connections/{id}/connect` | Open an SSH session from a connection: `{password?, passphrase?}` → 201, session description |
| GET | `/api/docker/sessions/{id}/info` | Whether the session's server has Docker, versions |
| GET | `/api/docker/sessions/{id}/containers` | Containers: `?all=false` running only |
| POST | `/api/docker/sessions/{id}/containers/{c}/{action}` | `start`, `stop`, `restart`, `pause`, `unpause`, `kill` (`?timeout=N` for stop/restart) |
| DELETE | `/api/docker/sessions/{id}/containers/{c}` | Remove a container: `?force=true&volumes=true` |
| GET | `/api/docker/sessions/{id}/containers/{c}/logs` | Logs: `?tail=200&since=<next_since>` |
| WS | `/api/docker/sessions/{id}/containers/{c}/logs/ws` | Live container log: `?tail=200&since=` (see below) |
| GET | `/api/docker/sessions/{id}/stats` | CPU, memory, network and disk of running containers |
| GET | `/api/docker/sessions/{id}/images` | Images on the server |
| GET | `/api/git/sessions/{id}/status` | Repository on the server: `?path=~/app`, branch, how far behind, changes |
| GET | `/api/git/sessions/{id}/log` | Commits: `?path=~/app&limit=20` |
| POST | `/api/git/sessions/{id}/fetch` | `git fetch`: `{path}` → state with a fresh `behind` |
| POST | `/api/git/sessions/{id}/pull` | `git pull --ff-only`: `{path}` → the incoming commits |
| POST | `/api/git/sessions/{id}/deploy` | pull and a command in the repository folder: `{path, command?, only_if_updated?, timeout?}` |
| GET | `/api/sftp/sessions/{id}/info` | Files on the server: home folder, user |
| GET | `/api/sftp/sessions/{id}/list` | Folder contents: `?path=/var/log` (home folder without `path`) |
| GET | `/api/sftp/sessions/{id}/stat` | About a file or folder: `?path=` |
| POST | `/api/sftp/sessions/{id}/mkdir` | Create a folder: `{path, parents?}` → 201 |
| POST | `/api/sftp/sessions/{id}/move` | Rename or move: `{source, target, overwrite?}` |
| POST | `/api/sftp/sessions/{id}/copy` | Copy: `{source, target, overwrite?}` → 201 |
| DELETE | `/api/sftp/sessions/{id}/entry` | Delete: `?path=&recursive=true` |
| POST | `/api/sftp/sessions/{id}/upload` | Upload a file in the request body: `?path=&name=&overwrite=false` → 201 |
| GET | `/api/sftp/sessions/{id}/download` | Download a file in the response body: `?path=` |
| POST | `/api/sftp/sessions/{id}/transfers` | Transfer a file or folder by a path on this computer, in the background → 202 |
| GET | `/api/sftp/transfers` | Transfers: `?session_id=&active=true` |
| GET | `/api/sftp/transfers/{id}` | Transfer state |
| DELETE | `/api/sftp/transfers/{id}` | Cancel a transfer or remove a finished one from the list |
| GET | `/api/sftp/local/list` | A folder of this computer (second pane): `?path=` |
| GET | `/api/sftp/sessions/{id}/search` | Find files and folders on the server: `?path=&name=&contains=&type=&max_depth=&hidden=&limit=` |
| GET | `/api/sftp/local/search` | The same on this computer |
| GET | `/api/monitor/metrics` | Server metrics right now (same as `metrics` in `/ws`) |
| GET | `/api/alerts` | Alerts of all servers: `?status=active\|resolved\|all&server=host:port` |
| GET | `/api/alerts/{id}` | One alert |
| POST | `/api/alerts/{id}/ack` | Mark an alert as seen; `POST /api/alerts/ack-all` for all active ones |
| DELETE | `/api/alerts/{id}` | Remove a resolved alert from the history; `DELETE /api/alerts` clears the history |
| GET / PUT / DELETE | `/api/alerts/rules` | Alert rules (thresholds): read, replace, reset to defaults |
| GET | `/api/logs/servers/{server}/sources` | What logs the server has: systemd journal (and access to it), files in `/var/log` |
| GET | `/api/logs/servers/{server}/journal` | Journal entries: `?unit=&priority=&grep=&since=&until=&boot=&lines=200&before=` |
| GET | `/api/logs/servers/{server}/journal/units` | Units that wrote to the journal (for the filter) |
| WS | `/api/logs/servers/{server}/journal/ws` | Live journal: same filters, `?lines=200&after=<cursor>` |
| GET | `/api/logs/servers/{server}/files` | Log files in a folder: `?dir=/var/log` |
| GET | `/api/logs/servers/{server}/file` | Last lines of a file: `?path=&lines=200&grep=` |
| WS | `/api/logs/servers/{server}/file/ws` | Live log file: `?path=&lines=200&grep=&offset=&inode=` |
| GET | `/api/health` | State of the backend and the worker, loaded plugins |
| GET | `/api/plugins` | Plugins: version, description, dependencies, `status` and failure reason (see below) |
| POST | `/api/plugins/{name}/enable` | Enable a plugin without a restart (with its dependencies); the choice is remembered |
| POST | `/api/plugins/{name}/disable` | Disable a plugin without a restart: `?cascade=true` together with the plugins that depend on it; the choice is remembered |
| WS | `/ws` | Data stream for the UI: server metrics every 500 ms (see below) |

With `DEBUG=true` Swagger is available at `/api/docs`.

### Example: key from the vault → connect → command → disconnect

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

The bash variant reads the JSON responses with [jq](https://jqlang.org/).

### SSH sessions

- Every `POST /api/ssh/sessions` (or `/api/ssh/connect`) opens a new session and returns a
  `session_id`. Sessions that are already open keep working. Each session has its own `cwd`.
  Up to `SSH_MAX_SESSIONS` (20) sessions at a time, then 429.
- Each session has its own IPC connection to the worker: the worker handles the requests of
  one connection one by one and those of different connections in parallel. Commands of one
  session run one by one, of different sessions in parallel. If the worker does not accept
  a separate connection, sessions share one (`channel: "shared"` in the session description)
  and the commands of all sessions run one at a time.
- Exactly one login method: `key_id` (a key from the vault), `key_path` (a key file on this
  machine; if the key is encrypted, add `passphrase`) or `password`. The password and the
  passphrase are never saved and never reach the logs.
- `exec`, `disconnect` and `status` without `session_id` work with the last opened session.
  So a client that knows about a single session only works as before.
- A session lives inside the worker and is tied to its IPC connection. If the worker crashes
  or the connection drops, the session is closed: tabs get `closed`, `exec` returns
  404/409, you need to connect again.
- Closing a session (`DELETE`, `close` from the WebSocket) also aborts a command still running
  in it: its request returns 409 `ssh_not_connected`.
- Everything that happens in a session goes into its output buffer: `opened`, `command`
  (the command and `cwd`, and where it came from: `api` or `ws`), `result` (`stdout`,
  `stderr`, `exit_code`, `cwd`, `timed_out`, `duration_ms`) and `error`. Each record has a
  number `seq` that grows by 1. The buffer is limited by `SSH_OUTPUT_BUFFER_BYTES` (1 MiB per
  session): old records are evicted, and an output that is too large keeps its end and
  `truncated: true`.
- Every open and close sends `ssh.session` to the event bus
  (`{action: opened | closed, session_id, host, port, username, connection_id, reason?}`).
- The session description has `host_key: {fingerprint, key_type}`: the server key the worker
  connected with (`null` for worker builds without key verification). How a key is
  confirmed: see "Server key verification".
- Plugins (docker, git) run their own service commands in a session
  (`ssh_manager.run_service`). They do not go into the output buffer or the terminal, do not
  change the session `cwd`, run from the home folder, with a limit on the server and an exact
  exit code. If a user command is running in the session, the service command waits for it
  up to `SSH_SERVICE_WAIT` (10 s), then 409 `ssh_session_busy`. For long plugin commands
  (deploys, logs) the UI should open a separate session to the same server.

### Server key verification (TOFU)

The worker (builds since 2026-11-14) connects only to a server whose key the user has
confirmed: this way a substituted server (man in the middle) does not go unnoticed, and the
password does not go to someone else's server. The backend keeps the fingerprints in
`known_hosts.json` next to `keys.json` (`KNOWN_HOSTS_PATH`), by `host:port`.

1. The first connection to a server (`POST /api/ssh/sessions`, `/api/ssh/connect` or
   `/api/connections/{id}/connect`) answers 409 `ssh_host_key_unknown`:

   ```json
   {"error": "ssh_host_key_unknown", "detail": "The key of 1.2.3.4:22 is not trusted yet: ...",
    "host_key": {"host": "1.2.3.4", "port": 22,
                 "fingerprint": "SHA256:TUKuyjD5oX42qzSLLzmaPDg4iVmiU6ThNtZwYmGiVek",
                 "key_type": "ssh-ed25519"}}
   ```

2. The UI shows the fingerprint and the key type and asks the user. The fingerprint can be
   checked on the server: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
3. Only after confirmation the UI saves it: `POST /api/ssh/known-hosts` with `host`, `port`,
   `fingerprint` and `key_type` from `host_key`, and repeats the connection. From then on the
   backend passes the fingerprint to the worker on every connection to this `host:port`
   (service sessions, for example for file transfers, use the key of their session).
4. If the server key has changed, connecting answers 409 `ssh_host_key_mismatch`, with the
   received `fingerprint` and the saved `expected_fingerprint` in `host_key`. The backend
   does not retry the connection and does not change the saved fingerprint. The UI warns:
   this may be an attack or a reinstalled server. The fingerprint can be replaced only by an
   explicit user action: `POST /api/ssh/known-hosts` with the new `fingerprint` and
   `replace: true` (without `replace`: 409 `ssh_known_host_conflict`).

`DELETE /api/ssh/known-hosts?host=&port=` forgets a key: the next connection asks for
confirmation again. Worker builds without key verification (before 2026-11-14) connect as
before; the fingerprint is passed to them but not checked.

Servers you have already connected to with `ssh` need no second confirmation: their keys are
in the OpenSSH `known_hosts` on this computer. When a server is not in `known_hosts.json`
yet, the backend looks for the key the server showed in these files (`~/.ssh/known_hosts`,
`~/.ssh/known_hosts2` and the machine-wide `/etc/ssh/ssh_known_hosts`, on Windows
`%PROGRAMDATA%\ssh\ssh_known_hosts`):

- the key is recorded there for this server: the backend saves the fingerprint (`source:
  "openssh"` in `GET /api/ssh/known-hosts`) and connects, the answer is a normal 201. For a
  server on a non-standard port, as with `ssh`, an `[host]:port` entry works, or an entry of
  the same server without a port with the same key;
- a different key of the same type is recorded there for the server (`ssh` prints REMOTE
  HOST IDENTIFICATION HAS CHANGED in this case): 409 `ssh_host_key_mismatch`, `host_key` also
  has `source: "openssh"`, and `expected_fingerprint` is the fingerprint from the file. No
  connection; the user can confirm the new key as in step 4;
- the key is marked there as revoked (`@revoked`): 409 `ssh_host_key_revoked`, no connection;
- the server is not there, or only keys of other types are recorded: the normal confirmation
  (step 1).

Hashed names (`HashKnownHosts`, the default in Debian and Ubuntu), the `*` and `?` patterns and
`!` negation are understood; `@cert-authority` lines are ignored. The backend only reads the
OpenSSH files. An entry in `known_hosts.json` wins: if the server fingerprint is there, the
OpenSSH files are not consulted. `OPENSSH_KNOWN_HOSTS=false` turns the lookup off,
`OPENSSH_KNOWN_HOSTS_FILES` sets your own files, comma-separated.

### Terminal WebSocket

`ws://127.0.0.1:8000/api/ssh/sessions/{id}/ws?since=N`. One connection watches one session;
one session can be watched by several tabs. The envelope is the same as in `/ws`:
`{"type", "ts", "data"}`.

| type | Who sends it and when | data |
|---|---|---|
| `hello` | server, right after connecting | `{protocol, session, replay: {since, count, gap, first_seq, last_seq}}` |
| `output` | server: first the buffer records after `since`, then new ones | a buffer record `{seq, ts, kind, ...}` |
| `closed` | server, when the session was closed; then it closes the connection | `{reason}`: `closed`, `worker_connection_lost`, `lost_in_worker`, `exec_timeout`, `shutdown` |
| `error` | server, if a message could not be parsed or a command did not start | `{error, detail, request_id?}`; `ssh_interactive_command` also has `program` and `hint` |
| `interrupt` | server, in reply to `interrupt` | `{status, signal, command}`, as `POST .../interrupt` |
| `pong` | server, in reply to `ping` | `data` from `ping` |
| `exec` | UI | `{command, timeout?, track_cwd?, allow_interactive?, id?}`: `id` comes back in the records as `request_id` |
| `interrupt` | UI | interrupt the running command (Ctrl+C) |
| `close` | UI | close the session for all tabs |
| `ping` | UI | anything |

- A command from one tab and its result are seen by all tabs of the session, and so are
  commands sent through REST.
- Closing a tab does not close the session: the command keeps running, the result waits in
  the buffer. When reconnecting, pass `since` = the last received `seq` and get only what was
  missed; `gap: true` means part of it has already been evicted.
- Terminal output is never dropped silently. If the UI cannot keep up, the connection is
  closed with code 1013: reconnect with `since`. Other close codes: 1000 (the session was
  closed), 1001 (the backend stopped), 4404 (no such session).

```js
const ws = new WebSocket(`ws://127.0.0.1:8000/api/ssh/sessions/${id}/ws`);
ws.onmessage = (e) => console.log(JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({type: "exec", data: {command: "uptime", id: "1"}}));
```

### /api/ssh/exec

Response: `{session_id, seq, stdout, stderr, exit_code, cwd, timed_out, interrupted,
duration_ms}`
(`seq`: the number of the result record in the output buffer). The same for
`/sessions/{id}/exec`.

- `timeout`: execution limit in seconds (default `SSH_EXEC_TIMEOUT=30`, at most 3600). It is
  applied **on the server** with coreutils `timeout`. The command and all its children get
  TERM, and KILL 3 seconds later; after the abort the wrapper also KILLs the whole process
  group of the command, so that a process ignoring TERM does not hold the channel (this is
  how both GNU `timeout` and the uutils `timeout` in Ubuntu 26.04 behave). The response comes
  with code 200, `timed_out: true`, `exit_code` 124 (TERM) or 137 (KILL) and whatever output
  had accumulated. This matters: the worker handles requests one by one, and `tail -f`
  without a limit would occupy it forever.
- Commands of one session run one at a time. The next one waits for the previous one to
  finish, and its timeout starts only when it is actually sent to the worker.
- `cwd` is kept between commands: after `cd /var/log` the next command starts there. If the
  directory was deleted, the command is not run (`exit_code` 1, `cwd` = the home folder).
- `track_cwd: false` sends the command to the worker as is: no cwd, no timeout on the server,
  and the `exit_code` comes from the worker (a real code since the 2026-11-14 worker, always 0
  in older builds). Needed for non-POSIX shells such as fish.
- Line breaks are normalized to `\n`: `\r\n` from Windows text and a lone `\r` (Enter in a
  terminal) would become part of the command on the server (`cd /tmp\r` → "can't cd to
  /tmp").
- The command input (stdin) is `/dev/null`: it cannot be passed through the API, so `cat`,
  `read`, `sudo` with a password or an `apt install` [Y/n] prompt get end-of-input at once and
  exit instead of waiting until the timeout. `sudo` needs NOPASSWD, apt needs `-y`.
- 504 `ssh_exec_timeout` means the worker did not return a result even after
  `timeout + 3 + SSH_EXEC_TIMEOUT_GRACE` seconds. For example, the server has no coreutils
  `timeout` (macOS, old busybox), or the command was sent without `track_cwd`. The worker
  handles the requests of a session one by one, so all following
  commands would be stuck behind the hung one: the session is closed (`ssh.session` with
  `reason: exec_timeout`, the terminal gets `closed`), and the UI opens a new one.

### Terminal: interrupt, programs that need a terminal, history, completion

- **Time limit**: a command from the terminal WebSocket without its own `timeout` gets
  `TERMINAL_TIMEOUT` (600 s) instead of `SSH_EXEC_TIMEOUT`: `apt upgrade` or a build take
  minutes, and Ctrl+C stops a command earlier.
- **Ctrl+C.** `POST .../interrupt` or the terminal message `{"type":"interrupt"}` requests cancellation over IPC. The response acknowledges the request. The command result arrives separately with `interrupted: true` and exit code 143. The worker discards buffered command output on cancellation. Older workers use a companion SSH connection and the command marker.
- **Programs that need a terminal.** Commands run without a TTY, so full-screen programs
  cannot work: `vim` hangs until the timeout and its escape codes break the terminal in the
  UI, `top` exits with an error. Such a command is not sent: 422 `ssh_interactive_command`
  with `program` and `hint` (what to use instead: `top -b -n 1`, the Files tab for editors,
  `git commit -m`). Editors, `top`/`htop`/`btop`, `watch`, `tmux`/`screen`, `mc`, `ncdu`
  and commands that open an editor (`git commit` without `-m`, `git rebase -i`,
  `crontab -e`, `systemctl edit`) are checked in every part of the line, also after `sudo`,
  `env` and `VAR=value`. `allow_interactive: true` sends the command anyway. `less`, `more`
  and `man` work without a terminal like `cat` and are not refused.
- **History**: `GET .../history?limit=` returns the commands of the session's server
  (`user@host:port`, shared by all its sessions, oldest first); it survives a restart in
  `terminal_history.json` next to `keys.json` (`TERMINAL_HISTORY_PATH`). A repeated command
  is stored once, a command starting with a space is not stored (as `HISTCONTROL=ignorespace`
  in bash: that is how commands with passwords are typed). `TERMINAL_HISTORY` (500) commands
  per server, `0` turns the history off. `DELETE .../history` clears it.
- **Completion**: `GET .../complete?line=cat /var/lo&cursor=15` →
  `{word, start, kind, candidates, insert, common, truncated}`. The first word of a command
  is completed with commands from `PATH`, the rest with files and folders from the session's
  current folder (folders end with `/`). Replace the line from `start` up to the cursor with
  `insert[i]` (spaces and special characters are escaped) or with `common` when there are
  several candidates. It is a service command (`compgen` from bash, a shell pattern without
  bash); while a command runs in the session the answer is 409 `ssh_session_busy`.

### Key vault

- File: `%LOCALAPPDATA%\Synex\keys.json` (macOS `~/Library/Application Support/Synex`, Linux
  `~/.local/share/synex`); the path is set with `KEYSTORE_PATH`.
- Private keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). Only the name, type,
  fingerprint and public key are stored in the clear. Writes are atomic; on Linux/macOS the
  file mode is 0600.
- The Fernet master key is created when the first key is saved and is stored in the Windows
  Credential Manager (`synex` / `keystore-master-key`), not next to the file. A copy of
  `keys.json` is useless without the user account. If the Credential Manager entry is
  deleted, the saved keys can no longer be decrypted: the API returns `keystore_corrupted`,
  and no new master key is created silently. Where there is no system store (CI, headless
  Linux), the master key is set in `KEYSTORE_MASTER_KEY`.
- Ed25519, RSA from 2048 bits and ECDSA (P-256/384/521) in OpenSSH and PEM formats are
  supported. A key with a passphrase is decrypted on import (the passphrase is not saved):
  in the vault the key is already protected by Fernet, and connecting by `key_id` needs no
  passphrase.
- The API never returns a private key. `ssh_connect` needs a file path for the worker, so with
  `key_id` the key is decrypted into a temporary file for the time of the connection
  (`runtime/` next to `keys.json`, mode 0600) and deleted right away.

### Saved connections (plugin `connections`)

The server list for the UI: name, address, user, folder (`group`) and login method.

```json
POST /api/connections
{"name": "prod", "host": "10.0.0.1", "port": 22, "username": "deploy", "group": "Work",
 "auth": "key_id", "key_id": "3f2a..."}
```

- `auth`: `key_id` (a key from the vault), `key_path` (a key file on this machine,
  + `passphrase` if it is encrypted) or `password`. The password may be omitted: then it is
  asked on every connection.
- File: `connections.json` next to `keys.json` (`CONNECTIONS_PATH`). Passwords and
  passphrases in it are encrypted with the same master key as the keys; the API never
  returns them, only `has_password` and `has_passphrase`.
- `PATCH` changes only the fields that are passed. A new login method is passed in full
  (`auth` and its fields); without `auth` you can change the key, password or passphrase of
  the current method, and `"password": null` forgets the saved password.
- Names are unique case-insensitively (409 `connection_exists`). An inconsistent login method
  (for example, `auth: key_id` without `key_id` or with a password) is 422
  `invalid_connection`.
- `POST /api/connections/{id}/connect` opens a new SSH session like `POST /api/ssh/sessions`
  and answers with its description. Pass a password or passphrase that is not saved in the
  body (`{"password": "..."}`), otherwise 422 `connection_secret_required`. The session (and
  the `ssh.session` event) has `connection_id`: the UI sees which sessions were opened from
  which connection. The connection time is saved in `last_connected_at`.

### Docker on servers (plugin `docker`)

The worker accesses Docker Engine over SSH through `/var/run/docker.sock`. HTTP API — `/api/docker/sessions/{session_id}/`.

- `GET .../info` always answers 200: `status` `ok`, `not_installed` (no docker),
  `permission_denied` (the SSH user is not in the docker group) or `daemon_unavailable` (the
  daemon is not running), the error text in `detail`, client and server versions.
- `GET .../containers` (`?all=false`: running only) returns the containers:

```json
{"id": "6de6...", "name": "web", "names": ["web"], "image": "nginx:1.27",
 "command": "nginx -g 'daemon off;'", "created_at": "2026-11-06T22:42:33+07:00",
 "state": "running", "status": "Up 17 minutes (healthy)", "health": "healthy",
 "exit_code": null, "networks": ["bridge"],
 "ports": [{"container_port": "80", "protocol": "tcp", "host_ip": "0.0.0.0", "host_port": "8080"}],
 "labels": {"com.docker.compose.project": "shop"}, "compose_project": "shop",
 "compose_service": "web"}
```

- `POST .../containers/{name or ID}/{action}`: `start`, `stop`, `restart`, `pause`,
  `unpause`, `kill`; `stop` and `restart` accept `?timeout=N` (seconds before KILL).
  `DELETE .../containers/{name or ID}` removes it (`?force=true` even if running,
  `?volumes=true` together with anonymous volumes). Response `{container, action, output}`.
  An action that does not fit the state (pausing a stopped container, removing a running one
  without `force`) is 409 `docker_container_conflict`.
- `GET .../containers/{name or ID}/logs?tail=200` returns the last lines of the container's
  stdout and stderr by time: `{container, lines: [{time, stream, text}], next_since}`.
  `since` takes the `next_since` of the previous response (lines already shown are not
  repeated), RFC3339, Unix time or a duration (`10m`, `1h`).
- Live log: WebSocket `.../containers/{name or ID}/logs/ws?tail=200`. The server sends `hello`,
  then `logs` `{lines: [{time, stream, text}], next_since, truncated}`: first the last `tail`
  lines, then new ones as they appear. After a reconnect the UI passes
  `?since=<last next_since>` and does not get lines twice. Messages use the same envelope
  `{type, ts, data}` as `/ws`:

  | type | data |
  |---|---|
  | `hello` | `{protocol, session_id, container, tail, interval_ms}` |
  | `logs` | `{lines, next_since, truncated}`; `truncated: true` if there were more than 10,000 lines between polls and older ones were skipped |
  | `error` | `{error, detail, fatal}`. `fatal: false`: a temporary hiccup (the session is busy with a user command, the daemon does not answer), the stream resumes by itself. `fatal: true`: no such container or no Docker, the connection is closed (code 4404 or 4403) |
  | `closed` | `{reason}`: `session_closed` (the SSH session was closed, code 1000) or `shutdown` (the plugin or the backend is stopping, code 1001) |
  | `pong` | `data` from the UI's `ping` |

  After the history batch, the worker sends live lines using `docker_logs` with `follow=true`.
- `GET .../stats`: one `docker stats` sample of the running containers (a couple of seconds):
  `cpu_percent` (100 = one core), `memory_used`, `memory_limit`, `memory_percent`, `net_rx`,
  `net_tx`, `block_read`, `block_write` (bytes), `pids`.
- `GET .../images`: images (`repository` and `tag` = `null` for untagged images, `size` in
  bytes).
- Errors: 404 `docker_not_installed`, 403 `docker_permission_denied`, 503
  `docker_daemon_unavailable`, 404 `docker_container_not_found`, 409
  `docker_container_conflict`, 504 `docker_timeout`, 502 `docker_command_failed` (the docker
  text in `detail`). A session busy with a user command gives 409 `ssh_session_busy`: the UI
  should open a separate session for the Docker panel.

### Server logs (plugin `logs`)

Logs of the server selected on the Dashboard: the systemd journal and log files. Paths start
with `/api/logs/servers/{server}/`, where `{server}` is `host:port`, the same as `server` in
the `metrics` of `/ws` and in alerts (in a URL: `encodeURIComponent`). The UI does not deal
with sessions: the backend takes an open SSH session to this server, one that is not busy
with a user command if there is one; `?session_id=` picks a specific session. No open session
to the server is 404 `logs_server_not_connected`. Commands are service commands of the
session and do not show up in its terminal.

- `GET .../sources`: `{journal: {available, version, access, detail}, directory, files,
  files_detail}`. `access`: `all` (the whole journal is visible), `user` (only the SSH user's
  own messages: add the user to the `systemd-journal` or `adm` group), `none`. `files` is the
  same as `GET .../files` for `/var/log`; if the folder cannot be listed, `files` is `[]`
  and the reason is in `files_detail`.
- `GET .../journal?lines=200`: the last entries, oldest first:

```json
{"server": "10.0.0.5:22", "session_id": "...", "more": true,
 "before": "s=...;i=1a2", "cursor": "s=...;i=1b7",
 "entries": [{"cursor": "s=...;i=1b7", "time": 1795150389396, "priority": 3, "level": "err",
              "severity": "error", "unit": "nginx.service", "identifier": "nginx",
              "pid": 812, "hostname": "web-1", "boot_id": "...",
              "message": "upstream timed out"}]}
```

  `severity` is the level tab of the UI: `error` (emerg ... err), `warn` (warning), `info`
  (notice, info), `debug`.

  Filters: `unit` (several allowed, a pattern too: `nginx*`), `severity` (only one tab, as
  the ALL / INFO / WARN / ERROR buttons of the Logs screen), `priority` (`emerg` ...
  `debug`: this level and more important; not together with `severity`), `grep` (text, not a
  regex; Latin letters are matched case-insensitively), `since` and `until` (Unix time in ms
  or what journalctl understands: `2026-11-20 10:00:00`, `today`, `-1h`), `boot=true` (since
  the last boot). An older page: `?before=<before of the previous page>`. `more: true`:
  there may be older entries. A filter journalctl does not accept is 422 `logs_bad_filter`
  with its text in `detail`.
- `GET .../files?dir=/var/log`: files of the folder and its subfolders (one level): `{path,
  name, readable, size, modified, rotated}`; compressed (`.gz`, `.xz`) and binary (`wtmp`,
  `btmp`, `lastlog`, journal files) are not listed. `rotated`: an old copy after logrotate
  (`syslog.1`). Files the SSH user cannot read are listed with `readable: false`.
- `GET .../file?path=/var/log/syslog&lines=200`: the last lines `{lines, size, offset,
  inode, truncated}`, a line is `{text, severity}`: the level is taken from a level word at
  the start of the line (`ERROR`, `[warn]`, `level=info`), `info` without one. With `grep`,
  the last lines containing the text; with `severity`, only lines of that tab.
  `truncated: true`: fewer lines than asked, because only the last `LOGS_CHUNK_BYTES` (1
  MiB) of the file are read.
- Live logs: WebSocket `.../journal/ws` (journal filters, `lines`, `after`) and
  `.../file/ws` (`path`, `lines`, `grep`, `severity`, `offset`, `inode`). First the last
  `lines` lines, then new ones as they appear. Messages use the same envelope
  `{type, ts, data}` as `/ws`:

  | type | data |
  |---|---|
  | `hello` | `{protocol, server, session_id, source: journal\|file, interval_ms, lines, companion}`; `companion: true`: polling goes through its own SSH connection and does not wait for a command in the terminal |
  | `entries` | journal: `{entries, cursor, truncated}`. After a reconnect the UI passes `?after=<cursor>` and does not get entries twice |
  | `lines` | file: `{lines, offset, inode, size, rotated, skipped, truncated}`. After a reconnect `?offset=&inode=` continue from the same place. `rotated: true`: logrotate replaced the file, it is read from the start; `skipped`: bytes skipped because the log grew faster than `LOGS_CHUNK_BYTES` per poll. A line still being written is sent once it ends |
  | `error` | `{error, detail, fatal}`. `fatal: false`: a temporary hiccup (the session is busy, the file disappeared during rotation, journalctl did not answer), polling goes on, the same error is not repeated. `fatal: true`: no journal, a wrong filter, the file does not exist or cannot be read when the stream opens; the connection is closed (4404, 4422, 4403) |
  | `closed` | `{reason}`: `server_disconnected` (the last SSH session to the server was closed) or `shutdown` |
  | `pong` | `data` from the UI's `ping` |

  The worker returns command output only as a whole, so `journalctl -f` and `tail -f` cannot
  stream through it: new lines are polled every `LOGS_INTERVAL` (2 s). When the core streams
  output, the protocol for the UI will not change.
- System logs usually need the SSH user in the `adm` group (files) or `systemd-journal`
  (journal): `sudo usermod -aG adm,systemd-journal <user>`, then reconnect.
- Errors: 404 `logs_server_not_connected`, 404 `logs_journal_unavailable` (no journalctl, not
  systemd), 422 `logs_bad_filter`, 404 `log_file_not_found`, 403 `log_file_permission_denied`,
  422 `log_file_is_directory`, 504 `logs_timeout` (`LOGS_TIMEOUT`, 20 s), 502
  `logs_command_failed`. A session busy with a user command longer than `SSH_SERVICE_WAIT`
  gives 409 `ssh_session_busy`.

### Git on servers (plugin `git`)

A repository on the server (for example, the folder of a project that gets deployed): the
plugin runs `git` through an SSH session with service commands. All paths start with
`/api/git/sessions/{session_id}/`; the repository folder is passed in `path`: an absolute
path, `~/app` or a path relative to the home folder.

- `GET .../status?path=~/app`: `head`, `branch` (`null` and `detached: true` for a detached
  HEAD), `upstream`, `ahead` and `behind` (as of the last fetch), `clean`, `changes`
  (`[{path, kind: changed|renamed|conflict|untracked, index, worktree, orig_path}]`) and
  `last_commit`.
- `GET .../log?path=~/app&limit=20`: commits of the current branch, newest first
  (`{hash, short_hash, author_name, author_email, date, subject}`).
- `POST .../fetch` `{"path": "~/app"}`: `git fetch --prune`, the response is like `status`
  with fresh `ahead` and `behind` (how many commits a pull will bring).
- `POST .../pull` `{"path": "~/app"}`: `git pull --ff-only`, fast-forward only, no merge
  commits. Response `{path, before, after, updated, commits, output}`: `commits` are the
  incoming commits (newest first). If the update is not possible, the files do not change,
  and the answer is 409 with the reason: `git_local_changes` (changes on the server that pull
  would overwrite), `git_not_fast_forward` (the server has its own commits),
  `git_no_upstream`, `git_detached_head`. An unreachable remote (network, key, login): 502
  `git_remote_unavailable`.
- `POST .../deploy` `{"path": "~/app", "command": "docker compose up -d --build",
  "only_if_updated": false, "timeout": 600}`: pull (errors as for `/pull`, then the command
  is not run), then `command` in the repository folder with a limit of `timeout` seconds.
  Response 200 `{path, ok, pull, command: {command, exit_code, stdout, stderr, timed_out,
  duration_ms}}`, `ok: false` if the command failed or did not fit into the limit. With
  `only_if_updated: true` the command runs only if pull brought commits. A deploy occupies
  the session until the command ends: the UI should open a separate session for it.
- Git messages are in English (`LC_ALL=C`), login and password prompts are off
  (`GIT_TERMINAL_PROMPT=0`): a private remote must open with the server's key.
- Errors: 404 `git_not_installed`, `git_path_not_found`, `git_not_a_repository`, 504
  `git_timeout` (`GIT_TIMEOUT`, 120 s), 502 `git_command_failed` (the git text in `detail`).

### Files on servers (plugin `sftp`)

A file manager: files and folders on the server, files on this computer (the second pane)
and transfers between them with progress. It works through the SFTP of the Rust worker
(`sftp_*` actions; builds 2026-11-12 and 2026-11-14). Server paths start with
`/api/sftp/sessions/{session_id}/`.

- A path on the server: absolute (`/var/log`), relative to the home folder (`docs/a.txt`) or
  `~/docs`. Paths in responses are always absolute; pass them on as they are.
- `GET .../info`: `{engine: "core", home, user, checksum}`; `checksum`: whether SHA-256 can be
  verified on the server (`null` until checked).
- `GET .../list?path=/var/log` (home folder without `path`):

```json
{"path": "/var/log", "parent": "/var", "home": "/home/deploy", "entries": [
  {"name": "nginx", "path": "/var/log/nginx", "type": "dir", "size": 4096,
   "is_dir": true, "hidden": false, "permissions": "drwxr-xr-x", "mode": 493,
   "target_type": null, "modified_at": "2026-11-14T09:30:12Z", "link_target": null,
   "uid": 0, "gid": 0},
  {"name": "syslog", "path": "/var/log/syslog", "type": "file", "size": 182734, "...": "..."}
]}
```

  Folders first, by name within each group. `type`: `file`, `dir`, `link`, `other`; a link to
  a folder has `target_type: "dir"` and `is_dir: true` (it opens on click), and `link_target`
  holds the path stored in the link. `modified_at`, `uid`, `gid` and `link_target` of server
  files come from the worker since 2026-11-14; with an older build they are `null`.

  The backend checks symlink targets in a single service command using `test -d`. It does not
  list target directories. The command is capped at 32 links and 12 KB, with a two-second
  server-side limit when `timeout` is available. If shell access is unavailable, the target type
  remains unknown. Directory listings and deletion still use worker metadata.
- `GET .../stat?path=`, `POST .../mkdir` `{path, parents?}` → 201, `POST .../move` and
  `POST .../copy` `{source, target, overwrite?}` (`target` is the new full path; a folder is
  copied as a whole), `DELETE .../entry?path=&recursive=true`. Response: the entry (like an
  item of `entries`). The root and the home folder cannot be deleted or moved (403
  `sftp_protected_path`); a non-empty folder is deleted only with `recursive=true`.
- Transferring a file or a folder. Two ways, both visible in `/api/sftp/transfers` and in
  `/ws`:
  - by a path on this computer, in the background: `POST .../transfers` `{direction: upload |
    download, local_path, remote_path, overwrite?}` → 202 and the transfer description. A
    folder (or a path ending with a separator) as the destination means "put it there under
    the same name". Transfers of one session run one after another. Whole folders are
    transferred this way too (below);
  - in the request body: `POST .../upload?path=<where>&name=<name, if path is a folder>`
    (the body is the file content, 201 after it is written and verified) and
    `GET .../download?path=` (the response body is the file, the transfer id is in the
    `X-Synex-Transfer-Id` header).
- `GET /api/sftp/transfers?session_id=&active=true`, `GET /api/sftp/transfers/{id}`,
  `DELETE /api/sftp/transfers/{id}`: cancels a running transfer (the unfinished file is
  deleted), removes a finished one from the list.
- Transfer state (API responses and the `sftp.transfer` event in `/ws`):

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

  `status`: `queued` → `running` → `verifying` → `completed`, or `failed` (`error` and
  `detail`), or `cancelled`; a folder goes through `scanning` after `queued`. The event comes
  on every status change and at most once per `SFTP_PROGRESS_INTERVAL` (0.25 s) while data
  flows. `speed` is in bytes per second.
- The worker transfers the file in a service SSH session to the same server (its own IPC
  connection), so the terminal, metrics, docker and git of the session do not wait during a
  transfer. Progress: on download the backend watches the temporary file grow on this
  computer, on upload it asks for the size of the temporary file on the server. If a service
  session cannot be opened (the worker does not accept extra connections), the transfer runs
  in the main session, and upload progress shows up only at the end.
- A file never appears half-written: the data goes into a temporary file
  (`.synex-<id>.part/` next to the target on the server, `<name>.synex-part` on the
  computer), after the transfer the size and SHA-256 are compared (`checksum`,
  `verified: true`; if the server has none of `sha256sum`, `shasum` and `openssl`, only the
  size is compared), and only then the temporary file replaces the target. On an error or a
  cancel it is deleted.
- Replacing a file (`overwrite: true`): the old one first moves into the temporary folder and
  is deleted only when the new one is in place. If the new one cannot be put in place, the
  old one comes back.
- Cancelling a running transfer answers `cancelled` at once, closes its service session and
  removes the temporary files; the next transfer of the session starts right away. The
  worker cannot abort a transfer: it reads the file to the end in the background, and the
  result goes nowhere. If the cancel arrived at the very start of a file, the worker creates
  it after the cleanup: the backend removes such a file afterwards (it keeps checking the
  temporary paths for another 15 seconds).
- A whole folder: the same `POST .../transfers`, with a folder in `local_path` (upload) or in
  `remote_path` (download). It is one transfer with `kind: "dir"`:

```json
{"id": "9b1d77c0a2e4", "kind": "dir", "direction": "upload", "name": "site",
 "remote_path": "/srv/site", "local_path": "C:\\work\\site", "status": "running",
 "bytes_done": 7340032, "bytes_total": 52428800, "percent": 14.0,
 "files_total": 318, "files_done": 41, "current": "img/photos/2026/cover.jpg",
 "skipped": [{"path": "node_modules/.bin/tsc", "reason": "Links are not transferred"}],
 "skipped_count": 1, "verified": null, "...": "..."}
```

  - The worker transfers files only, so the backend walks the folder itself and transfers
    the files one by one, all in one service session (the terminal stays free). First the
    walk (`scanning`, `bytes_total` and `files_total` are still `null`), then the files:
    `files_done` of `files_total`, `current` is the file in progress (a path from the folder
    root).
  - Destination: an existing folder means "into it under the same name" (`site` into `/srv`
    → `/srv/site`); a path that does not exist yet is the name of the new folder.
  - A new folder appears in place only when complete: it is assembled in a temporary one
    (`.synex-<id>.part/` next to it on the server, `<name>.<id>.synex-part` on the computer)
    and is moved into place by a rename. On an error and a cancel the temporary folder is
    deleted.
  - The folder already exists: 409 `sftp_target_exists` (on the computer
    `sftp_local_exists`). With `overwrite: true` the folders are merged: files with the same
    names are replaced (each goes under a temporary name and is moved into place after the
    check), everything else in the folder is left alone. On an error and a cancel the files
    already transferred stay.
  - Verification: SHA-256 of every file, one command on the server per batch of files
    (without `sha256sum`, `shasum` and `openssl` the size is compared; `verified: true` if
    SHA-256 matched for all of them). A file that does not match is transferred once more:
    usually it was changing during the transfer (a log being written to). If it does not
    match again, it is skipped.
  - Whatever is not transferred goes into `skipped` (the first 50 with a reason, the full
    count in `skipped_count`), and the transfer goes on and ends as `completed`: links (the
    backend does not follow them: a link may lead outside or in a loop), sockets and devices,
    anything without permission, a file in place of a folder and a folder in place of a file
    when merging, names this computer cannot use (on Windows `a:b.txt`, `con`, and also
    `Readme` next to `readme`). The whole transfer is stopped by a lost connection, a server
    refusal without a reason (for example, out of space) and a cancel.
  - Permissions and modification times are not carried over. A folder may hold at most
    `SFTP_FOLDER_LIMIT` files and folders (100,000), otherwise `sftp_folder_too_large`. A
    folder cannot be transferred in a request body (`GET .../download` of a folder: 409
    `sftp_is_a_directory`).
- `GET /api/sftp/local/list?path=C:\Users\me` (home folder without `path`): a folder of this
  computer for the second pane: `{path, parent, home, roots, separator, entries}`, `roots`
  are the drives on Windows and `["/"]` on Linux and macOS. The UI runs in a browser window
  and cannot see the disk, so the backend lists the folders for it.
- Search: `GET .../search` (server) and `GET /api/sftp/local/search` (this computer) look
  through a folder and its subfolders (`path`, home folder without it). Filtering the open
  folder by name is the UI's job: it already has the list. Parameters: `name` (text in the
  name, case-insensitive, or a pattern with `*?[`: `*.log`), `contains` (text inside files;
  binary files and files over `SFTP_SEARCH_MAX_FILE`, 10 MB, are skipped), `type`
  (`any`, `file`, `dir`), `max_depth` (10), `hidden=true` (also inside `.git`, `.cache` and
  other hidden folders), `limit` (up to `SFTP_SEARCH_LIMIT`, 1000). At least `name` or
  `contains` is needed, otherwise 422 `sftp_bad_search`. Response `{path, entries: [{name,
  path, parent, type, is_dir, size, modified_at}], truncated, timed_out, denied,
  elapsed_ms}`, nearer levels first; `parent` opens the folder of a result. On the server
  it is one `find` command in its own SSH connection (the terminal does not wait for it),
  `/proc`, `/sys`, `/dev` and `/run` are skipped; without GNU find (BSD, busybox) only paths
  come back (`type` and `size` are `null`). After `SFTP_SEARCH_TIMEOUT` (30 s) the search
  stops and returns what it found with `timed_out: true`; `denied` counts folders it could
  not open.
- Errors: 404 `sftp_not_found`, 403 `sftp_permission_denied`, `sftp_protected_path`, 409
  `sftp_target_exists`, `sftp_is_a_directory`, `sftp_not_a_directory`, `sftp_not_a_file`,
  `sftp_directory_not_empty`, 422 `sftp_bad_search`, 422 `sftp_bad_path` (including a server
  file name that cannot be given to a file on this computer, for example `a:b.txt` on
  Windows), 502 `sftp_failed` (the worker text in `detail`), `sftp_unavailable` (the server
  has no SFTP: its `sshd_config` needs a `Subsystem sftp ...` line; or the worker build is
  old and does not know the `sftp_*` actions, then update the worker),
  `sftp_transfer_corrupted`, 504
  `sftp_timeout`, for a folder transfer also 422 `sftp_folder_too_large`; for this computer
  `sftp_local_not_found` (404), `sftp_local_permission_denied` (403), `sftp_local_exists` and
  `sftp_local_not_a_directory` (409), `sftp_local_error` (422). A session busy with a user
  command for longer than `SSH_SERVICE_WAIT`: 409 `ssh_session_busy`. The `SFTP_*` settings
  are described in `.env.example`.

### WebSocket /ws

The data stream for the UI: `ws://127.0.0.1:8000/ws` (no `/api` prefix). JSON in text frames
both ways, every message in the envelope `{"type", "ts", "data"}`, `ts` in Unix
milliseconds.

| type | Who sends it and when | data |
|---|---|---|
| `hello` | server, right after connecting | `{app, version, protocol, streams}` |
| `metrics` | server, every 500 ms | server metrics, example below |
| `ssh.session` | server, when an SSH session opens or closes | `{action: opened\|closed, session_id, host, port, username, connection_id, reason?}` |
| `worker.status` | server, when the worker state changes | `{status, ...}`: `connected`, `connection_lost`, `worker_exited`, `worker_started` |
| `sftp.transfer` | server, while a file transfer runs | transfer state: `{id, status, bytes_done, bytes_total, percent, speed, ...}` (see "Files on servers") |
| `plugin.status` | server, when a plugin was enabled, disabled or failed | `{name, status, version, error}` (see "Plugins at runtime") |
| `alert` | server, when an alert fires, changes, resolves or is acknowledged | `{action: fired\|updated\|resolved\|acknowledged, alert}` (see "Alerts") |
| `ping` | UI | anything, comes back in `pong` |
| `pong` | server, in reply to `ping` | `data` from `ping` |
| `error` | server, if it could not parse a UI message | `{error, detail}`: `bad_message` or `unknown_type` |

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

`ssh.session` also comes when the session was closed by another client or lost: `reason` is
`closed` (closed through the API), `worker_connection_lost` (the worker crashed or its
connection dropped), `lost_in_worker`, `exec_timeout` (a command did not answer even after
the limit, see 504 below), `shutdown` (the backend is stopping). The UI updates its session
list from it, even if the session terminal is not open. `worker.status` shows the core
state: for example, after `worker_exited` the backend restarts the worker (`worker_started`
with `restarts`), then `connected`.

#### Monitoring (plugin `monitor`)

- `servers`: one item per server (`host:port`) with open SSH sessions, however many sessions
  it has. Machine metrics do not depend on the session, so a server is polled once per period
  (`MONITOR_POLL_INTERVAL`, 0.5 s) with the worker action `monitor_get_values` through one of
  its sessions. Different servers are polled in parallel.
- `cpu.percent`: load since the previous sample; `memory`: bytes and percent. `avg`, `min`,
  `max` are percentages over the `window_s` window (`MONITOR_WINDOW`, 30 s).
- `network`: the server's network, summed over all interfaces except `lo`. `rx` is receive,
  `tx` is transmit. `total`: byte counter since the server booted; `speed`: bytes per second
  since the previous sample (`null` while there is only one sample); `avg`, `min`, `max`:
  `speed` over the window. Megabits per second: `speed * 8 / 1e6`. On a server with Docker,
  container traffic is counted twice (the `docker0` bridge and `veth*`). `network: null`: the
  server has only `lo`, or the worker build is older than 2026-11-17.
- `disk`: the root partition `/` (bytes, `percent` as in `df`), one value for the "Disk" card.
  It comes with every sample, together with CPU and memory. With a worker build older than
  2026-11-17 it is taken from `disks` and updated together with them.
- `disks` and `gpus` are updated less often, every `MONITOR_HARDWARE_INTERVAL` (10 s): the
  worker does not provide them, so the backend runs `df -kP` and `nvidia-smi` on the server
  through the same free session (this does not show up in the session terminal). `disks`:
  real `/dev/...` devices (without snap loop images) and the root `/` (it comes first), each
  device once; `percent` as in `df`. `gpus`: NVIDIA only, values the card does not report come
  as `null`; `[]` means no cards. Before the first sample both fields are `null`, the sample
  time is in `hardware_updated_at`.
- `status`:
  - `waiting`: no samples yet;
  - `warming_up`: memory is already there, CPU appears from the second sample. The worker
    counts the first sample of a session from the server boot, so it is dropped;
  - `ok`: fresh data;
  - `stale`: no fresh data for longer than `MONITOR_STALE_AFTER` (3 s), the values are the
    last known ones;
  - `error`: the last poll failed, the text is in `error` (for example, the server has no
    `/proc`: metrics work on Linux only).
- Polling does not wait for user commands: the worker handles the requests of one session one
  by one, so a server is polled through a free session. If all sessions of the server are
  busy with commands, the message has `busy: true`, and after 3 s the status becomes `stale`.
  Open one more session to the server so that metrics keep flowing during long commands.
- Servers are polled only while someone needs the metrics: a UI is connected to `/ws`, or
  `GET /api/monitor/metrics` was called in the last `MONITOR_DEMAND_TIMEOUT` (10) seconds.
- `WS_FAKE_METRICS=true` (default `false`) sends test metrics in the same format to `/ws`
  (`source: "fake"`, one server `demo.synex.local:22`), so the UI can be built without an SSH
  server. Real metrics are then available through REST only.
- Every client has its own queue of 64 messages. If the UI cannot keep up, the oldest
  messages are dropped, and memory does not grow.
- On shutdown the backend closes the connection with code 1012: the UI should reconnect.

#### Alerts (plugin `alerts`)

Alerts of all servers with open SSH sessions, from the same metrics the UI sees. The
rules are shared by all servers:

| metric | default | what is checked |
|---|---|---|
| `cpu` | warning 90 %, critical 95 %, 60 s | CPU load |
| `memory` | warning 90 %, critical 95 %, 60 s | memory usage |
| `disk` | warning 90 %, critical 95 %, at once | every partition from `disks` separately (`target` is the mount) |
| `gpu_temperature` | warning 80 °C, critical 90 °C, 60 s | every NVIDIA card (`target` is its index) |
| `server` | 30 s | the server does not respond: the poll fails, or there is no fresh data although its sessions are free |

- A value must stay above the threshold for `duration_s` seconds: a short CPU spike gives no
  alert. An alert goes out when the value falls 5 points below the warning threshold (so a
  value around the threshold does not blink); critical turns into warning 5 points below
  the critical threshold. While a server does not respond, its other metrics are not
  checked (the values are old).
- `GET /api/alerts`: `{counts: {active, critical, warning, unacknowledged, resolved},
  alerts: [...]}`, active first (critical above), then resolved, newest first. An alert:
  `{id, server, host, port, metric, target, severity, status, value, peak, threshold, title,
  message, started_at, updated_at, resolved_at, resolved_reason, acknowledged}`; `title` is
  the card heading (`CPU load above 95%`, `Disk /data above 90%`,
  `Server does not respond`), `message` the details under it; `resolved_reason`:
  `back_to_normal`, `server_disconnected` (the last session to it was closed),
  `rule_changed` (its rule was turned off).
- `/ws` gets `alert` `{action, alert}` when an alert fires, changes its level, resolves or is
  acknowledged. The UI keeps the list from `GET /api/alerts` and updates it from these
  events.
- `POST /api/alerts/{id}/ack` marks an alert as seen (for the badge); when its level changes,
  it becomes unacknowledged again. Resolved alerts are kept in memory (`ALERTS_HISTORY`,
  200) until a restart.
- `PUT /api/alerts/rules` `{"rules": [{"metric": "cpu", "warning": 80, "critical": 95,
  "duration_s": 30, "enabled": true}]}` replaces the rules (a metric that is not listed keeps
  its default) and saves them to `alerts.json` next to `keys.json` (`ALERTS_PATH`);
  `DELETE /api/alerts/rules` returns the defaults.
- The monitor polls servers only while someone watches the metrics. So that alerts work with
  the Dashboard closed, the plugin keeps the polling on (`ALERTS_KEEP_POLLING=true`).

Quick check: open http://127.0.0.1:8000/api/health and run in the browser console

```js
const ws = new WebSocket("ws://127.0.0.1:8000/ws");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

### Errors

All errors come as `{"error": "<code>", "detail": "<text>"}` (some have extra fields with
details, for example `host_key`); validation errors as 422:

| HTTP | error | When |
|---|---|---|
| 404 | `key_not_found` | No key with this id |
| 404 | `connection_not_found` | No saved connection with this id |
| 409 | `connection_exists` | A connection with this name already exists |
| 422 | `invalid_connection` | The connection's login method is inconsistent |
| 422 | `connection_secret_required` | The connection password is not saved: pass it to `/connect` |
| 500 | `connections_corrupted` | `connections.json` is damaged |
| 404 | `ssh_session_not_found` | No session with this `session_id` |
| 409 | `ssh_not_connected` | No open session, or the worker no longer knows it |
| 403 | `forbidden_origin` / `forbidden_host` | A request from a page not in `ALLOWED_ORIGINS` or with a foreign `Host` |
| 409 | `key_exists` | The name is taken or this key is already saved |
| 409 | `ssh_session_busy` | A plugin command did not get the session in time (`SSH_SERVICE_WAIT`) |
| 422 | `ssh_interactive_command` | The program needs a terminal (vim, top, tmux): `program` and `hint`; `allow_interactive: true` sends it anyway |
| 409 | `ssh_interrupt_unavailable` | The command cannot be interrupted: the worker gives no second connection, or `track_cwd: false` |
| 429 | `ssh_too_many_sessions` | `SSH_MAX_SESSIONS` sessions are open |
| 422 | `invalid_key` / — | The key cannot be read, a passphrase is needed, wrong fields |
| 500 | `keystore_corrupted` | `keys.json` is damaged or encrypted with another master key |
| 502 | `ssh_connect_failed` | The worker could not connect (host, key, password, passphrase) |
| 409 | `ssh_host_key_unknown` | The server key is not confirmed yet: the fingerprint is in `host_key` (see "Server key verification") |
| 409 | `ssh_host_key_mismatch` | The server key has changed: `host_key.fingerprint` and `expected_fingerprint`, no connection |
| 409 | `ssh_host_key_revoked` | The server key is revoked (`@revoked`) in the OpenSSH `known_hosts` on this computer, no connection |
| 409 | `ssh_known_host_conflict` | Another fingerprint is saved for the server: replace only with `replace: true` |
| 404 | `ssh_known_host_not_found` | No saved key for the server |
| 500 | `known_hosts_corrupted` | `known_hosts.json` is damaged |
| 504 | `ssh_connect_timeout` | The server did not answer within `SSH_CONNECT_TIMEOUT` (20 s): address, port, firewall |
| 502 | `worker_command_failed` | The worker returned an error |
| 503 | `plugin_unavailable` | The plugin is enabled but did not load (the reason is in `/api/plugins`) |
| 404 | `plugin_disabled` | The plugin is disabled: its endpoints are closed until `POST /api/plugins/{name}/enable` |
| 404 | `plugin_not_found` | No such plugin, or it cannot be imported |
| 409 | `plugin_in_use` | Cannot disable: enabled plugins depend on it (`?cascade=true` disables them too) |
| 503 | `worker_unavailable` | The worker is not running or IPC is unavailable |
| 503 | `keystore_unavailable` | No access to the Credential Manager / wrong `KEYSTORE_MASTER_KEY` |
| 504 | `ssh_exec_timeout` | The worker did not return the command result |
| 504 | `worker_timeout` | The worker did not answer another request |
| 403/404/502/503/504 | `docker_*` | Docker errors on the server (see "Docker on servers") |
| 404/409/502/504 | `git_*` | Git errors on the server (see "Git on servers") |
| 404/409/500 | `alert_*` | `alert_not_found`, `alert_active` (an active alert cannot be removed), `alert_rules_corrupted` (`alerts.json` is damaged) |
| 403/404/422/502/504 | `logs_*`, `log_file_*` | Server log errors (see "Server logs") |
| 403/404/409/422/5xx | `sftp_*` | File errors on the server and on this computer (see "Files on servers") |

## Structure

```
app/
  main.py              entry point (uvicorn)
  desktop.py           the app: backend, worker and UI in a window (packaging/ builds it)
  core/                platform: config, app factory, errors, event bus, logging,
                       files (atomic writes of files with secrets)
    target/release/    workers for Windows x64, Linux x64, macOS Intel/ARM64
  worker/              Rust core: client.py (IPC protocol), manager.py (process),
                       executable.py (finding the binary for the OS and CPU),
                       unix_socket.py (socket path and folder on Linux/macOS),
                       process.py (worker process on Linux/macOS)
  keystore/            key vault: store.py, ssh_keys.py, master_key.py
  api/                 root router /api, /api/health, dependencies
  ws/                  WebSocket /ws: hub.py (protocol, broadcast), metrics.py (metrics
                       format and the test source)
  plugins/             plugin manager (manager), the user's choice between runs (choices)
    ssh/               /api/ssh/*: manager (session manager), session (a session and its
                       worker connection), buffer (output buffer), ws (terminal),
                       wrapper (cwd, timeout), known_hosts (trusted server keys),
                       openssh_hosts (keys from the OpenSSH known_hosts), interrupt
                       (Ctrl+C through a second connection), terminal (programs that need
                       a terminal, history, completion), routes, schemas, errors
    monitor/           /api/monitor/*: service (polling and publishing), aggregator
                       (window, statuses), values (worker response), hardware (disks, GPU)
    logs/              /api/logs/*: journal (journalctl commands and JSON parsing), files
                       (log file scripts), service (session choice, errors), stream (live
                       log WebSocket), routes
    alerts/            /api/alerts/*: rules (thresholds, alerts.json), engine (when an
                       alert fires and resolves), service (history, /ws events), routes
    keys/              /api/keys/*
    connections/       /api/connections/*: store (connections.json), routes, schemas
    docker/            /api/docker/*: commands (docker commands and output parsing),
                       service (running in a session, errors), logs (live log over
                       WebSocket), routes, schemas
    git/               /api/git/*: commands (git commands and output parsing), service,
                       routes, schemas
    sftp/              /api/sftp/*: remote (server files through the worker's SFTP),
                       transfers (transfers, progress, verification), folders (a whole
                       folder), tree (walking a folder), local (files of this computer),
                       search (find on the server, walking folders here), paths,
                       routes, schemas
tests/                 pytest; fake_worker.py imitates synex_worker
  integration/         the full cycle UI → FastAPI → worker → SSH server
```

New functionality is added as a plugin: `app/plugins/<name>/plugin.py` with a `BasePlugin`
subclass and a `router`. By default (`PLUGINS_ENABLED=*`) all installed plugins are enabled;
a comma-separated list enables only the listed ones, the rest show up in `/api/plugins` with
the status `disabled`.

* `requires = ("ssh",)`: plugins this one cannot work without. They are enabled
  automatically (even if they are not in `PLUGINS_ENABLED`) and loaded first.
* A plugin is skipped with a warning in the log if it is not installed, cannot be imported or
  a dependency is unavailable; the others keep working.
* If a plugin's `on_load` fails, it and the plugins depending on it are not loaded, and the
  backend keeps running without them. Their endpoints (and WebSockets) answer 503
  `plugin_unavailable`.

`GET /api/plugins` shows the UI what works:

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

`status`: `loaded` works; `failed` is enabled, but `on_load` failed or a dependency did not
load; `skipped` is not installed, cannot be imported or lacks a dependency; `disabled` is
installed but turned off (not in `PLUGINS_ENABLED` or via `/disable`, including in a previous
run); `registered` is not loaded yet (the app is starting).

### Plugins at runtime (data bus)

Modules are enabled and disabled on a running backend, without a restart:

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

Response: `{"changed": ["docker"], "plugins": [...]}` (`changed`: what was enabled or
disabled just now, `plugins`: as in `GET /api/plugins`).

- Enabling calls `on_load` of the plugin and its dependencies and opens its endpoints. A
  package that appeared in `app/plugins/` after the backend started is picked up too. If
  `on_load` fails, the answer is 503 `plugin_unavailable` with the reason, the rest keep
  working.
- Disabling calls `on_unload`: the plugin stops its work (`ssh` closes all sessions, `sftp`
  cancels transfers, `docker` closes live logs, `monitor` stops polling servers). Its
  endpoints (HTTP and WebSocket) then answer 404 `plugin_disabled` and disappear from
  `/api/docs`.
- If enabled plugins depend on it (all server modules depend on `ssh`), the answer is 409
  `plugin_in_use`; `?cascade=true` disables them first, then it. Enabling a plugin also
  enables what it depends on.
- Every state change goes to the bus as a `plugin.status` event, and the hub forwards it to
  `/ws`: `{"name": "docker", "status": "disabled", "version": "0.2.0", "error": null}`.
  Events a plugin declared in `BasePlugin.events` (`ssh.session`, `sftp.transfer`) go to
  `/ws` while the plugin is enabled.
- A plugin's `on_load` and `on_unload` must survive repeated calls: a plugin can be disabled
  and enabled any number of times.

The set of plugins survives a backend restart. What the user enabled and disabled at runtime
is written to `plugins.json` next to `keys.json` (`PLUGINS_STATE_PATH`) and applied on top of
`PLUGINS_ENABLED` at the next start:

```json
{"version": 1, "plugins": {"docker": false, "extra": true}}
```

- The file holds only the differences from the settings: `false` for a plugin that is in
  `PLUGINS_ENABLED` but disabled by the user; `true` for a plugin that is not there but the
  user enabled it. Switched back to the setting: the entry disappears. So a change of
  `PLUGINS_ENABLED` applies to everything the user has not switched themselves.
- A plugin needed by an enabled one is enabled as a dependency, even if it was disabled
  before. A plugin the user enabled that failed to start tries to start again after a restart
  (and again shows the reason in `/api/plugins`).
- No file or a damaged file: the set from `PLUGINS_ENABLED` works, with a log line about the
  reason. The file could not be written: the choice lasts until a restart.
- `PLUGINS_REMEMBER=false` turns remembering off: after a restart exactly what is in
  `PLUGINS_ENABLED` is enabled. You can also return to the set from the settings by deleting
  `plugins.json`.

## Tests

**PowerShell** (Windows):

```powershell
uv run pytest
```

**bash** (Linux, macOS):

```bash
uv run pytest
```

Tests use a temporary vault and their own master key: they do not touch the real `keys.json`
or the Credential Manager. They do not depend on `backend/.env` either: tests set the plugin
set and the metrics source themselves.

Tests run on Windows, Linux and macOS. On Linux and macOS the fake worker runs SSH commands
with a real `sh -c`, and on Windows with a shell emulator (`tests/fake_shell.py`): a virtual
file system with `/tmp` and `/home/synex` and exactly the commands the tests need. Server
programs such as `docker` and `git` are plugged into the emulator by the tests
(`FakeShell.programs`). `tests/test_fake_shell.py` checks that the emulator answers the same
way as a real sh. The emulator can be enabled explicitly on any OS (on Windows it is always
on), for example to run the Windows variant of the tests on Linux/macOS:

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

Only tests that need a specific OS are skipped: the command wrapper in real
sh/bash/dash/zsh and file permissions (Linux/macOS), the Unix socket and the worker process
(Linux/macOS), the timeout through coreutils `timeout` (macOS does not have it).

GitHub Actions (`.github/workflows/backend.yml`) runs `ruff check`, `ruff format --check` and
pytest on Windows, Linux and macOS on every push to `main` and every pull request with
changes in `backend/`, and once more on Linux with `SYNEX_TEST_SHELL=emulated`. The same
locally before a push:

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

`tests/test_real_worker.py` starts the real worker: `synex_worker.exe` on Windows, the
`synex_worker` build on Linux/macOS (found the same way as when the backend starts) or the
command from `SYNEX_TEST_WORKER` (for example, a script that runs the exe through wine).
Checks against a real SSH server are enabled with environment variables:

**PowerShell** (Windows):

```powershell
$env:SYNEX_TEST_SSH_HOST = "1.2.3.4"
$env:SYNEX_TEST_SSH_USER = "root"
$env:SYNEX_TEST_SSH_KEY = "C:/Users/me/.ssh/id_ed25519"
$env:SYNEX_TEST_SSH_PASSWORD = "..."   # optional: check password login
uv run pytest tests/test_real_worker.py -v
```

**bash** (Linux, macOS):

```bash
export SYNEX_TEST_SSH_HOST=1.2.3.4
export SYNEX_TEST_SSH_USER=root
export SYNEX_TEST_SSH_KEY=~/.ssh/id_ed25519
export SYNEX_TEST_SSH_PASSWORD=...      # optional: check password login
uv run pytest tests/test_real_worker.py -v
```

### Integration tests: the full cycle

`tests/integration/` checks the whole chain **UI → FastAPI → synex_worker → SSH server →
answer in the UI**: the backend runs on uvicorn with the real worker, and the client behaves
like the UI: REST and both WebSockets (`/ws` and the session terminal) with the UI page's
`Origin` header. What is checked:

- terminal: a command over WebSocket, output, stderr, exit code, cwd, the timeout on the
  server, closing a session reaches the terminal and `/ws`;
- server metrics in `/ws`: one server for all its sessions, it disappears with the last one;
  network and the `/` partition from the worker, the received bytes counter does not
  decrease;
- two sessions run commands in parallel;
- login with an encrypted key, with a key from the vault, with a password; login and
  connection errors reach the UI as 502, a silent server as 504 `ssh_connect_timeout`;
- a command that reads input (`cat`, `read`) does not wait until the timeout;
- a deleted session folder: the command is not run in the home folder;
- reconnecting the terminal with `since`: the UI gets the missed output in order;
- a worker crash: the terminal and `/ws` learn about the lost session, the backend restarts
  the worker, a new session works;
- the server key (TOFU): the first connection answers 409 with a fingerprint the worker
  computed the same way as `ssh-keygen -lf` from the sshd key; after confirmation the session
  opens; a foreign fingerprint gives 409 `ssh_host_key_mismatch` without a retry, replacing
  only with `replace`. The other tests confirm their sshd key in advance (the `ui` fixture),
  for an external server the first connection shows the fingerprint;
- a server recorded in the OpenSSH `known_hosts` connects without confirmation, and another
  key of the same type in that file gives 409 `ssh_host_key_mismatch`;
- all plugins load (`PLUGINS_ENABLED=*`); a saved connection opens a session, and
  `connection_id` reaches `/ws`;
- milestone B4: SSH, SFTP, monitoring and Docker are disabled and enabled on a running
  backend, the UI sees it in `/ws`, the API of a disabled module answers 404, after enabling
  everything works again (disabling `ssh` closes the sessions, a new one opens);
- the whole MVP scenario (`test_mvp_scenario.py`): "connect, transfer a file, watch the
  metrics, restart a container", checked against the project charter criteria: an SSH
  connection within 3 s, a 100 MB file without errors, metrics at least once a second and no
  older than 500 ms, restart, stop, start and logs of three containers. With `-s` the test
  prints a summary with the measurements;
- git on the server: a repository behind origin, `status`, `fetch` and a deploy (`pull` and a
  command in the repository folder), the session terminal does not see the service commands;
- docker on the server: a container from an image built from an empty archive (no download),
  the list, logs, a start error and removal. Needs a running Docker, otherwise the test is
  skipped;
- the MVP criterion for Docker: three running containers (the image is built from `sh` and
  `sleep` of this machine, without network): logs of each, a live log over WebSocket without
  repeats and with a reconnect, `restart`, `stop`, `start`, `stats`;
- files on the server (the worker's SFTP): folders, names with spaces, quotes and Cyrillic,
  upload and download in a request body, copying and deleting folders, a listing of fifteen
  hundred files with Russian names;
- the MVP criterion for SFTP: a 100 MB file goes to the server and comes back, SHA-256
  matches, progress goes to `/ws`, and a terminal command passes during the transfer. The size
  is set by `SYNEX_TEST_SFTP_MB`; the test prints the time and speed (`-s`). On a local `sshd`
  it takes about 1 s each way;
- a whole folder: more than a hundred files, nested and empty folders, names with spaces,
  quotes and Cyrillic go to the server and come back unchanged; merging with an existing
  folder; a link on the server is skipped; a cancel in the middle of a big file leaves
  neither the folder nor temporary files on the server and on this computer.

They need a worker (as for `test_real_worker.py`) and an SSH server. If `SYNEX_TEST_SSH_*` is
not set, on Linux and macOS the tests start their own `sshd` on `127.0.0.1` with a random
port and their own keys (login as the current user, the system is not changed). Without a
worker or an SSH server the tests are skipped.

**PowerShell** (Windows):

```powershell
uv run pytest -m integration            # integration tests only
uv run pytest -m "not integration"      # everything else
```

**bash** (Linux, macOS):

```bash
uv run pytest -m integration            # integration tests only
uv run pytest -m "not integration"      # everything else
SYNEX_TEST_WORKER=./run_worker.sh uv run pytest -m integration   # exe through wine
```

In GitHub Actions they run in a separate `integration` job, twice: with the Linux worker from
the repository and with a worker built from `core/` (`cargo build --release`), against `sshd`
on the runner. That is why the workflow also runs on changes in `core/`: a change to the Rust
code is checked together with the backend right away. The regular `test` jobs run with
`-m "not integration"`.

`test_real_worker_raw_exit_codes` checks the exit codes of the worker itself (fixed in the
2026-11-14 build); with an older build, where they are always 0, the test is `xfail`.
`/api/ssh/exec` with `track_cwd=true` gets the exit code from the shell anyway.

Since 2026-11-14 the worker connects only with a confirmed server key: tests with
`SYNEX_TEST_SSH_*` do what the UI does: the first connection learns the fingerprint
(`host_key_unknown`), the second one uses it. The tests never turn the check off in the
worker.

`tests/fake_worker.py` reproduces the behaviour of worker builds 2026-11-03 … 2026-11-17: a
session = the request `id`, sessions belong to a connection, the requests of a connection
run one by one, `exit_code` is always 0, server key verification (`host_key_mode`), SFTP of
two builds (`sftp_build`), network and the `/` partition in metrics (`monitor_build`).
`tests/ssh_helpers.py` builds the ssh plugin on top of it and runs uvicorn for the WebSocket
tests. `tests/worker_host.py` is a separate "backend" process for the tests where it gets
killed with `kill -9`.
