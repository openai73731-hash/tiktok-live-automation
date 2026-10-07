"""Local observation, safe operator settings and read-only LIVE connection control."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, RedirectResponse, StreamingResponse

from app.admin.events import sse_message
from app.admin.models import (AnalyzeInput, UIModel, ConnectInput, SaveSettingsInput,
    ApplySettingsInput, ResetSettingsInput, DisconnectInput)
from app.admin.runtime import RuntimeConflict
from app.admin.service import PaidRequestNotConfirmed, PlaygroundBusy, PRESETS
from app.delivery.models import ApprovalInput, ApprovalEvent, ManualDeliveryInput
from app.delivery.suggestions import SuggestionConflict

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


@router.get("/api/admin/operator/settings")
async def operator_settings(request: Request):
    return request.app.state.operator_runtime.view()


async def runtime_action(action, *args):
    try:
        return await action(*args)
    except RuntimeConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except SuggestionConflict:
        raise HTTPException(409, detail="Выберите устройство из последнего списка backend.") from None
    except ValueError:
        raise HTTPException(422, detail="Проверьте username и допустимые параметры настроек.") from None
    except OSError:
        raise HTTPException(500, detail="Не удалось сохранить локальные настройки.") from None


@router.post("/api/admin/operator/settings")
async def save_settings(request: Request, body: SaveSettingsInput):
    return await runtime_action(request.app.state.operator_runtime.save, body.settings, body.expected_saved_revision)


@router.post("/api/admin/operator/apply")
async def apply_settings(request: Request, body: ApplySettingsInput):
    return await runtime_action(request.app.state.operator_runtime.apply, body.expected_saved_revision, body.expected_active_runtime_revision)


@router.post("/api/admin/operator/reset")
async def reset_settings(request: Request, body: ResetSettingsInput):
    return await runtime_action(request.app.state.operator_runtime.reset, body.expected_saved_revision)


@router.post("/api/admin/live/connect", status_code=202)
async def live_connect(request: Request, body: ConnectInput):
    return await runtime_action(request.app.state.operator_runtime.live.connect, body.username)


@router.post("/api/admin/live/disconnect")
async def live_disconnect(request: Request, body: DisconnectInput):
    return await runtime_action(request.app.state.operator_runtime.live.disconnect, body.expected_live_session_id)


@router.get("/api/admin/delivery/status")
async def delivery_status(request: Request):
    return request.app.state.delivery.view()


@router.get("/api/admin/delivery/history")
async def delivery_history(request: Request):
    return request.app.state.delivery.recent()


@router.post("/api/admin/device/inspect")
async def inspect_device(request: Request, body: UIModel):
    return await request.app.state.delivery.inspect()


@router.post("/api/admin/delivery/prepare")
async def prepare_delivery(request: Request, body: UIModel):
    return request.app.state.delivery.prepare()


@router.post("/api/admin/delivery/dry-run")
async def dry_run(request: Request, body: ManualDeliveryInput):
    return await delivery_action(request.app.state.delivery.dry_run, body)


@router.post("/api/admin/delivery/send")
async def send_reply(request: Request, body: ManualDeliveryInput):
    return await delivery_action(request.app.state.delivery.send, body)


async def delivery_action(action, body):
    try:
        return await action(body)
    except SuggestionConflict:
        raise HTTPException(409, detail="Delivery binding изменён. Проверьте карточку и состояние устройства.") from None


@router.get("/api/admin/ai/status")
async def ai_status(request: Request):
    return request.app.state.admin.ai_status


@router.post("/api/admin/ai/check")
async def ai_check(request: Request, body: EmptyInput):
    try:
        return await request.app.state.admin.check_ai()
    except PlaygroundBusy:
        raise HTTPException(409, detail="Дождитесь применения настроек.") from None


@router.get("/api/admin/playground/presets")
async def presets():
    return [{"label": label, "text": text} for label, text in PRESETS]


@router.get("/api/admin/playground/history")
async def playground_history(request: Request):
    admin = request.app.state.admin
    return [admin.review_record(record) for record in admin.playground.history]


@router.get("/api/admin/live/history")
async def live_history(request: Request):
    admin = request.app.state.admin
    return [admin.review_record(record) for record in admin.live_history]


@router.get("/api/admin/suggestions/{suggestion_id}")
async def suggestion_view(request: Request, suggestion_id: str):
    admin = request.app.state.admin
    try:
        return admin.suggestions.view(suggestion_id, admin.delivery_context())
    except SuggestionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None


@router.post("/api/admin/suggestions/{suggestion_id}/approve")
async def approve_suggestion(request: Request, suggestion_id: str, body: ApprovalInput):
    admin = request.app.state.admin
    try:
        admin.suggestions.approve(suggestion_id, body, admin.delivery_context())
    except SuggestionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None
    admin.events.publish("suggestion_approval", ApprovalEvent(suggestion_id=suggestion_id))
    return admin.suggestions.view(suggestion_id, admin.delivery_context())


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
