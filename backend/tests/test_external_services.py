from types import SimpleNamespace

import pytest

from app.external import services
from app.external.ipc import HostError


async def test_snapshot_filters_servers(monkeypatch):
    item = SimpleNamespace(server="A", updated_at=10, cpu=SimpleNamespace(percent=33), memory=None)
    monkeypatch.setattr(
        services.metrics_monitor, "snapshot", lambda: SimpleNamespace(servers=[item])
    )
    monkeypatch.setattr(services.metrics_monitor, "touch", lambda: None)
    assert (await services.read_service("metrics.read", "A", {}, ["monitor"]))["cpu_percent"] == 33
    with pytest.raises(HostError, match="measurement"):
        await services.read_service("metrics.read", "B", {}, ["monitor"])


async def test_journal_scope_and_output_limit(monkeypatch):
    from app.plugins.logs.schemas import JournalEntryInfo

    item = JournalEntryInfo.model_construct(
        time=11, unit="app.service", level=None, message="x" * 140000
    )

    async def read(server, manager, query, lines):
        assert server == "A" and query.since == "-1h" and query.units == ("app.service",)
        assert lines == 5
        return SimpleNamespace(entries=[item, item])

    monkeypatch.setattr(services, "journal_entries", read)
    result = await services.read_service(
        "logs.read", "A", {"unit": "app.service", "limit": 5}, ["logs"]
    )
    assert result["truncated"] and len(result["entries"]) == 1
    with pytest.raises(HostError, match="disabled"):
        await services.read_service("logs.read", "A", {}, [])
