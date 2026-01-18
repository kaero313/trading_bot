from fastapi import APIRouter

from app.services.bot_service import start_bot, stop_bot
from app.models.schemas import BotStatus

router = APIRouter()


@router.post("/bot/start", response_model=BotStatus)
def bot_start() -> BotStatus:
    return start_bot()


@router.post("/bot/stop", response_model=BotStatus)
def bot_stop() -> BotStatus:
    return stop_bot()
