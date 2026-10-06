"""Local CSRF-protected actions with no provider/model/endpoint overrides."""
from fastapi import APIRouter, HTTPException, Request

from app.admin.models import UIModel
from app.admin.service import PlaygroundBusy
from app.quality.models import ReviewInput, RunInput
from app.quality.service import QualityDisabled, StaleRun

router = APIRouter(prefix="/api/admin/quality")


@router.get("")
async def snapshot(request: Request):
    return request.app.state.quality.snapshot()


@router.post("/start")
async def start(request: Request, body: UIModel):
    try:
        return await request.app.state.quality.start()
    except QualityDisabled as exc:
        raise HTTPException(403, detail=str(exc)) from None
    except PlaygroundBusy:
        raise HTTPException(409, detail="Playground или Quality Lab занят.") from None


@router.post("/stop")
async def stop(request: Request, body: RunInput):
    try:
        return request.app.state.quality.stop(body.run_id)
    except StaleRun:
        raise HTTPException(409, detail="Прогон заменён. Обновите результаты.") from None


@router.post("/review")
async def review(request: Request, body: ReviewInput):
    try:
        return request.app.state.quality.review(body.run_id, body.case_id, body.review)
    except StaleRun:
        raise HTTPException(409, detail="Прогон заменён или кейс ещё не завершён.") from None
