from fastapi import APIRouter

from app.api.deps import ManagerDep
from app.api.schemas import AccountOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", summary="Список аккаунтов")
async def list_accounts(manager: ManagerDep) -> list[AccountOut]:
    """Все настроенные аккаунты и их готовность к работе."""
    return [
        AccountOut(
            id=account.id,
            authorized=account.authorized,
            username=account.username,
            bot_username=account.config.anon_bot_username,
            mode=account.config.mode,
            state=account.service.state if account.authorized else None,
            limited_until=account.service.limited_until if account.authorized else None,
        )
        for account in manager.list()
    ]
