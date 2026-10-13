"""Local observation, safe operator settings and read-only LIVE connection control."""
import asyncio
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator

from fastapi import APIRouter, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, RedirectResponse, StreamingResponse

from app.admin.events import sse_message
from app.admin.models import (AnalyzeInput, UIModel, ConnectInput, SaveSettingsInput,
    ApplySettingsInput, ResetSettingsInput, DisconnectInput)
from app.admin.runtime import RuntimeConflict
from app.admin.service import PaidRequestNotConfirmed, PlaygroundBusy, PRESETS
from app.delivery.models import ApprovalInput, ManualDeliveryInput
from app.delivery.suggestions import SuggestionConflict
from app.android.diagnostics import CaptureError
from app.windows.target import TargetRejected

router = APIRouter()
ASSETS = Path(__file__).resolve().parents[1] / "static" / "admin"
PREPARATION_POLL_SECONDS = 2


class EmptyInput(UIModel):
    pass


class DeliveryModeInput(UIModel):
    mode: Literal["MANUAL", "AUTOMATIC"]
    expected_live_session_id: str | None = Field(pattern=r"^[a-f0-9]{32}$")
    expected_active_runtime_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
class WindowsArmInput(UIModel):
    empty_confirmed: bool = Field(strict=True)


class WindowsFillInput(WindowsArmInput):
    delivery: ManualDeliveryInput


class WindowsAutoInput(UIModel):
    enabled: bool = Field(strict=True)


def windows_service(request):
    service = request.app.state.delivery
    if service.view().transport != "windows_live_studio": raise HTTPException(409, detail="Windows transport unavailable.")
    return service


def android_service(request):
    service = request.app.state.delivery
    if service.view().transport != "android": raise HTTPException(409, detail="Android transport unavailable.")
    return service


@router.post("/api/admin/windows/arm", status_code=202)
async def arm_windows(request: Request, body: WindowsArmInput):
    try: return windows_service(request).start_workflow(empty_confirmed=body.empty_confirmed)
    except SuggestionConflict: raise HTTPException(409, detail="Windows arm unavailable.") from None


@router.post("/api/admin/windows/disarm")
async def disarm_windows(request: Request, body: EmptyInput):
    return await windows_service(request).disarm()


@router.post("/api/admin/windows/fill", status_code=202)
async def fill_windows(request: Request, body: WindowsFillInput):
    try: return windows_service(request).start_workflow(empty_confirmed=body.empty_confirmed, body=body.delivery)
    except SuggestionConflict: raise HTTPException(409, detail="Windows fill unavailable.") from None


@router.post("/api/admin/windows/send-once", status_code=202)
async def send_once_windows(request: Request, body: WindowsFillInput):
    try: return windows_service(request).start_workflow(empty_confirmed=body.empty_confirmed, body=body.delivery, mode="send_once")
    except SuggestionConflict: raise HTTPException(409, detail="Windows send gate unavailable.") from None


@router.post("/api/admin/windows/accept-empty")
async def accept_windows_empty(request: Request, body: WindowsArmInput):
    service = windows_service(request)
    try:
        if not body.empty_confirmed: raise TargetRejected("not_confirmed")
        service.sender.confirm_empty()
        return service.view()
    except TargetRejected: raise HTTPException(409, detail="A recent verified send and explicit empty-field review are required.") from None


@router.post("/api/admin/windows/auto")
async def windows_auto(request: Request, body: WindowsAutoInput):
    service = windows_service(request)
    try:
        service.set_auto(body.enabled)
        return service.view()
    except TargetRejected: raise HTTPException(409, detail="Windows AUTO requires the startup gate, own identity and verified empty-composer acceptance.") from None


class ConfirmSendInput(UIModel):
    token: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True: raise ValueError("Explicit confirmation required")
        return value


@router.post("/api/admin/device/verify-account")
async def verify_account(request: Request, body: EmptyInput):
    try: return await request.app.state.delivery.trust_action(verify=True)
    except SuggestionConflict:
        raise HTTPException(409, detail="Откройте собственный Profile с точным настроенным @handle и повторите проверку.") from None


@router.post("/api/admin/device/confirm-send")
async def confirm_send(request: Request, body: ConfirmSendInput):
    try: return await request.app.state.delivery.trust_action(token=body.token)
    except SuggestionConflict:
        raise HTTPException(409, detail="Кандидат изменился или не проверен. Проверьте устройство, LIVE и composer.") from None


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


async def runtime_action(action, *args, **kwargs):
    try:
        return await action(*args, **kwargs)
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
    return await runtime_action(request.app.state.operator_runtime.live.connect, body.username,
        expected_active_runtime_revision=body.expected_active_runtime_revision)


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
    if request.app.state.admin.settings.tiktok_transport == "android":
        from app.android.preparation import prepare_mobile
        try:
            return await prepare_mobile(request.app.state.delivery, request.app.state.operator_runtime)
        except (SuggestionConflict, RuntimeConflict) as exc:
            raise HTTPException(409, detail=str(exc)) from None
    return request.app.state.delivery.prepare()


@router.post("/api/admin/device/capture")
async def capture_device(request: Request, body: EmptyInput):
    try:
        return await request.app.state.delivery.capture()
    except (CaptureError, SuggestionConflict):
        raise HTTPException(409, detail="Capture недоступен: проверьте устройство, foreground и локальный каталог diagnostics/android (не более 20 captures).") from None


@router.post("/api/admin/delivery/dry-run")
async def dry_run(request: Request, body: ManualDeliveryInput):
    return await delivery_action(request.app.state.delivery.dry_run, body)


@router.post("/api/admin/delivery/mode")
async def delivery_mode(request: Request, body: DeliveryModeInput):
    try:
        return android_service(request).set_mode(body.mode, body.expected_live_session_id,
            body.expected_active_runtime_revision)
    except SuggestionConflict:
        raise HTTPException(409, detail="AUTO недоступен: проверьте startup SEND gate, текущую сессию, Profile, LIVE/composer и журнал. UNKNOWN повторять нельзя.") from None


@router.post("/api/admin/delivery/fill")
async def fill_phone(request: Request, body: ManualDeliveryInput):
    return await delivery_action(android_service(request).fill, body)


@router.post("/api/admin/delivery/verify-fill")
async def verify_fill(request: Request, body: EmptyInput):
    try: return await android_service(request).verify_fill()
    except SuggestionConflict:
        raise HTTPException(409, detail="Нет текущего введённого ответа для проверки.") from None


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
    return admin.current_live_history()


@router.get("/api/admin/suggestions/{suggestion_id}")
async def suggestion_view(request: Request, suggestion_id: str):
    admin = request.app.state.admin
    try:
        return admin.review_view(suggestion_id)
    except SuggestionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None


@router.post("/api/admin/suggestions/{suggestion_id}/approve")
async def approve_suggestion(request: Request, suggestion_id: str, body: ApprovalInput):
    admin = request.app.state.admin
    try:
        return admin.approve_suggestion(suggestion_id, body)
    except SuggestionConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None


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
            snapshot = admin.status().model_dump(mode="json")
            preparation = snapshot['preparation']
            windows = admin.settings.tiktok_transport == 'windows_live_studio'
            loop = asyncio.get_running_loop()
            next_preparation_check = loop.time() + PREPARATION_POLL_SECONDS
            yield "retry: 3000\n" + sse_message("snapshot", snapshot)
            while True:
                windows = admin.settings.tiktok_transport == 'windows_live_studio'
                if await request.is_disconnected():
                    return
                try:
                    async with asyncio.timeout(max(.001, next_preparation_check - loop.time()) if windows else 15):
                        event = await subscriber.queue.get()
                except TimeoutError:
                    event = False
                # Artifact changes originate outside the backend. A bounded
                # low-frequency refresh also works while the stream is quiet.
                # Read-only projection revalidates current process identities.
                if windows and loop.time() >= next_preparation_check:
                    next_preparation_check = loop.time() + PREPARATION_POLL_SECONDS
                    current = admin.preparation.view().model_dump(mode='json')
                    if current != preparation:
                        preparation = current
                        yield sse_message('preparation_status', current)
                if event is False:
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
