from fastapi import APIRouter

router = APIRouter()


@router.get("/positions")
def list_positions() -> list[dict]:
    return []
