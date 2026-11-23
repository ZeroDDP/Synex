"""Test peer for IPC v1. Run as a child process, with stdout reserved for frames."""

import hashlib
import struct
import sys
from pathlib import Path

import msgpack


def send(data):
    raw = msgpack.packb(data, use_bin_type=True)
    sys.stdout.buffer.write(struct.pack(">I", len(raw)) + raw)
    sys.stdout.buffer.flush()


card = {
    "manifest": {
        "id": "local.demo",
        "version": "1.0",
        "permissions": ["metrics.read"],
        "actions": [{"id": "summary", "input_schema": {"additionalProperties": False}}],
    },
    "state": "disabled",
    "grants": [],
}
installed = False
pending = {}
while head := sys.stdin.buffer.read(4):
    frame = msgpack.unpackb(sys.stdin.buffer.read(int.from_bytes(head, "big")), raw=False)
    key = frame["id"]
    if frame["type"] == "response":
        send(dict(frame, id=pending.pop(key)))
        continue
    method, params = frame["method"], frame["params"]
    if method == "hello":
        result = {"ipc": 1}
    elif method == "list":
        result = {"plugins": [card] if installed else []}
    elif method == "get":
        result = card
    elif method == "inspect":
        result = {
            "manifest": card["manifest"],
            "sha256": hashlib.sha256(Path(params["path"]).read_bytes()).hexdigest(),
        }
    elif method in {"install", "update", "rollback", "uninstall"}:
        installed = method != "uninstall"
        result = card
    elif method in {"disable", "enable"}:
        card["state"] = "enabled" if method == "enable" else "disabled"
        send(
            {
                "type": "event",
                "name": "plugin.status",
                "data": {"id": "local.demo", "state": card["state"]},
            }
        )
        result = card
    elif method in {"grant", "revoke"}:
        card["grants"] = [params["server_id"]] if method == "grant" else []
        result = card
    elif method == "authorize":
        result = {"allowed": params["server_id"] in card["grants"]}
    elif method == "invoke":
        callback = "cb-" + key
        pending[callback] = key
        send({"type": "request", "id": callback, "method": "metrics.read", "params": params})
        continue
    elif method == "disconnect":
        break
    elif method == "hang":
        continue
    else:
        result = {}
    send({"type": "response", "id": key, "result": result})
