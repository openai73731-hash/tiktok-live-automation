"""PC-SEND-2: fake desktop, actual keyboard encoding, fake SDK receipts only."""
import asyncio
import ctypes
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from TikTokLive.events import CommentEvent

from app.config import Settings, WindowsSettings
from app.delivery.journal import SQLiteDeliveryOperationStore
from app.delivery.models import DeliveryContext, DeliveryRequest, ManualDeliveryInput
from app.live.listener import LiveListener, comment_from_event
from app.live.models import Comment, LiveStatus
from app.main import create_app
from app.windows.native_input import Input, NativeInput, unicode_events
from app.windows.sender import WindowsLiveStudioReplySender
from app.windows.service import WindowsManualDeliveryService
from app.windows.target import DesktopSnapshot, TargetRejected, Window, resolve
from test_predelivery_hardening import suggestion

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]


@pytest.fixture(autouse=True)
def forbid_native_io(monkeypatch):
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: pytest.fail('Real native API forbidden'))


def desktop():
    return DesktopSnapshot(99, '99-start1', (
        Window(100, 99, 'Chrome_WidgetWin_1'),
        Window(200, 99, 'Chrome_WidgetWin_1', owner=100, topmost=True),
        Window(201, 99, 'Chrome_RenderWidgetHostHWND', parent=200),
    ))


class FakeWatch:
    def __init__(self, native, notify, invalid):
        self.native, self.notify, self.invalid = native, notify, invalid
        self.started = self.closed = False
    async def start(self): self.started = True
    async def close(self): self.closed = True


class FakeNative(NativeInput):
    """Uses production fill/enter/encoding, replaces every OS boundary."""
    def __init__(self, config):
        super().__init__(config)
        self.current = desktop()
        self.watches, self.batches = [], []
        self.before_emit = self.after_emit = self.on_enter = None
    def snapshot(self): return self.current
    def watch(self, target, notify, invalidate):
        watch = FakeWatch(self, notify, invalidate); self.watches.append(watch); return watch
    def emit(self, events):
        if self.before_emit: self.before_emit()
        batch = [(e.kind, e.keyboard.vk, e.keyboard.scan, e.keyboard.flags) for e in events]
        self.batches.append(batch)
        if self.after_emit: self.after_emit()
        if batch[0][1] == 13 and self.on_enter: self.on_enter()
    def focus(self):
        self.current = replace(self.current, foreground_root=200, active_root=200, focus_root=200)
        self.watches[-1].notify('focus')
    def lose_focus(self):
        self.current = replace(self.current, foreground_root=0, active_root=0, focus_root=0)
        self.watches[-1].invalid.set(); self.watches[-1].notify('changed')
    @property
    def enters(self): return sum(batch[0][1] == 13 for batch in self.batches)


def configuration(**updates):
    config = WindowsSettings(executable_path=Path('C:/fixture/Studio.exe'), broadcaster_user_id='123')
    # Short test clocks only, production configuration is bounded separately.
    config = config.model_copy(update={'verification_seconds': .1, 'focus_watch_seconds': .3, 'cooldown_seconds': 0})
    return Settings(_env_file=None, tiktok_transport='windows_live_studio', windows=config, **updates)


def context():
    return DeliveryContext(live_session_id='a' * 32, target_account='own', active_runtime_revision='b' * 32,
        knowledge_revision='c' * 64, policy_revision='d' * 64, connected=True, active=True,
        device_available=True, delivery_available=True)


def request(text='Test Привет 😀', ctx=None):
    ctx = ctx or context()
    return DeliveryRequest(operation_id=uuid4().hex, idempotency_key='fixture', suggestion_id=uuid4().hex,
        suggestion_revision='c' * 64, comment_identity='d' * 64, approval_id=uuid4().hex,
        suggested_reply_hash='e' * 64, text=text, live_session_id=ctx.live_session_id,
        target_account='own', active_runtime_revision=ctx.active_runtime_revision,
        expires_at=datetime.now(UTC) + timedelta(minutes=5))


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate(): await asyncio.sleep(.001)


async def arm(sender, native):
    old = len(native.watches)
    task = asyncio.create_task(sender.arm(empty_confirmed=True))
    await until(lambda: len(native.watches) > old and native.watches[-1].started)
    native.focus()
    await task
    assert sender.state == 'ARMED'


@pytest.fixture
async def rig():
    settings = configuration(enable_physical_windows_send=True)
    native = FakeNative(settings.windows)
    sender = WindowsLiveStudioReplySender(settings, context, native=native)
    sender.preflight = lambda r: True
    yield SimpleNamespace(sender=sender, native=native, settings=settings)
    await sender.aclose()


def echo(sender, *, message_id='999', **updates):
    pending = sender.pending
    comment = Comment(username='own', author_handle='own', user_id='123', message_id=message_id,
        text=pending['text'], server_created_at=pending['wall'] + timedelta(milliseconds=1),
        timestamp=pending['wall'] + timedelta(milliseconds=1))
    comment._received_monotonic = pending['mono'] + .001
    return comment.model_copy(update=updates)


def verify_on_enter(rig):
    def emit():
        comment = echo(rig.sender)
        rig.sender.observe(comment, rig.sender.session, 'own')
        rig.sender.observe(comment, rig.sender.session, 'own')  # Receipt / AI callback duplicate.
    rig.native.on_enter = emit


def test_structural_target_has_no_geometry_or_fixed_ids():
    first = desktop(); target = resolve(first)
    assert (target.primary, target.root, target.renderer) == (100, 200, 201)
    shifted = replace(first, pid=771, instance='new', windows=tuple(replace(w, hwnd=w.hwnd+500,
        pid=771, parent=w.parent+500 if w.parent else 0, owner=w.owner+500 if w.owner else 0) for w in first.windows))
    assert resolve(shifted).root == 700
    # Owner relation is sufficient even when both windows have the same topmost state.
    assert resolve(replace(first, windows=tuple(replace(w, topmost=False) for w in first.windows))) == target


@pytest.mark.parametrize('mutation', ['extra', 'hidden', 'disabled', 'cloaked', 'renderer', 'wrong_pid', 'two_renderers', 'ambiguous_primary'])
def test_unknown_topology_rejected(mutation):
    d = desktop(); rows = list(d.windows)
    if mutation == 'extra': rows.append(Window(300, 99, '#32770'))
    elif mutation == 'renderer': rows.pop()
    elif mutation == 'two_renderers': rows.append(replace(rows[-1], hwnd=202))
    elif mutation == 'wrong_pid': rows[-1] = replace(rows[-1], pid=88)
    elif mutation == 'ambiguous_primary': rows = [replace(w, owner=0, topmost=False) for w in rows]
    else: rows[1] = replace(rows[1], **{{'hidden':'visible', 'disabled':'enabled'}.get(mutation, mutation): False if mutation != 'cloaked' else 1})
    with pytest.raises(TargetRejected): resolve(replace(d, windows=tuple(rows)))


async def test_default_inert_and_arm_requires_attestation_and_new_focus(rig):
    s, n = rig.sender, rig.native
    assert s.state == 'DISARMED' and not s.auto_enabled and not n.batches
    with pytest.raises(TargetRejected): await s.arm(empty_confirmed=False)
    n.current = replace(n.current, foreground_root=200, active_root=200, focus_root=200)
    with pytest.raises(TimeoutError): await s.arm(empty_confirmed=True)
    assert s.state == 'NEED_OPERATOR' and not n.batches
    await arm(s, n)
    assert s.target.instance == '99-start1' and s.get_status().foreground_guard


@pytest.mark.parametrize('failure', ['foreground', 'restart', 'destroy', 'renderer', 'modal', 'ttl', 'context', 'modifiers'])
async def test_arm_invalidation_before_any_input(rig, failure):
    s, n = rig.sender, rig.native
    await arm(s, n)
    if failure == 'foreground': n.lose_focus()
    elif failure == 'restart': n.current = replace(n.current, instance='99-start2')
    elif failure == 'destroy': n.watches[-1].invalid.set(); s._event('destroyed')
    elif failure == 'renderer': n.current = replace(n.current, windows=n.current.windows[:-1])
    elif failure == 'modal': n.current = replace(n.current, windows=n.current.windows + (Window(300, 99, '#32770'),))
    elif failure == 'ttl': s.armed_at -= 601
    elif failure == 'modifiers': n.current = replace(n.current, modifiers_clear=False)
    else: s.context = lambda: context().model_copy(update={'active_runtime_revision': 'f' * 32})
    assert not s.check()
    assert (await s.send(request())).status == 'needs_operator'
    assert s.state == 'NEED_OPERATOR' and not n.batches


@pytest.mark.parametrize('text', ['ASCII', 'Привет', 'a😀б'])
async def test_manual_unicode_fill_without_enter_gate(rig, text):
    s, n = rig.sender, rig.native
    s.settings = s.settings.model_copy(update={'enable_physical_windows_send': False})
    await arm(s, n)
    result = await s.send(request(text))
    assert (result.status, result.reason) == ('not_sent', 'filled_only')
    units = [e[2] for batch in n.batches for e in batch if e[3] == 4]
    expected = text.encode('utf-16-le')
    assert units == [int.from_bytes(expected[i:i+2], 'little') for i in range(0, len(expected), 2)]
    assert all(e[0] == 1 and e[1] == 0 and e[3] in {4, 6} for batch in n.batches for e in batch)
    if '😀' in text: assert any(len(batch) == 4 for batch in n.batches)
    assert n.enters == 0 and s.composer == 'FILLED_UNVERIFIED'
    assert (await s.send(request(text))).status == 'needs_operator'


@pytest.mark.parametrize('text', ['x\n', '\ud800', 'x' * 401])
async def test_invalid_whole_text_never_partially_typed(rig, text):
    await arm(rig.sender, rig.native)
    assert (await rig.sender.send(request().model_copy(update={'text':text}))).status == 'needs_operator'
    assert not rig.native.batches


async def test_guard_gate_false_prevents_all_send_once_input(rig):
    s, n = rig.sender, rig.native; await arm(s, n)
    s.operation_mode = 'send_once'
    s.settings = s.settings.model_copy(update={'enable_physical_windows_send': False})
    assert (await s.send(request())).status == 'not_sent'
    assert not n.batches


async def test_single_enter_and_strong_echo_confirmed(rig):
    s, n = rig.sender, rig.native; await arm(s, n)
    s.operation_mode = 'send_once'; verify_on_enter(rig)
    result = await s.send(request())
    assert (result.status, result.reason) == ('confirmed', 'echo_verified')
    assert n.enters == 1 and n.batches[-1] == [(1, 13, 0, 0), (1, 13, 0, 2)]
    assert s.composer == 'UNKNOWN' and s.last_send_status == 'SENT_CONFIRMED'
    n.lose_focus(); await asyncio.sleep(0)
    s.confirm_empty()  # Review after switching to Admin supplies no input authority.
    s.set_auto(True)
    await arm(s, n)
    assert s.auto_enabled and s.empty_after_verified_confirmed
    s.operation_mode = 'auto'
    n.on_enter = lambda: s.observe(echo(s, message_id='1000'), s.session, 'own')
    assert (await s.send(request())).status == 'confirmed'
    assert n.enters == 2 and s.composer == 'EMPTY_ATTESTED'


@pytest.mark.parametrize('bad', ['uid', 'handle', 'handle_missing', 'text', 'session', 'account', 'server_old', 'server_missing', 'receipt_old', 'local_old', 'duplicate', 'late'])
async def test_untrusted_stale_or_duplicate_echo_never_confirms(rig, bad):
    s, n = rig.sender, rig.native; await arm(s, n); s.operation_mode = 'send_once'
    def emit():
        changes = {'uid': {'user_id':'321'}, 'handle':{'author_handle':'viewer'}, 'handle_missing':{'author_handle':None}, 'text':{'text':'different'},
            'server_old':{'server_created_at':datetime(2020,1,1,tzinfo=UTC)}, 'server_missing':{'server_created_at':None},
            'local_old':{'timestamp':datetime(2020,1,1,tzinfo=UTC)}}.get(bad, {})
        c = echo(s, **changes)
        if bad == 'receipt_old': c._received_monotonic = s.pending['mono'] - 1
        if bad == 'duplicate': s.seen[(s.session, c.message_id)] = True
        if bad == 'late': s.pending['deadline'] = s.clock() - 1
        s.observe(c, 'other' if bad == 'session' else s.session, 'other' if bad == 'account' else 'own')
    n.on_enter = emit
    assert (await s.send(request())).status == 'unknown'
    assert s.state == 'NEED_OPERATOR' and s.composer == 'UNKNOWN' and n.enters == 1
    if bad in {'handle_missing','server_missing'}: assert s.verification == 'WEAK'
    assert not s.auto_enabled and (await s.send(request())).status == 'needs_operator'
    assert n.enters == 1


@pytest.mark.parametrize('change', ['focus', 'approval', 'partial'])
async def test_input_guard_after_every_await_and_before_enter(rig, change):
    s, n = rig.sender, rig.native; await arm(s, n); s.operation_mode = 'send_once'
    def after():
        if change == 'focus': n.lose_focus()
        elif change == 'approval': s.preflight = lambda r: False
        else: raise TargetRejected('partial_input')
    n.after_emit = after
    assert (await s.send(request('ab'))).status == 'unknown'
    assert len(n.batches) == 1 and n.enters == 0


async def test_guard_rechecked_between_last_character_and_enter(rig):
    s, n = rig.sender, rig.native; await arm(s, n); s.operation_mode = 'send_once'
    n.after_emit = lambda: setattr(s, 'preflight', lambda r: False)
    assert (await s.send(request('x'))).status == 'unknown' and n.enters == 0


def delivery_body(r, key='service_test'):
    return ManualDeliveryInput(suggestion_id=r.e.suggestion_id, expected_suggestion_revision=r.e.suggestion_revision,
        approval_id=r.approval.approval_id, idempotency_key=key)


@pytest.fixture
async def service(suggestion, tmp_path):
    settings = configuration(enable_physical_windows_send=True)
    native = FakeNative(settings.windows)
    service = WindowsManualDeliveryService(settings, suggestion.store, lambda: suggestion.context, native=native,
        journal_factory=lambda: SQLiteDeliveryOperationStore(tmp_path / 'windows.sqlite3'))
    yield service, native, suggestion
    await service.aclose()


async def test_reserved_before_fill_and_duplicate_never_typed(service):
    s, n, r = service
    assert s.journal is None
    await arm(s.sender, n)
    def before(): assert s.journal.lookup('service_test').status == 'reserved'
    n.before_emit = before
    result = await s._execute(delivery_body(r), 'fill')
    assert result.reason == 'filled_only' and s.journal.recent()[0].mode == 'MANUAL'
    count = len(n.batches)
    assert (await s._execute(delivery_body(r), 'fill')).reason == 'filled_only'
    await arm(s.sender, n)
    assert (await s._execute(delivery_body(r, 'other_key'), 'fill')).reason == 'rejected'
    assert len(n.batches) == count


async def test_service_auto_uses_real_approval_and_journal_mode(service):
    s, n, r = service; await arm(s.sender, n)
    s.sender.accepted_target = s.sender.target  # Fake acceptance, never OS.
    s.sender.set_auto(True)
    n.on_enter = lambda: s.observe_comment(echo(s.sender), r.context.live_session_id, 'own')
    s.approved(r.e.suggestion_id)
    await until(lambda: not s.auto_pending)
    assert n.enters == 1 and s.journal.recent()[0].status == 'confirmed'
    assert s.journal.recent()[0].mode == 'AUTO' and s.recent()[0].mode == 'AUTO'
    s.queue_approved(); await asyncio.sleep(.01)
    assert n.enters == 1


async def test_revocation_during_fill_stops_service_enter(service):
    s, n, r = service; await arm(s.sender, n)
    n.after_emit = r.store.approvals.clear
    assert (await s._execute(delivery_body(r), 'send_once')).status == 'unknown'
    assert n.enters == 0 and s.paused
    assert s.journal.recent()[0].status == 'unknown'


async def test_only_one_worker_and_stream_end_disarms(service):
    s, n, r = service; await arm(s.sender, n)
    entered, release = asyncio.Event(), asyncio.Event()
    original = n.fill
    async def slow(text, guard):
        entered.set(); await release.wait(); await original(text, guard)
    n.fill = slow
    one = asyncio.create_task(s._execute(delivery_body(r), 'fill'))
    await entered.wait()
    two = asyncio.create_task(s._execute(delivery_body(r), 'fill'))
    s.live_state(LiveStatus(state='ended', username='own', mode='real'))
    release.set(); await asyncio.gather(one, two)
    assert not n.batches and s.sender.state == 'NEED_OPERATOR'


async def test_explicit_workflow_and_lifespan_owned_cleanup(service):
    s, n, r = service
    s.start_workflow(empty_confirmed=True, body=delivery_body(r))
    await until(lambda: n.watches and n.watches[-1].started)
    n.focus(); await s.arm_task
    assert s.sender.last_send_status == 'FILLED_ONLY'
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_close(): entered.set(); await release.wait(); n.watches[-1].closed = True
    n.watches[-1].close = slow_close
    foreign = asyncio.create_task(asyncio.Event().wait())
    close = asyncio.create_task(s.aclose())
    await entered.wait(); close.cancel(); await asyncio.sleep(0); close.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await close
    assert not s.tasks and n.watches[-1].closed and not foreign.done()
    foreign.cancel(); await asyncio.gather(foreign, return_exceptions=True)


def test_native_input_layout_and_partial_return_never_retries():
    assert ctypes.sizeof(Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    native = NativeInput(WindowsSettings())
    calls = []
    def partial(count, array, size): calls.append((count, size)); return count - 1
    native.u = SimpleNamespace(SendInput=partial)
    with pytest.raises(TargetRejected, match='partial_input'): native.emit(next(unicode_events('x')))
    assert len(calls) == 1
    source = Path('backend/app/windows/native_input.py').read_text()
    for forbidden in ['SetForegroundWindow', 'SetFocus', 'WM_SETTEXT', 'WM_PASTE', 'OpenClipboard', 'SetClipboardData', 'mouse_event', 'VkKeyScan']:
        assert forbidden not in source


def fake_win32():
    """Exercise actual native snapshot/identity/SendInput marshalling without DLLs."""
    config = configuration().windows
    n = NativeInput(config); n.loaded = True
    n.enum_type = lambda callback: callback
    n.event_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_int32, ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32)
    calls = []
    def process(hwnd, out):
        if out is not None: out._obj.value = 99
        return 55
    def enum(callback, unused):
        for hwnd in (100, 200): callback(hwnd, 0)
        return 1
    def child(hwnd, cmd):
        return 201 if hwnd == 200 and cmd == 5 else 100 if hwnd == 200 and cmd == 4 else 0
    def klass(hwnd, buffer, size):
        buffer.value = 'Chrome_RenderWidgetHostHWND' if hwnd == 201 else 'Chrome_WidgetWin_1'
        return len(buffer.value)
    def gui(thread, out): out._obj.active = 200; out._obj.focus = 201; return 1
    def path(handle, flags, buffer, size): buffer.value = str(config.executable_path); return 1
    def times(handle, *out): out[0]._obj.dwLowDateTime = 1234; return 1
    def send(count, rows, size):
        calls.append(('input', count, size, [(rows[i].keyboard.scan, rows[i].keyboard.vk) for i in range(count)])); return count
    n.u = SimpleNamespace(EnumWindows=enum, GetWindowThreadProcessId=process, GetWindow=child,
        GetClassNameW=klass, IsWindowVisible=lambda h:1, IsWindowEnabled=lambda h:1,
        GetWindowLongW=lambda h,i:8 if h == 200 else 0, GetForegroundWindow=lambda:200,
        GetAncestor=lambda h,f:200 if h in (200,201) else 100, GetGUIThreadInfo=gui,
        GetAsyncKeyState=lambda k:0, SendInput=send,
        SetWinEventHook=lambda *a:len(calls)+1, UnhookWinEvent=lambda h:calls.append(('unhook',h)) or 1,
        PeekMessageW=lambda *a:0, DispatchMessageW=lambda *a:0)
    n.k = SimpleNamespace(OpenProcess=lambda *a:44, CloseHandle=lambda h:calls.append(('close',h)) or 1,
        QueryFullProcessImageNameW=path, GetProcessTimes=times,
        CreateMutexW=lambda *a:77, ReleaseMutex=lambda h:calls.append(('release',h)) or 1)
    def dwm(hwnd, attr, out, size): out._obj.value = 0; return 0
    n.d = SimpleNamespace(DwmGetWindowAttribute=dwm)
    return n, calls


async def test_native_snapshot_process_gui_and_actual_keyboard_marshalling():
    n, calls = fake_win32(); d = n.snapshot(); t = resolve(d)
    assert t.instance == '99-0-1234' and d.focus_root == d.active_root == d.foreground_root == 200
    assert ('close', 44) in calls
    await n.fill('Я😀', lambda: True); await n.enter(lambda: True)
    inputs = [v for v in calls if v[0] == 'input']
    assert [v[1] for v in inputs] == [2, 4, 2]
    assert inputs[-1][-1] == [(0,13), (0,13)]
    n.k.QueryFullProcessImageNameW = lambda h,f,b,s:setattr(b,'value','C:/other.exe') or 1
    with pytest.raises(TargetRejected, match='ambiguous_process'): n.snapshot()


async def test_native_external_hooks_destroy_latch_mutex_and_cleanup():
    import threading
    n, calls = fake_win32(); callbacks = []
    def hook(first, last, module, callback, pid, thread, flags):
        assert module is None and flags == 0
        callbacks.append(ctypes.cast(callback, n.event_type)); return len(callbacks)
    n.u.SetWinEventHook = hook
    invalid = threading.Event(); observed = []
    watch = n.watch(resolve(n.snapshot()), observed.append, invalid)
    await watch.start()
    assert len(callbacks) == 2
    callbacks[0](None, 3, 200, 0, 0, 55, 0)
    assert observed[-1] == 'focus' and not invalid.is_set()
    callbacks[-1](None, 0x8001, 201, 0, 0, 55, 0)
    assert invalid.is_set() and 'destroyed' in observed
    await watch.close()
    assert len([c for c in calls if c[0] == 'unhook']) == 2
    assert ('release',77) in calls and ('close',77) in calls and not watch.thread.is_alive()


async def test_cleanup_failure_permanently_blocks_rearm(rig):
    s, n = rig.sender, rig.native; await arm(s,n)
    async def failed_close(): raise TargetRejected('watch_cleanup_failed')
    n.watches[-1].close = failed_close
    s.invalidate('foreground_lost')
    await until(lambda:s.cleanup_failed)
    s.invalidate('live_not_connected')
    with pytest.raises(TargetRejected, match='cleanup_failed'): await s.arm(empty_confirmed=True)
    assert len(n.watches) == 1


async def test_old_focus_callback_cannot_arm_new_watch(rig):
    s, n = rig.sender, rig.native; await arm(s,n)
    old = n.watches[-1]
    task = asyncio.create_task(s.arm(empty_confirmed=True))
    await until(lambda:len(n.watches) == 2)
    old.notify('focus'); await asyncio.sleep(.01)
    assert not task.done() and s.state == 'ARMING'
    n.focus(); await task


async def test_explicit_disarm_during_arm_is_disarmed(service):
    s,n,_ = service
    s.start_workflow(empty_confirmed=True)
    await until(lambda:n.watches)
    await s.disarm()
    assert s.sender.state == 'DISARMED' and not s.sender.auto_enabled and not n.batches


def test_windows_startup_configuration_and_strict_bounds(monkeypatch):
    from pydantic import ValidationError
    assert not Settings(_env_file=None).enable_physical_windows_send
    monkeypatch.setenv('WINDOWS__MANUAL_FILL_ENABLED','false')
    assert not Settings(_env_file=None).windows.manual_fill_enabled
    for key, value in [('arm_ttl_seconds',0), ('focus_watch_seconds',31), ('verification_seconds',float('nan')),
        ('cooldown_seconds',1), ('broadcaster_user_id','nickname')]:
        with pytest.raises(ValidationError): WindowsSettings(**{key:value})


def test_installed_sdk_identity_and_server_time_contract(settings):
    event = CommentEvent().from_dict({'user': {'id':'123', 'displayId':'own'}, 'common': {'msgId':'987'}, 'content':'echo'})
    event.user.id = 123; event.user.display_id = 'own'; event.user.nickname = 'not identity'
    event.common.msg_id = 987; event.common.create_time = int(datetime.now(UTC).timestamp() * 1000)
    event.content = 'echo'
    comment = comment_from_event(event)
    assert (comment.user_id, comment.author_handle, comment.message_id) == ('123', 'own', '987')
    assert comment.server_created_at is not None
    event.user.display_id = ''; event.common.create_time = 0
    missing = comment_from_event(event)
    assert missing.author_handle is None and missing.server_created_at is None


async def test_receipt_observer_precedes_full_ai_queue(settings):
    queue = asyncio.Queue(maxsize=1); queue.put_nowait(Comment(username='x', text='x'))
    listener = LiveListener(settings, queue)
    observed = []; listener.on_receipt = observed.append
    event = CommentEvent().from_dict({'user':{'nickname':'own'}, 'content':'echo', 'common':{'msgId':'991'}})
    listener.on_comment(event)
    assert len(observed) == 1 and queue.qsize() == 1


async def test_windows_routes_are_identity_only_csrf_and_inert(settings):
    app = create_app(settings.model_copy(update={'tiktok_transport':'windows_live_studio'}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            assert (await client.get('/api/admin/delivery/status')).json()['windows_sender']['state'] == 'DISARMED'
            assert (await client.post('/api/admin/windows/arm', json={'empty_confirmed':True})).status_code == 403
            session = (await client.get('/api/admin/session')).json()
            client.headers.update({'Origin':'http://127.0.0.1:8000', 'X-Admin-CSRF':session['csrf_token']})
            for path in ['arm', 'fill', 'send-once']:
                response = await client.post('/api/admin/windows/' + path, json={'empty_confirmed':True, 'text':'injected', 'hwnd':42})
                assert response.status_code == 422
            assert (await client.post('/api/admin/windows/auto', json={'enabled':True})).status_code == 409
            assert app.state.delivery.journal is None


async def test_configured_http_fill_arm_requires_fresh_manual_focus(settings, suggestion, tmp_path):
    app = create_app(settings.model_copy(update={'tiktok_transport':'windows_live_studio',
        'windows':configuration().windows, 'state_dir':tmp_path / 'state'}))
    async with app.router.lifespan_context(app):
        service = app.state.delivery; native = FakeNative(service.settings.windows)
        service.sender.native = native; service.suggestions = suggestion.store
        service.context = service.sender.context = lambda:suggestion.context
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            session = (await client.get('/api/admin/session')).json()
            client.headers.update({'Origin':'http://127.0.0.1:8000','X-Admin-CSRF':session['csrf_token']})
            response = await client.post('/api/admin/windows/fill', json={'empty_confirmed':True,
                'delivery':delivery_body(suggestion).model_dump()})
            assert response.status_code == 202
            await until(lambda:native.watches)
            assert not native.batches
            native.focus(); await service.arm_task
            assert service.sender.last_send_status == 'FILLED_ONLY' and native.enters == 0
            assert service.journal.recent()[0].suggestion_id == suggestion.e.suggestion_id


@pytest.mark.parametrize('bad', ['uid', 'acceptance', 'gate', 'live'])
async def test_auto_unavailable_without_every_gate(rig, bad):
    s,n = rig.sender,rig.native; await arm(s,n)
    if bad != 'acceptance': s.accepted_target = s.target
    if bad == 'uid': s.config = s.config.model_copy(update={'broadcaster_user_id':None})
    if bad == 'gate': s.settings = s.settings.model_copy(update={'enable_physical_windows_send':False})
    if bad == 'live': s.context = lambda:context().model_copy(update={'connected':False})
    with pytest.raises(TargetRejected): s.set_auto(True)
    s.operation_mode = 'auto'
    assert (await s.send(request())).status != 'confirmed' and not n.batches


async def test_native_enter_partial_and_verification_cancellation_consumes_reservation(service):
    s,n,r = service; await arm(s.sender,n)
    def partial(): raise TargetRejected('partial_input')
    n.on_enter = partial
    assert (await s._execute(delivery_body(r),'send_once')).status == 'unknown'
    assert n.enters == 1
    assert (await s._execute(delivery_body(r),'send_once')).status == 'unknown'
    assert n.enters == 1


async def test_cancel_after_enter_is_owned_unknown_and_never_retry(service):
    s,n,r = service; await arm(s.sender,n)
    s.sender.config = s.sender.config.model_copy(update={'verification_seconds':5})
    task = asyncio.create_task(s._execute(delivery_body(r),'send_once'))
    await until(lambda:n.enters == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert s.journal.recent()[0].status == 'unknown' and s.sender.target is None
    assert (await s._execute(delivery_body(r),'send_once')).status == 'unknown' and n.enters == 1


async def test_auto_serial_queue_waits_cooldown_before_first_attempt(service):
    from app.admin.service import record_for
    from app.delivery.models import ApprovalInput
    s,n,r = service; await arm(s.sender,n)
    s.settings = s.settings.model_copy(update={'windows':s.settings.windows.model_copy(update={'cooldown_seconds':.03})})
    s.sender.accepted_target = s.sender.target; s.sender.set_auto(True)
    count = 2000
    def receipt():
        nonlocal count
        count += 1; s.observe_comment(echo(s.sender, message_id=str(count)), r.context.live_session_id,'own')
    n.on_enter = receipt
    assert (await s._execute(delivery_body(r),'auto')).status == 'confirmed'
    comment = Comment(username='viewer2',text='Is it automatic?',message_id='124')
    reply = await r.processor.process(comment)
    e = r.store.register(record_for(comment,reply,'tiktok',2,uuid4().hex),comment,r.processor,
        r.context.active_runtime_revision,session_id=r.context.live_session_id,target_account='own')
    approval = r.store.approve(e.suggestion_id,ApprovalInput(expected_suggestion_revision=e.suggestion_revision),r.context)
    body = ManualDeliveryInput(suggestion_id=e.suggestion_id,expected_suggestion_revision=e.suggestion_revision,
        approval_id=approval.approval_id,idempotency_key='second_auto')
    assert (await s._execute(body,'auto')).status == 'confirmed' and n.enters == 2


async def test_journal_failure_blocks_focus_watch_before_arming(service):
    s,n,_ = service
    def broken(): raise OSError('private database error')
    s.journal_factory = broken
    from app.delivery.suggestions import SuggestionConflict
    with pytest.raises(SuggestionConflict): s.start_workflow(empty_confirmed=True)
    assert s.paused and s.sender.target is None and not n.watches


async def test_full_owned_queue_does_not_leak_auto_pending_identity(service):
    s,n,r = service; await arm(s.sender,n)
    s.sender.accepted_target = s.sender.target; s.sender.set_auto(True)
    blocked = {asyncio.create_task(asyncio.Event().wait()) for _ in range(8)}
    s.tasks.update(blocked)
    s.approved(r.e.suggestion_id)
    await until(lambda:not s.auto_pending)
    assert not n.batches
    for task in blocked: task.cancel()
    await asyncio.gather(*blocked,return_exceptions=True)
    s.tasks.difference_update(blocked)


def test_browser_fill_workflow_and_default_off_gate_are_identity_only():
    node = shutil.which('node')
    if not node: pytest.skip('Optional Node regression')
    source = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'); const els=new Map();
const el=()=>({children:[],listeners:{},value:'',append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},addEventListener(k,f){this.listeners[k]=f}});
const document={getElementById(id){if(!els.has(id))els.set(id,el());return els.get(id)}};
const window={confirm:()=>true};
vm.runInNewContext(fs.readFileSync('backend/app/static/admin/delivery.js','utf8'),{window,document,crypto:require('crypto'),Date});
const status={transport:'windows_live_studio',instance_id:'vm1',windows_sender:{stage:'PC-SEND-2',state:'DISARMED',auto_enabled:false},physical_delivery_enabled:false};
const calls=[];const api=async(path,body)=>{calls.push({path,body});return path.endsWith('/history')?[]:status};
const delivery=window.createDeliveryControls({api,node:(_,__,s)=>Object.assign(el(),{textContent:s}),toast(){},state:{connected:true},refreshCards(){}});
(async()=>{await delivery.refresh();assert(!document.getElementById('windows-auto').checked);assert(document.getElementById('windows-auto').disabled);
const record={delivery_review:{approval_state:'approved',approval:{approval_id:'a'.repeat(32)},text_entry_supported:true,
envelope:{source:'tiktok',status:'ready',suggestion_id:'b'.repeat(32),suggestion_revision:'c'.repeat(64),expires_at:new Date(Date.now()+60000).toISOString(),suggested_reply:'NEVER BROWSER AUTHORITY'}}};
const send=el(),actions=el();delivery.attach(actions,record,send);
assert(!send.disabled);assert(actions.children[0].disabled);assert(send.textContent.includes('Fill'));
await send.listeners.click();const fill=calls.find(c=>c.path.endsWith('/fill'));assert(fill.body.empty_confirmed);
assert.deepStrictEqual(Object.keys(fill.body.delivery).sort(),['approval_id','expected_suggestion_revision','idempotency_key','suggestion_id']);
assert(!JSON.stringify(fill.body).includes('NEVER BROWSER AUTHORITY'));
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node,'-e',source],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
