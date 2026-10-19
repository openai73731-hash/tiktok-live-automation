"""Informational read-only checks; this result grants no input/AUTO authority."""
import asyncio

from app.ai.lmstudio_provider import LMStudioReplyEngine
from app.ai.periodic_policy import periodic_statement
from app.android.delivery_evidence import unresolved
from app.android.tiktok_ui import TIKTOK_YOU_4683
from app.delivery.suggestions import SuggestionConflict


async def check_readiness(runtime, service):
    if runtime.transport != 'android' or runtime.admin.delivery is not service:
        raise SuggestionConflict('Выберите Android и примените настройки.')
    if service.busy or service.tasks:
        raise SuggestionConflict('Дождитесь завершения текущей проверки или отправки.')
    if (service.mode == 'MANUAL' and not runtime.live.service and not service.account_proof.prepared_now()):
        from app.android.preparation import prepare_mobile
        await prepare_mobile(service, runtime)
    before = service.context()
    binding = (before.active_runtime_revision, before.live_session_id, before.target_account)
    checks = []

    def add(name, ok, detail, action):
        checks.append(dict(name=name, ok=bool(ok), detail=detail, next_step='' if ok else action))

    view = await service.inspect()  # Existing fresh, guarded, read-only inspection.
    state = view.inspection
    add('Устройство', state.adb == 'AVAILABLE' and state.device in {'CONNECTED','READY'},
        'Проверка подключения ADB.', 'Подключите телефон, разрешите отладку USB и выберите устройство.')
    add('Приложение', service.controller._profile == TIKTOK_YOU_4683 and state.profile_verified,
        'Проверка точной версии TikTok и установки.', 'Откройте поддерживаемый TikTok на проверенном телефоне.')
    add('Безопасность', not state.security_state, 'Проверка экрана входа и защитных диалогов.',
        'Остановитесь и самостоятельно завершите проверку TikTok; повторная отправка запрещена.')
    add('Свой аккаунт', state.account_verified, 'Используется действительная серверная привязка аккаунта.',
        'До эфира откройте собственный профиль с точным @handle и нажмите «Подготовить и проверить».')
    add('Экран эфира', state.live_verified and state.screen.value in {'LIVE_READY','COMMENT_INPUT_READY'},
        'Проверка собственного эфира или действительной привязки открытого поля.', 'Откройте обычный экран собственного эфира.')
    live = runtime.admin.live_view()
    add('Подключение к чату', live.connection_owned and live.state == 'connected' and live.activity == 'active',
        'Проверка текущего подключения к чату.', 'Сначала запустите эфир самостоятельно, затем подключите чат своего аккаунта.')
    engine = runtime.admin.playground.processor.engine
    model_ok = False
    if isinstance(engine, LMStudioReplyEngine):
        try:
            async with asyncio.timeout(8): status = await runtime.admin.check_ai()
            model_ok = status.available is True and status.model_found is True
        except Exception: pass
    add('Модель ИИ', model_ok, 'Проверка наличия модели на локальном сервере; генерация не выполняется.',
        'Запустите локальный сервер ИИ, загрузите выбранную модель и проверьте настройки профиля.')
    add('Настройки', not runtime.pending and not runtime.config_error and not runtime.closed,
        'Проверка применённой конфигурации.', 'Сохраните и примените настройки до начала эфира.')
    try: journal_ok = not unresolved(service, allow_rejected=True)
    except Exception: journal_ok = False
    add('Журнал отправок', journal_ok and not service.pending_fill and not service.paused,
        'Старые использованные сообщения не разблокируются.',
        'Проверьте результат старой операции и выполните существующее безопасное восстановление; старый ответ не повторяйте.')
    profile = runtime.project.ai_profile
    cfg = profile.periodic_comments if profile else None
    periodic_ok = bool(profile and (not cfg.enabled or cfg.mode == 'ai'
        or cfg.templates and all(periodic_statement(t) for t in cfg.templates)))
    add('Периодические сообщения', periodic_ok, 'Только нейтральные английские утверждения без вопросов.',
        'Настройте тему и нейтральные шаблоны профиля; сохраните и примените изменения до эфира.')
    manual_ok = bool(service.account_proof.manual_accepted or service.unknown_recovery.authorized())
    spent = service.settings.mobile_acceptance_one_shot and service.acceptance_spent
    admission_step = ('Лимит единственной отправки в этом запуске исчерпан. Повторное включение запрещено.' if spent else
        'Физическая отправка выключена при запуске. Включение допуска требует отдельного разрешения и перезапуска.' if not service.settings.enable_physical_tiktok_send else
        'Пройдите ручную проверку отправки и подтверждение кнопки. Автоотправка включается отдельно оператором.')
    add('Допуск отправки', service.settings.enable_physical_tiktok_send and manual_ok
        and not (service.settings.mobile_acceptance_one_shot and service.acceptance_spent)
        and service.device_ready(service.context()),
        'Проверка разрешения запуска, ручного допуска и подтверждённой кнопки отправки.',
        admission_step)
    after = service.context()
    unchanged = (after.active_runtime_revision, after.live_session_id, after.target_account) == binding
    add('Текущая сессия', unchanged, 'Проверка неизменности сессии во время диагностики.',
        'Сессия или настройки изменились. Повторите проверку готовности.')
    failed = next((c for c in checks if not c['ok']), None)
    return dict(checks=checks, ready=failed is None, active_runtime_revision=after.active_runtime_revision, live_session_id=after.live_session_id,
        auto_enabled=service.mode == 'AUTOMATIC',
        next_step='Сессия или настройки изменились. Повторите проверку готовности.' if not unchanged else
            failed['next_step'] if failed else 'Проверки пройдены. Включите автоотправку отдельным действием, когда будете готовы.')
