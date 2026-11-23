# Synex Worker — Usage Documentation

> Русская версия: [README.ru.md](README.ru.md)

## 1. Overview

**Synex Worker** (`synex_worker.exe` / `synex_worker`) is a Rust binary — the SSH/SFTP core of the Synex application. It runs as a child process of the Python backend and talks to it over IPC (inter-process communication).

**Key properties:**

- At startup the binary **does nothing** except open the IPC channel.
- An SSH connection is established **only after an `ssh_connect` command** from Python.
- One SSH session is reused for all commands (no re-authentication).
- The server key is verified using **TOFU** (trust on first use, then compare the fingerprint), see section 5.1.
- A command's return code (`exit_code`) comes from the SSH protocol; if the process is killed by a signal it is `128 + signal number`.
- All errors are returned in the response; the binary must not panic.

Not implemented yet (SFTP progress frames, SFTP channel reuse, folder transfer, Docker), see section 9.

## 2. Platform support and transport

The binary picks the transport automatically, based on the OS:

| Platform          | Primary transport  | Environment variable   | Fallback                            |
| ----------------- | ------------------ | ---------------------- | ----------------------------------- |
| **Windows**       | Named Pipe         | `RUST_IPC_PIPE_NAME`   | TCP on `RUST_IPC_TCP_FALLBACK_PORT` |
| **Linux / macOS** | Unix Domain Socket | `RUST_IPC_SOCKET_PATH` | TCP on `RUST_IPC_TCP_FALLBACK_PORT` |

**Initialization order:**

1. The binary reads the environment variables.
2. It tries to open the primary transport for the current OS.
3. If that fails, it falls back to TCP `127.0.0.1:<port>`.

The Unix socket is created with `0600` permissions: only the owner can connect.

## 3. Environment variables

All variables are **optional**. Defaults are used when a variable is not set.

| Variable                     | Description                    | Default                                      |
| ---------------------------- | ------------------------------ | -------------------------------------------- |
| `RUST_BRIDGE_MODE`           | Operation mode (always `ipc`)  | `ipc`                                        |
| `RUST_IPC_SOCKET_PATH`       | Unix socket path (Linux/macOS) | `/tmp/synex_worker.sock`                     |
| `RUST_IPC_PIPE_NAME`         | Named Pipe name (Windows)      | `\\.\pipe\synex_worker`                      |
| `RUST_IPC_TCP_FALLBACK_PORT` | TCP fallback port              | `9100`                                       |
| `RUST_EXECUTABLE_PATH_UNIX`  | Binary path on Unix            | `./app/core/target/release/synex_worker`     |
| `RUST_EXECUTABLE_PATH_WIN32` | Binary path on Windows         | `./app/core/target/release/synex_worker.exe` |

**Example setup in Python:**

```python
import os

env = os.environ.copy()
env["RUST_IPC_PIPE_NAME"] = r"\\.\pipe\synex_worker"
env["RUST_IPC_TCP_FALLBACK_PORT"] = "9100"
env["RUST_EXECUTABLE_PATH_WIN32"] = "./app/core/target/release/synex_worker.exe"
```

## 4. IPC protocol

### 4.1. Physical layer: framing

Every message is **length-prefixed**:

```
[4 bytes: payload length in Big-Endian] [N bytes: MessagePack payload]
```

**Why:** MessagePack is a binary format, and its payload may contain `0x0A` (newline) and `0x00` bytes. Splitting messages by `\n` is not possible.

**Frame size limit:** `16 MiB` (`MAX_FRAME_LEN` in `synex_worker/src/main.rs`). If the header announces a larger size, the worker:

1. replies with `{"id": "", "status": "error", "payload": {"stderr": "frame too large: N bytes", ...}}`;
2. closes the IPC connection.

The frame body is not read. After such a reply the connection has to be reopened.

### 4.2. Data format: MessagePack

MessagePack is used in **map mode** (a dictionary):

- On the Rust side: `rmp_serde::to_vec_named(&resp)` — a dictionary with field names.
- On the Python side: `msgpack.unpackb(data, raw=False)` — a `dict` with `str` keys.

⚠️ **If you use `to_vec` (without `_named`)**, Python receives a list instead of a dict and `response["status"]` fails.

### 4.3. Request schema (Python → Rust)

```json
Request {
    id: String,           // request ID. For ssh_connect it becomes the session ID
    action: String,       // action type
    payload: { ... }      // depends on the action
}
```

Common `payload` fields used by actions: `command`, `connection`, `remote_path`, `local_path`, `destination_path`, `target_id` (for `cancel` only). Action-specific fields are described in section 5 and section 8.

### 4.4. Response schema (Rust → Python)

```json
Response {
    id: String,           // same ID as in the request
    status: String,       // "ok", "error" or "cancelled" (see below)
    payload: {
        stdout: String,   // action output
        stderr: String,   // error text; for errors it starts with a code (see section 7)
        exit_code: i32    // command return code (0 = success)
    }
}
```

**Meaning of `status`:**

- `"ok"` — the action was performed. For `ssh_exec` this only means SSH worked; the actual command result is in `exit_code`. A non-zero `exit_code` with `"ok"` is normal.
- `"error"` — the action was not performed (no session, bad payload, SFTP error, server key problem, etc.). The reason is in `stderr`.
- `"cancelled"` — the action was stopped by a `cancel` request (see 5.5). It is reported only by the action that was cancelled, under its original `id`.

The single success status across all actions (including SFTP) is `"ok"`.

**Several responses on one connection:** the worker reads IPC and runs actions independently. While `ssh_exec` or an SFTP transfer is still running, a `cancel` (or any other quick action) can already be sent over the same connection. Responses arrive as they become ready and are distinguished by `id`, so the client must be able to accept frames in any order.

## 5. Actions

### 5.1. `ssh_connect` — connect to a server

**Must** be sent first. Without a successful `ssh_connect` all other actions return the “Session dont founded” error.

**Request:**

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

**`connection` fields:**

| Field                  | Type        | Description                                                                                |
| ---------------------- | ----------- | ------------------------------------------------------------------------------------------ |
| `host`                 | string      | Server IP or domain name                                                                   |
| `port`                 | int         | SSH port (usually 22)                                                                      |
| `username`             | string      | User name                                                                                  |
| `key_path`             | string/null | Path to the **private** key on the worker's machine. Used instead of the password when set |
| `passphrase`           | string/null | Passphrase for the private key (only with `key_path`)                                      |
| `password`             | string/null | User password. Used when `key_path` is not set. Never logged                               |
| `host_key_fingerprint` | string/null | Trusted server key fingerprint as `SHA256:<base64>`. Optional, see the TOFU part below     |

If neither `key_path` nor `password` is set, the reply is `status: "error"` with `stderr` set to `Connection failed: SSH internal error: No authentication method provided`.

**Server key verification (TOFU).** The worker keeps no `known_hosts`; trusted fingerprints are stored by the backend and passed in `host_key_fingerprint`:

| Situation                            | Response                                                                                                                                                                                |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `host_key_fingerprint` is not passed | `status: "error"`, `stderr: "host_key_unknown: unknown host key SHA256:..."`, `payload` contains `fingerprint` and `key_type`. The connection is not continued                          |
| Passed and matches the server key    | The connection continues and `status: "ok"` is returned                                                                                                                                 |
| Passed but does not match            | `status: "error"`, `stderr: "host_key_mismatch: expected X, got Y"`, `payload` contains `fingerprint` (received) and `expected_fingerprint` (expected). The connection is not continued |

Backend flow:

1. First connection without a fingerprint → receive `host_key_unknown` with `fingerprint` and `key_type`.
2. Show the fingerprint to the user and store it after confirmation.
3. Repeat `ssh_connect` with `host_key_fingerprint`.
4. On `host_key_mismatch` do not continue; warn the user that the server key changed (possible man-in-the-middle attack, or the server was reinstalled).

**Successful response:**

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

On success `fingerprint` and `key_type` are always present. In error responses unrelated to key verification these fields are absent.

**Authentication or network failure:**

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

### 5.2. `ssh_exec` — run a command

**Request:**

```json
{
  "id": "sess-1",
  "action": "ssh_exec",
  "payload": {
    "command": "ls -la /tmp"
  }
}
```

**`command` field:**

- A plain command string. Chains are supported: `cd /tmp && ls -la`.
- The worker does not keep `cwd` between commands. If a working directory is needed, add `cd` to every command or track `cwd` on the Python side.

**Return code (`exit_code`):**

- Normal exit: `exit_code` equals the process code (`exit 3` → `3`, `false` → `1`).
- Process killed by a signal: `exit_code = 128 + signal number` (`kill -9 $$` → `137`, `kill -TERM $$` → `143`). The server must send `exit-signal`; this depends on sshd.
- `stderr` is delivered in `payload.stderr`, `stdout` in `payload.stdout`.

**Successful response:**

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

**The command failed** (for example, command not found). The status is `"ok"` because SSH itself worked:

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

**Session not found** (no `ssh_connect` with this `id`):

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

If `command` is missing: `stderr: "Missing 'command' in payload"`.

**Cancellation:** the command runs in its own task, so it can be stopped by a `cancel` action (see 5.5) with `target_id` set to this session's id. The worker sends `TERM` to the process, `KILL` 3 seconds later, and replies under the same `id` with `status: "cancelled"` and `exit_code: 143`.

### 5.3. `monitor_get_values` — server metrics

**Request:**

```json
{
  "id": "sess-1",
  "action": "monitor_get_values",
  "payload": {}
}
```

The worker runs a metrics-collection command on the server and parses its output. Metrics are returned as a **JSON string** in `payload.stdout`:

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

JSON fields in `stdout`:

| Field                  | Type        | Description                                                                                    |
| ---------------------- | ----------- | ---------------------------------------------------------------------------------------------- |
| `ok`                   | bool        | `true` if the metrics were parsed                                                              |
| `cpu_usage_percent`    | float       | CPU load in percent. `0.0` on the first call in a session, because there is no previous sample |
| `mem_total_kb`         | int         | Total memory, KiB                                                                              |
| `mem_available_kb`     | int         | Available memory, KiB                                                                          |
| `mem_used_kb`          | int         | Used memory, KiB                                                                               |
| `mem_usage_percent`    | float       | Memory usage in percent                                                                        |
| `net_rx_bytes_total`   | int/null    | Bytes received on all interfaces except `lo` (counter from `/proc/net/dev`)                    |
| `net_tx_bytes_total`   | int/null    | Bytes sent on all interfaces except `lo`                                                       |
| `net_rx_bytes_per_sec` | float/null  | Receive rate, bytes/s. Delta since the previous call in this session. `null` on the first call |
| `net_tx_bytes_per_sec` | float/null  | Send rate, bytes/s. Same rules, `null` on the first call                                       |
| `disk_total_kb`        | int/null    | Size of the root filesystem `/`, KiB (`df -P -k /`)                                            |
| `disk_used_kb`         | int/null    | Used space on `/`, KiB                                                                         |
| `disk_available_kb`    | int/null    | Available space on `/`, KiB                                                                    |
| `disk_usage_percent`   | float/null  | `/` usage in percent: `used / (used + available) * 100`, like the Capacity column in `df`      |
| `error`                | string/null | Parse error text when `ok` is `false`                                                          |

Notes:

- Disk usage is read with `df -P -k /`, not `df -h /`: `-h` output is human-readable and rounded. `-P` forces one line per filesystem in a fixed format.
- Network counters are summed over all interfaces except `lo`. On a host with Docker, traffic through bridges (`docker0`, `veth*`) is counted twice.
- Rates are computed from counter differences between requests in the same session. If a counter decreases (reboot or reset), the rate is `0`.

If the metrics cannot be parsed, `stdout` contains JSON with `"ok": false` and `"error": "..."`. `status` itself stays `"ok"`.

**Session not found:** `status: "error"`, `stderr: "Session dont founded by key. Create session first!"`.

### 5.4. `ssh_disconnect` — close a session

```json
{
  "id": "sess-1",
  "action": "ssh_disconnect",
  "payload": {}
}
```

- Success: `status: "ok"`, `stdout: "Disconnected id: sess-1"`.
- No such session: `status: "error"`, `stderr: "Session not found"`.
- If `ssh_exec` or an SFTP transfer is still running on the session, it is cancelled first (like `cancel`), and closing waits for it to finish.

### 5.5. `cancel` — stop the current operation

Stops the action currently running on a session: `ssh_exec`, `sftp_upload_file` or `sftp_download_file`. There is nothing else to cancel — all other actions are short and never sit in a cancellable state.

**Request:**

```json
{
  "id": "cancel-1",
  "action": "cancel",
  "payload": {
    "target_id": "sess-1"
  }
}
```

`target_id` is the session `id` (the same one used in `ssh_connect`). A session runs at most one operation at a time, so a separate operation id is not needed.

**Reply to `cancel` itself** (arrives immediately):

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

- `target_id` missing: `status: "error"`, `stderr: "Missing 'target_id' in payload"`.
- Nothing is running on that session: `status: "error"`, `stderr: "No cancellable operation for id: sess-1"`. This is a normal situation: the operation may have just finished.

**Reply from the cancelled operation** arrives separately, under its original `id`:

```json
{
  "id": "sess-1",
  "status": "cancelled",
  "payload": { "stdout": "", "stderr": "", "exit_code": 143 }
}
```

What cancellation actually does:

| Action                                    | What happens                                                                        |
| ----------------------------------------- | ----------------------------------------------------------------------------------- |
| `ssh_exec`                                | `TERM` is sent to the remote process, `KILL` 3 seconds later. `exit_code: 143`      |
| `sftp_upload_file` / `sftp_download_file` | Copying stops immediately and the files are closed. The partial file is not deleted |

Ordering matters: if cancellation arrives before the command starts (while it waits for the session), the command is not started at all. The worker reads IPC separately from actions, so `cancel` is received while a command or transfer is still running.

When the IPC connection is closed (the client disconnected), the worker cancels all actions of that connection and closes its sessions.

## 6. Python examples

A complete working client with tests lives in `core/rust_tests.py`. Below is the minimum required to talk to the worker.

### 6.1. Client (recommended)

Frame: 4 bytes of length (Big-Endian) + MessagePack. Writing and reading happen under one lock, because the socket is shared between threads.

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
                raise RuntimeError("worker closed the connection")
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

> Note: for `cancel` you need a client that can send a frame without waiting for a reply (see `send_only` / `recv_frame` in `rust_tests.py`), because the reply to the original request only arrives after the operation finishes.

### 6.2. Using TOFU

```python
import os

client = SynexClient()
host = os.environ["SSH_TEST_HOST"]
user = os.environ.get("SSH_TEST_USER", "root")
password = os.environ["SSH_TEST_PASS"]

# 1. First connection: no fingerprint yet
resp = client.connect_ssh("sess-1", host, user, password=password)
if resp["status"] == "error" and resp["payload"]["stderr"].startswith("host_key_unknown"):
    fingerprint = resp["payload"]["fingerprint"]
    # Show the fingerprint to the user and wait for confirmation.
    # Store it in your own storage (in the backend).
    resp = client.connect_ssh("sess-1", host, user, password=password,
                              host_key_fingerprint=fingerprint)

if resp["status"] == "ok":
    print(client.ssh_exec("sess-1", "uname -a")["payload"]["stdout"])
    client.disconnect_ssh("sess-1")
```

### 6.3. Windows transport (Named Pipe)

On Windows a file named `\\.\pipe\synex_worker` is opened instead of an `AF_UNIX` socket, and `send` works the same way, but through the file object's `read`/`write`. `rust_tests.py` has a `transport == "pipe"` branch for this.

## 7. Error handling

All errors are returned in a response with `status: "error"`; the reason is in `payload.stderr`. If an error has a code, the code is at the start of the string, followed by `: `.

### IPC-level errors

| Message                           | Cause                                                           | What to do                                  |
| --------------------------------- | --------------------------------------------------------------- | ------------------------------------------- |
| `frame too large: N bytes`        | Frame header exceeds `16 MiB`. The worker closes the connection | Reconnect, send a smaller request           |
| `Unknown action: <name>`          | Unknown `action` value                                          | Check the action name in sections 5 and 8.4 |
| `Missing 'connection' in payload` | `ssh_connect` has no `connection`                               | Pass `connection`                           |

If a frame cannot be unpacked as MessagePack, the worker replies with `id: "bad_msgpack"` and `stderr: "Failed to unpack: ..."`; the connection stays open and the worker continues with the next frame.

### SSH errors

| Message (stderr)                                                           | Cause                                                      | What to do                                                            |
| -------------------------------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------------------------------------- |
| `Session dont founded by key. Create session first!`                       | No session with this `id` (no `ssh_connect`, or it failed) | Run `ssh_connect` with this `id`                                      |
| `host_key_unknown: unknown host key SHA256:...`                            | `host_key_fingerprint` was not passed                      | Show the fingerprint to the user, retry with it                       |
| `host_key_mismatch: expected X, got Y`                                     | The server key does not match the stored one               | Do not connect. Check whether the server changed or is being attacked |
| `Connection failed: SSH authentication failed: access denied`              | The server rejected the key or password                    | Check the login, password, key and `passphrase`                       |
| `Connection failed: SSH internal error: No authentication method provided` | Neither `key_path` nor `password` was passed               | Pass one of them                                                      |
| `Connection failed: ...`                                                   | Network, port, SSH timeout                                 | Check `host`, `port` and server reachability                          |
| `Session not found`                                                        | `ssh_disconnect` for an unknown `id`                       | Nothing, the session is already closed                                |

`ssh_connect` errors not listed in the table come with the `Connection failed: ` prefix (network, port, timeout). `ssh_exec` and SFTP errors come as `SSH protocol error: ...` (a russh error) or `SSH internal error: ...`. SFTP errors additionally start with an `sftp_*` code.

### Command execution outcomes

- The command exited with a non-zero code: `status: "ok"`, `exit_code` = process code. This is not a worker error.
- The process was killed by a signal: `exit_code = 128 + signal number` (137 for `SIGKILL`, 143 for `SIGTERM`). The server must send `exit-signal`; if sshd does not send it, the code will be `0`.
- `ssh_exec` errors for a missing session or missing `command` are described above.

## 8. SFTP: payload structure and examples

### 8.1. RequestPayload structure

All SFTP actions read their fields from the common `payload`:

```rust
struct RequestPayload {
    command: Option<String>,
    connection: Option<ConnectPayload>,
    remote_path: Option<String>,
    local_path: Option<String>,
    destination_path: Option<String>,
    target_id: Option<String>, // cancel only
}
```

`ConnectPayload` contains `host`, `port`, `username`, `key_path`, `passphrase`, `password`, `host_key_fingerprint`.

### 8.2. Session = request ID

SFTP actions use the same SSH session as `ssh_exec`. The `id` in an SFTP request is the session `id` obtained from `ssh_connect`.

If the session does not exist, the response is `status: "error"`, `stderr: "Session dont founded by key. Create session first!"`.

Every SFTP action opens its own SFTP channel and closes it when done. Channel reuse is not implemented yet (see section 9).

### 8.3. SFTP actions and examples

**List a directory** (`sftp_list_dir`, payload `remote_path`):

```json
{
  "id": "sess-1",
  "action": "sftp_list_dir",
  "payload": { "remote_path": "/root/app" }
}
```

Response: `stdout` is a JSON array of entries (see 8.4).

**Stat a single path** (`sftp_stat`, payload `remote_path`). The path is checked with `lstat`, symlinks are not followed. `stdout` is JSON for a single entry:

```json
{
  "id": "sess-1",
  "action": "sftp_stat",
  "payload": { "remote_path": "/root/app/link" }
}
```

**Upload a file** (`sftp_upload_file`, payload `local_path`, `remote_path`). If `remote_path` ends with `/` or points to an existing directory, the file is placed inside it under its own name:

```json
{
  "id": "sess-1",
  "action": "sftp_upload_file",
  "payload": { "local_path": "C:/data/report.txt", "remote_path": "/root/app/" }
}
```

**Download a file** (`sftp_download_file`, payload `remote_path`, `local_path`). Same rule: if `local_path` is an existing directory, the file is saved into it under its own name.

**Move** (`sftp_move_file`, `remote_path` as source, `destination_path` as target). If the target already exists, `sftp_exists` is returned.

**Move a directory** (`sftp_move_dir`, `remote_path` as the source directory, `destination_path` as the new path). Unlike `sftp_move_file`, the source must be a directory: a file or a symlink (even to a directory) returns `sftp_not_a_directory`. A missing source is `sftp_not_found`, an existing target is `sftp_exists`. The directory contents move as a whole, via `rename` on the server.

Example:

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

**Copy** (`sftp_copy_file`, payload `remote_path`, `destination_path`). SFTP cannot copy directly, so the worker runs `cp -- SRC DST` on the server. A non-zero `cp` exit code is returned as an error with the stderr text.

**Create and remove:** `sftp_create_dir`, `sftp_remove_file`, `sftp_remove_dir` (payload `remote_path`). `sftp_remove_dir` removes only an empty directory.

### 8.4. SFTP action table

| Action               | Payload                                              | Successful `stdout`   |
| -------------------- | ---------------------------------------------------- | --------------------- |
| `sftp_list_dir`      | `remote_path`                                        | JSON array of entries |
| `sftp_stat`          | `remote_path`                                        | JSON for one entry    |
| `sftp_create_dir`    | `remote_path`                                        | empty                 |
| `sftp_remove_file`   | `remote_path`                                        | empty                 |
| `sftp_remove_dir`    | `remote_path` (empty directory only)                 | empty                 |
| `sftp_upload_file`   | `local_path`, `remote_path`                          | empty                 |
| `sftp_download_file` | `remote_path`, `local_path`                          | empty                 |
| `sftp_move_file`     | `remote_path` (source), `destination_path`           | empty                 |
| `sftp_move_dir`      | `remote_path` (source directory), `destination_path` | empty                 |
| `sftp_copy_file`     | `remote_path` (source), `destination_path`           | empty                 |

Entry fields in `sftp_list_dir` and `sftp_stat`:

| Field         | Type        | Description                                  |
| ------------- | ----------- | -------------------------------------------- |
| `Type`        | string      | `folder`, `file` or `symlink`                |
| `name`        | string      | File or directory name                       |
| `size`        | int         | Size in bytes                                |
| `rights`      | string      | Permissions as `rwxr-xr-x` (9 characters)    |
| `mtime`       | int/null    | Modification time, Unix seconds              |
| `uid`         | int/null    | Owner (numeric ID)                           |
| `gid`         | int/null    | Group (numeric ID)                           |
| `link_target` | string/null | Symlink target; `null` for all other entries |

All fields are always present; missing values are `null`. The success status for every SFTP action is `"ok"`.

### 8.5. SFTP errors and remedies

SFTP errors start with a code from `SftpErrorKind`:

| Code                     | When it happens                                           | What to do                                  |
| ------------------------ | --------------------------------------------------------- | ------------------------------------------- |
| `sftp_unavailable`       | The server refused the `sftp` subsystem or did not answer | Check whether SFTP is enabled on the server |
| `sftp_not_found`         | The path does not exist                                   | Check the path                              |
| `sftp_permission_denied` | No rights to read, write or delete                        | Check the user's permissions on the server  |
| `sftp_exists`            | The target already exists (`create_dir`, `move`)          | Pick another name or delete the target      |
| `sftp_not_a_directory`   | A directory was expected but the path is a file           | Check the path type                         |
| `sftp_failed`            | Other SFTP server errors                                  | Read the text after the code                |

Example: `stderr: "sftp_not_found: Failed to read remote directory: No such file"`.

Errors that occur before SFTP is involved (for example, a local file cannot be opened for upload) come with an `SSH internal error:` prefix and no code. A message like `Missing file path in payload` (and similar `Missing ... in payload` messages) means the payload lacks the required field.

### 8.6. The client in `rust_tests.py`

`core/rust_tests.py` contains a `SynexClient` class with: `connect_ssh` (with `host_key_fingerprint`), `ssh_exec`, `monitor`, `cancel`, `send_only`/`recv_frame` (for concurrent cancellation), `docker_info`, `docker_containers`, `docker_images`, `docker_stats`, `docker_logs`, `docker_container_action`, `docker_container_remove`, `list_dir`, `sftp_stat`, `sftp_create_dir`, `sftp_remove_file`, `sftp_remove_dir`, `sftp_download_file`, `sftp_upload_file`, `sftp_move_file`, `sftp_move_dir`, `sftp_copy_file`, `disconnect_ssh`, `close_ssh`. The client supports Named Pipe, Unix socket and TCP fallback.

Test groups:

- `run_tofu_tests`: `host_key_unknown` without a fingerprint, successful connection with a fingerprint, `host_key_mismatch` with a foreign fingerprint;
- `run_exec_tests`: `exit_code` (`exit 3` → 3, `false` → 1, `kill -9 $$` → 137, `kill -TERM $$` → 143), stderr, unknown session;
- `run_monitor_tests` (called from `run_exec_tests`): `monitor_get_values` twice in one session. Network rates are `null` on the first call and numbers on the second; rx counters never decrease; `/` is filled in and `disk_usage_percent` is within 0..100;
- `run_sftp_tests`: create, upload, download, move files, move a directory (`sftp_move_dir`: contents move with the directory, codes `sftp_not_found`, `sftp_not_a_directory`, `sftp_exists`), copy, remove;
- `run_sftp_error_tests`: SFTP error codes, `sftp_stat` (file, directory, symlink), `list_dir` fields;
- `run_cancel_tests`: `cancel` without `target_id` and against a non-existent target — errors; `sleep 100` is cancelled (`status: "cancelled"`, reply within seconds); a file upload is cancelled and answers `cancelled`;
- `run_docker_tests`: `docker_info` (envelope `ok`, status from the allowed set), `docker_containers`/`docker_images` (arrays, `ContainerInfo` fields), `docker_stats`. When Docker is unavailable on the server, the remaining Docker checks are skipped;
- `run_socket_permission_test`: `0600` permissions on the Unix socket (unix only);
- `run_frame_limit_test`: a frame above `16 MiB`.

All remote paths live under `/root/synex_test_<random>`. Deletion checks the prefix so it cannot leave the test directory.

### 8.7. Running the tests

1. Build the worker: `cargo build -p synex_worker --release` (from the `core` directory).
2. Start the worker separately. `__main__` uses `SynexClient(spawn_worker=False)`, so the tests do not spawn it.
3. Set the environment variables:

| Variable                  | Required   | Default | Description                                            |
| ------------------------- | ---------- | ------- | ------------------------------------------------------ |
| `SSH_TEST_HOST`           | —          | —       | Not read: the address is hardcoded in `load_config()`  |
| `SSH_TEST_PORT`           | no         | `22`    | SSH port                                               |
| `SSH_TEST_USER`           | no         | `root`  | User name                                              |
| `SSH_TEST_PASS`           | —          | —       | Not read: the password is hardcoded in `load_config()` |
| `SSH_TEST_KEY`            | one of two | —       | Path to the private key                                |
| `SSH_TEST_KEY_PASSPHRASE` | no         | —       | Key passphrase                                         |

If `load_config()` has neither a password nor a key, the script exits with code `2`.

4. Run from the `core` directory: `python rust_tests.py`. Exit code `0` means all checks passed, `1` means some failed.

Order: TOFU → exec → SFTP → SFTP errors → cancel → Docker → disconnect → socket permissions → frame limit. The frame limit check goes last, because the connection is unusable afterwards.

**Important:** the tests run against a real server and create/remove files under `/root/synex_test_*`. Run them only against a test server.

**Manual host key rotation scenario** (on a test server, never on production):

1. Connect with a fingerprint; it is stored.
2. On the server replace `/etc/ssh/ssh_host_ed25519_key` and `.pub`, then restart sshd.
3. Connect again with the same fingerprint: `host_key_mismatch` is expected.

The tests do not cover this scenario automatically.

**Not verified at runtime** (depends on the server): `exit_code` 137/143 for `kill -9 $$` and `kill -TERM $$` (requires `exit-signal` from sshd); `cpu_usage_percent` is always `0.0` on the first `monitor_get_values` call.

## 9. Implementation status

**Done:**

- `exit_code` from `ExitStatus`, signal → `128 + N`;
- monitoring: network (`net_*`, rates as a delta since the previous call in the session) and `/` disk (`disk_*`, from `df -P -k /`);
- TOFU for `ssh_connect` (`host_key_unknown`, `host_key_mismatch`, `fingerprint` and `key_type` fields);
- frame size limit (16 MiB);
- `0600` permissions on the Unix socket;
- SFTP error codes, `sftp_stat`, `list_dir` entry fields, unified `ok` status;
- directory move (`sftp_move_dir`);
- action cancellation: `cancel` with `target_id`, `status: "cancelled"`, `ssh_exec` — TERM → KILL after 3 s, SFTP transfers stop immediately; IPC is read concurrently with actions (responses are written under a shared `Mutex`); on IPC disconnect all actions are cancelled;
- Docker over an SSH tunnel to `/var/run/docker.sock` (bollard, `direct-streamlocal@openssh.com`), `docker_*` actions and error codes, live logs via `docker_logs` with `follow`.

**Not done:**

- **SFTP progress** (intermediate frames with `status: "progress"`).
- **SFTP channel reuse** within a session.
- **Folder transfer** (`sftp_upload_dir`, `sftp_download_dir`).
- **musl and macOS builds**, Windows exe build with SFTP.

**Out of the worker's scope:** the backend does not pass `host_key_fingerprint` yet (TOFU), and it still manages Docker through the `docker` CLI.

## 10. Docker

Docker runs on the remote server. The worker does not expose any port: it opens a `direct-streamlocal@openssh.com` channel to `/var/run/docker.sock` inside the same SSH session and speaks plain HTTP over it (client: `bollard` with a custom transport).

How it works:

- Every HTTP request opens its own channel to the socket, so the session is busy only while the channel opens, not for the whole action. A long `docker_logs -f` therefore does not block `monitor` or `ssh_exec`.
- The request `id` is the session `id` from `ssh_connect`.
- Successful `stdout` is JSON shaped like the backend models (`backend/app/plugins/docker/schemas.py`).
- Errors: envelope `status: "error"` with a `docker_*` code at the start of `stderr` (see 10.5).
- Socket path is `/var/run/docker.sock` (the `DEFAULT_SOCKET` constant).

### 10.1. Actions

| Action                    | Payload                                | Successful `stdout`                                                                               |
| ------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `docker_info`             | —                                      | `DockerInfo`: `status`, `detail`, `client_version`, `server_version`, `api_version`, `os`, `arch` |
| `docker_containers`       | `all` (bool)                           | array of `ContainerInfo`                                                                          |
| `docker_container_action` | `container`, `action`, `timeout`       | `{container, action, output}` where `container` is the state after the action                     |
| `docker_container_remove` | `container`, `force`, `volumes`        | empty                                                                                             |
| `docker_logs`             | `container`, `tail`, `since`, `follow` | `{lines, next_since}`                                                                             |
| `docker_stats`            | `container` (optional)                 | array of `ContainerStatsInfo`                                                                     |
| `docker_images`           | —                                      | array of `ImageInfo`                                                                              |

`action` for `docker_container_action`: `start`, `stop`, `restart`, `pause`, `unpause`, `kill`. `timeout` is Docker's grace period for `stop`/`restart` (seconds), not a request timeout: the request itself is bounded separately (60 s, exceeding it gives `docker_timeout`).

`docker_stats` without `container` covers all running containers.

### 10.2. Response fields

`ContainerInfo` (same as the backend):

| Field             | Type        | Description                                                                |
| ----------------- | ----------- | -------------------------------------------------------------------------- |
| `id`              | string      | Full container ID (64 chars)                                               |
| `name`            | string      | Name without the leading `/`                                               |
| `names`           | string[]    | All names                                                                  |
| `image`           | string      | Image                                                                      |
| `command`         | string      | Startup command                                                            |
| `created_at`      | string/null | RFC3339, UTC                                                               |
| `state`           | string      | `created`, `running`, `paused`, `restarting`, `removing`, `exited`, `dead` |
| `status`          | string      | As in `docker ps`: `Up 5 minutes (healthy)`                                |
| `health`          | string/null | `healthy`, `unhealthy`, `starting`                                         |
| `exit_code`       | int/null    | Exit code of a stopped container                                           |
| `ports`           | object[]    | `{container_port, protocol, host_ip, host_port}`, all strings              |
| `labels`          | object      | Container labels                                                           |
| `networks`        | string[]    | Network names                                                              |
| `compose_project` | string/null | `com.docker.compose.project` label                                         |
| `compose_service` | string/null | `com.docker.compose.service` label                                         |

`LogLineInfo`: `{time, stream, text}`, where `time` is RFC3339 with nanoseconds (UTC) and `stream` is `stdout` or `stderr`. `next_since` is the time of the last line; pass it back as `since` in the next request.

`since` accepts Unix seconds (`1700000000`), RFC3339 (`2026-11-06T12:00:00Z`) and durations (`30s`, `10m`, `1h30m`). `tail` is a line count (all lines by default).

`ContainerStatsInfo`: `id`, `name`, `cpu_percent` (100 = one full core), `memory_used`, `memory_limit`, `memory_percent`, `net_rx`, `net_tx`, `block_read`, `block_write`, `pids`. Byte values are numbers.

CPU is computed from two consecutive Docker samples (about one second apart); if the second sample does not arrive, `cpu_percent` is `null`.

`ImageInfo`: `id` (without the `sha256:` prefix), `repository`, `tag`, `digest`, `created_at`, `size` (bytes).

### 10.3. `docker_info` and its statuses

This is an answer to a question, not a failure, so the envelope is always `status: "ok"` and the result is the `status` field inside the JSON:

| `status`             | When                                                                       |
| -------------------- | -------------------------------------------------------------------------- |
| `ok`                 | The daemon answered; `server_version`/`api_version` are filled             |
| `not_installed`      | No `/var/run/docker.sock` and no `docker` binary on the server             |
| `permission_denied`  | The socket exists but the SSH user has no access (`test -w`)               |
| `daemon_unavailable` | The socket exists (or `docker` is installed) but the daemon did not answer |

The probe runs through `ssh_exec` (`test -S`, `test -w`, `command -v docker`), because an SSH channel-open failure carries no errno text.

### 10.4. Live logs (`docker_logs` with `follow: true`)

- Intermediate frames: `status: "progress"` with `stdout` set to JSON `{lines, next_since}` (new lines only).
- Final frame: `status: "cancelled"` (stopped by `cancel` with `target_id` equal to the session id) or `status: "ok"` (the daemon closed the stream, e.g. the container stopped).
- The client must accept several frames with the same `id`.

### 10.5. Error codes

| Code                         | When                                                                        |
| ---------------------------- | --------------------------------------------------------------------------- |
| `docker_not_installed`       | Docker is not installed                                                     |
| `docker_permission_denied`   | No access to the socket/daemon                                              |
| `docker_daemon_unavailable`  | The daemon is unavailable                                                   |
| `docker_container_not_found` | No such container                                                           |
| `docker_container_conflict`  | The action does not fit the state (`is not running`, `already paused`, ...) |
| `docker_timeout`             | The action exceeded its limit                                               |
| `docker_command_failed`      | Other daemon errors                                                         |

Example: `stderr: "docker_container_not_found: No such container: web"`.

### 10.6. Limitations

Not implemented (out of scope for this task): a `docker_logs` follow stream does not survive an SSH reconnect, and `docker_stats` polls containers one by one (the Docker API has no bulk `stats`).
