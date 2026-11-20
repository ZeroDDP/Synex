"""/api/alerts: алерты всех серверов и правила, по которым они загораются."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from app.plugins.alerts.schemas import AckResult, AlertCounts, AlertInfo, AlertList, AlertRules
from app.plugins.alerts.service import alert_service

router = APIRouter(tags=["alerts"])

ERRORS = {
    404: {"description": "Нет алерта с таким id (alert_not_found)"},
    409: {"description": "Алерт ещё активен (alert_active)"},
}


def _list(status: str = "all", server: str | None = None) -> AlertList:
    return AlertList(
        counts=AlertCounts(**alert_service.counts()),
        alerts=[
            AlertInfo(**a.as_dict()) for a in alert_service.alerts(status=status, server=server)
        ],
    )


@router.get(
    "",
    response_model=AlertList,
    summary="Алерты всех серверов",
    description=(
        "Сначала активные (critical выше, новые выше), потом погасшие (новые выше). "
        "Изменения приходят в /ws событием `alert` `{action, alert}`, action: `fired`, "
        "`updated`, `resolved`, `acknowledged`."
    ),
)
async def list_alerts(
    status: Annotated[Literal["active", "resolved", "all"], Query()] = "all",
    server: Annotated[str | None, Query(max_length=300, description='"host:port"')] = None,
) -> AlertList:
    return _list(status, server)


@router.post("/ack-all", response_model=AckResult, summary="Отметить все активные как увиденные")
async def acknowledge_all() -> AckResult:
    count = await alert_service.acknowledge_all()
    return AckResult(acknowledged=count, counts=AlertCounts(**alert_service.counts()))


@router.get("/rules", response_model=AlertRules, summary="Правила алертов")
async def get_rules() -> AlertRules:
    return AlertRules(rules=alert_service.rules())


@router.put(
    "/rules",
    response_model=AlertRules,
    summary="Заменить правила алертов",
    description=(
        "Одно правило на метрику: cpu, memory, disk, gpu_temperature (пороги warning и "
        "critical, % или °C) и server (сервер не отвечает duration_s секунд). Метрика, "
        "которой нет в списке, получает правило по умолчанию. Алерты по выключенным "
        "правилам гаснут. Правила сохраняются в alerts.json."
    ),
)
async def put_rules(body: AlertRules) -> AlertRules:
    return AlertRules(rules=await alert_service.set_rules(body.rules))


@router.delete("/rules", response_model=AlertRules, summary="Вернуть правила по умолчанию")
async def reset_rules() -> AlertRules:
    return AlertRules(rules=await alert_service.reset_rules())


@router.delete(
    "",
    status_code=204,
    summary="Очистить историю погасших алертов",
    response_class=Response,
)
async def clear_history() -> Response:
    alert_service.clear_history()
    return Response(status_code=204)


@router.get("/{alert_id}", response_model=AlertInfo, summary="Один алерт", responses=ERRORS)
async def get_alert(alert_id: str) -> AlertInfo:
    return AlertInfo(**alert_service.get(alert_id).as_dict())


@router.post(
    "/{alert_id}/ack",
    response_model=AlertInfo,
    summary="Отметить алерт как увиденный",
    responses=ERRORS,
)
async def acknowledge(alert_id: str) -> AlertInfo:
    return AlertInfo(**(await alert_service.acknowledge(alert_id)).as_dict())


@router.delete(
    "/{alert_id}",
    status_code=204,
    summary="Убрать погасший алерт из истории",
    responses=ERRORS,
    response_class=Response,
)
async def dismiss(alert_id: str) -> Response:
    alert_service.dismiss(alert_id)
    return Response(status_code=204)
