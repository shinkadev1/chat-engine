from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.api.deps import AccountDep
from app.api.schemas import ChatStatus, HistoryResponse, MessageOut, SendMessageRequest

router = APIRouter(
    prefix="/accounts/{account_id}/chat",
    tags=["chat"],
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Аккаунт не найден"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Аккаунт не авторизован"},
    },
)

_CONFLICT = {
    status.HTTP_409_CONFLICT: {"description": "Действие недоступно в текущем состоянии чата"}
}


@router.post(
    "/start", status_code=status.HTTP_202_ACCEPTED, summary="Начать чат", responses=_CONFLICT
)
async def start_chat(account: AccountDep) -> ChatStatus:
    """Попросить бота найти случайного собеседника. Ошибка, если чат уже идёт."""
    state = await account.service.start_chat()
    return ChatStatus(state=state, last_seq=account.service.last_seq)


@router.post("/stop", summary="Закончить чат", responses=_CONFLICT)
async def stop_chat(account: AccountDep) -> ChatStatus:
    """Выйти из текущего диалога или отменить поиск. Ошибка, если чата нет."""
    state = await account.service.stop_chat()
    return ChatStatus(state=state, last_seq=account.service.last_seq)


@router.post(
    "/messages",
    status_code=status.HTTP_201_CREATED,
    summary="Отправить сообщение",
    responses=_CONFLICT,
)
async def send_message(payload: SendMessageRequest, account: AccountDep) -> MessageOut:
    """Передать сообщение текущему собеседнику. Ошибка, если собеседника нет."""
    message = await account.service.send_message(payload.text)
    return MessageOut.model_validate(message)


@router.post(
    "/photos",
    status_code=status.HTTP_201_CREATED,
    summary="Отправить фото",
    responses=_CONFLICT,
)
async def send_photo(account: AccountDep, file: UploadFile) -> MessageOut:
    """Передать фото текущему собеседнику файлом."""
    data = await file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл больше 20 МБ")
    message = await account.service.send_photo(data)
    return MessageOut.model_validate(message)


@router.get("/photos/{photo_id}", summary="Забрать фото собеседника")
async def get_photo(photo_id: str, account: AccountDep) -> Response:
    """Отдаёт байты один раз. Повторный запрос отвечает 404."""
    found = account.service.take_photo(photo_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото не найдено")
    data, content_type = found
    return Response(content=data, media_type=content_type)


@router.get("/videos/{video_id}", summary="Забрать видео собеседника")
async def get_video(video_id: str, account: AccountDep) -> Response:
    """Отдаёт байты один раз. Повторный запрос отвечает 404."""
    found = account.service.take_video(video_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Видео не найдено")
    data, content_type = found
    return Response(content=data, media_type=content_type)


@router.get("/messages", summary="Переписка")
async def get_messages(
    account: AccountDep,
    after_seq: int = Query(0, ge=0, description="Вернуть только сообщения новее этого seq"),
    limit: int = Query(50, ge=1, le=500, description="Сколько сообщений вернуть"),
) -> HistoryResponse:
    """История переписки; `after_seq` позволяет опрашивать только новые сообщения."""
    messages = account.service.history(after_seq=after_seq, limit=limit)
    return HistoryResponse(
        state=account.service.state,
        messages=[MessageOut.model_validate(message) for message in messages],
    )


@router.get("/status", summary="Текущее состояние")
async def get_status(account: AccountDep) -> ChatStatus:
    """Состояние чата и `seq` последнего сообщения, чтобы дальше опрашивать `/messages`."""
    return ChatStatus(state=account.service.state, last_seq=account.service.last_seq)
