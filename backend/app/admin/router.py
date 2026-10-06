"""Observation and explicit AI tests only. No send, control, file or config editing."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, RedirectResponse, StreamingResponse

from app.admin.events import sse_message
from app.admin.models import AnalyzeInput, UIModel
from app.admin.service import PaidRequestNotConfirmed, PlaygroundBusy, PRESETS

router = APIRouter()
ASSETS = Path(__file__).resolve().parents[1] / "static" / "admin"


class EmptyInput(UIModel):
    pass


@router.get("/")
async def root():
    return RedirectResponse("/ui/")


@router.get("/ui/")
async def ui():
    return FileResponse(ASSETS / "index.html")


@router.get("/api/admin/session")
async def session(request: Request):
    return {"csrf_token": request.app.state.admin.csrf_token}


@router.get("/api/admin/status")
async def status(request: Request):
    return request.app.state.admin.status()


@router.get("/api/admin/settings")
async def settings(request: Request):
    return request.app.state.admin.public_settings()


@router.get("/api/admin/ai/status")
async def ai_status(request: Request):
    return request.app.state.admin.ai_status


@router.post("/api/admin/ai/check")
async def ai_check(request: Request, body: EmptyInput):
    return await request.app.state.admin.check_ai()


@router.get("/api/admin/playground/presets")
async def presets():
    return [{"label": label, "text": text} for label, text in PRESETS]


@router.get("/api/admin/playground/history")
async def playground_history(request: Request):
    return list(request.app.state.admin.playground.history)


@router.get("/api/admin/live/history")
async def live_history(request: Request):
    return list(request.app.state.admin.live_history)


@router.post("/api/admin/playground/analyze")
async def analyze(request: Request, body: AnalyzeInput):
    try:
        return await request.app.state.admin.playground.analyze(body.text, allow_paid=body.allow_paid)
    except PaidRequestNotConfirmed:
        raise HTTPException(403, detail="Подтвердите отдельный платный запрос OpenAI.") from None
    except PlaygroundBusy:
        raise HTTPException(409, detail="Playground занят. Дождитесь текущего анализа.") from None
    except ValueError:
        raise HTTPException(422, detail="Комментарий превышает установленный лимит.") from None


@router.get("/api/admin/events")
async def events(request: Request):
    admin = request.app.state.admin
    try:
        subscriber = admin.events.subscribe()
    except RuntimeError:
        raise HTTPException(503, detail="Поток событий недоступен.") from None

    async def stream():
        try:
            yield "retry: 3000\n" + sse_message("snapshot", admin.status().model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    return
                try:
                    async with asyncio.timeout(15):
                        event = await subscriber.queue.get()
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    return
                if subscriber.overflowed:
                    subscriber.overflowed = False
                    yield sse_message("resync", {"reason": "slow_subscriber"})
                yield event.encode()
        finally:
            admin.events.unsubscribe(subscriber)

    return StreamingResponse(stream(), media_type="text/event-stream",
        headers={"x-accel-buffering": "no"},
        background=BackgroundTask(admin.events.unsubscribe, subscriber))
