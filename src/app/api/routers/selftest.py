from dataclasses import asdict

from fastapi import APIRouter, HTTPException, status

from app.accounts import UnknownAccountError
from app.api.deps import ManagerDep
from app.api.schemas import (
    HealthRequest,
    HealthResponse,
    SelftestAccountOut,
    SelftestRequest,
    SelftestResponse,
)
from app.config import settings
from app.healthcheck import notify_report, run_healthcheck
from app.selftest import is_running, run_selftest

router = APIRouter(tags=["selftest"])


@router.post("/selftest", summary="Самопроверка аккаунтов и ботов")
async def selftest(body: SelftestRequest, manager: ManagerDep) -> SelftestResponse:
    """Реально пишет анонимным ботам (если не `dry_run`) и может идти минуты."""
    if body.accounts:
        try:
            accounts = [manager.get(account_id) for account_id in body.accounts]
        except UnknownAccountError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown account '{exc.args[0]}'",
            ) from None
    else:
        accounts = manager.list()

    if is_running():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Selftest is already running")
    try:
        results = await run_selftest(
            accounts,
            bots=body.bots or None,
            dry_run=body.dry_run,
            text=body.text,
            search_timeout=body.search_timeout,
            reply_timeout=body.reply_timeout,
            close_timeout=body.close_timeout,
        )
    except RuntimeError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Selftest is already running"
        ) from None
    return SelftestResponse(
        dry_run=body.dry_run,
        results=[SelftestAccountOut.model_validate(asdict(r)) for r in results],
    )


@router.post("/selftest/health", summary="Лёгкая проверка без сообщений незнакомым")
async def selftest_health(body: HealthRequest, manager: ManagerDep) -> HealthResponse:
    """Только сессия и стоп-команда ботам; по `notify` шлёт сводку в Telegram."""
    try:
        report = await run_healthcheck(
            manager.list(), interval_hours=settings.selftest_interval_hours or 4
        )
    except RuntimeError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Selftest is already running"
        ) from None
    if body.notify:
        await notify_report(settings, report)
    return HealthResponse.model_validate(asdict(report))
