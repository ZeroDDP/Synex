from __future__ import annotations

from app.plugins.ssh.buffer import OutputBuffer


def test_entries_are_numbered_and_replayed_after_since():
    buffer = OutputBuffer(max_bytes=10_000)
    assert buffer.since(0).entries == [] and buffer.last_seq == 0

    first = buffer.append("command", {"command": "ls"})
    second = buffer.append("result", {"stdout": "a\nb", "exit_code": 0})
    third = buffer.append("command", {"command": "pwd"})
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert second.as_dict() == {
        "seq": 2,
        "ts": second.ts,
        "kind": "result",
        "stdout": "a\nb",
        "exit_code": 0,
    }

    replay = buffer.since(1)
    assert [e.seq for e in replay.entries] == [2, 3]
    assert (replay.gap, replay.first_seq, replay.last_seq) == (False, 1, 3)
    assert [e.seq for e in buffer.since(0, limit=2).entries] == [1, 2]
    assert buffer.since(3).entries == [] and not buffer.since(3).gap
    assert buffer.since(99).entries == []


def test_old_entries_are_evicted_and_gap_is_reported():
    buffer = OutputBuffer(max_bytes=2_000)
    for i in range(10):
        buffer.append("result", {"stdout": str(i) * 400})
    assert buffer.size <= 2_000
    assert buffer.first_seq > 1 and buffer.last_seq == 10

    replay = buffer.since(0)
    assert replay.gap is True
    assert [e.seq for e in replay.entries] == list(range(buffer.first_seq, 11))
    # UI видел всё до first_seq - 1: дыры нет
    assert buffer.since(buffer.first_seq - 1).gap is False


def test_oversized_entry_keeps_the_tail_of_output():
    buffer = OutputBuffer(max_bytes=1_024)
    buffer.append("command", {"command": "cat big.log"})
    entry = buffer.append("result", {"stdout": "head\n" + "x" * 5_000 + "\ntail", "stderr": ""})

    assert entry.data["truncated"] is True
    assert entry.data["stdout"].endswith("\ntail")
    assert not entry.data["stdout"].startswith("head")
    assert len(buffer) == 1  # вытеснила всё остальное, но сама осталась
    assert buffer.size <= 1_024
