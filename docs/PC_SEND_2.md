# PC-SEND-2: guarded Windows delivery

This is a production-oriented implementation, **not a real-device acceptance claim**.
All development validation is offline. No real LIVE, Studio click, SendInput or
Chromium AX probe was executed by the agent. PocketWin and the AI algorithms are unchanged.

## Architecture and authority

`LiveListener → LiveService → CommentProcessor → AIReplyEngine → ReplySuggestion →
policy + operator approval → WindowsManualDeliveryService → DeliveryCoordinator →
SQLite reservation → WindowsLiveStudioReplySender → NativeInput`.

The sender receives immutable server text; browser requests contain only suggestion,
revision, approval and idempotency identities plus explicit empty-field attestation.
The existing monotonic expiry, account/session/runtime/knowledge/policy checks run
after reservation and before **every** Unicode scalar batch and the single Enter.
AUTO consumes only already approved eligible LIVE suggestions. It does not remove
mandatory translation review/approval or change `auto_send_eligible=false` in AI output.

The pure target resolver requires one exact configured executable instance (and PID
when configured), exactly two visible enabled uncloaked `Chrome_WidgetWin_1` roots,
a unique primary (owner relation, otherwise unique non-topmost main root), and one
visible enabled direct `Chrome_RenderWidgetHostHWND` child of the secondary in the
same PID. It uses no coordinates, sizes, window titles, fixed IDs or exact z-order.
Unexpected visible target roots, ambiguity, process start-time changes or renderer
changes reject the target. Inner Chromium DOM is not claimed to be readable.
Security UI inside unchanged Chromium topology cannot be recognized by this boundary;
the operator must inspect the composer and stop on login/verification/security UI.

Construction, startup, GET and SSE perform no desktop I/O and create no journal.
Explicit ARM starts external WinEvent hooks and a bounded 15-second focus watch.
ARM requires an event from this watch plus fresh foreground/active/focus roots all
equal to the secondary. The operator must click the empty field manually after READY.
PID, process start identity, root/renderer and monotonic ARM age form the lease.
A per-instance Windows named mutex excludes another backend's worker. Event-loop
locks serialize all delivery; hook threads only latch invalidation and queue events.
Old watch generations cannot arm a new lease. A 100-ms monitor and fresh native
checks before each input enforce topology/focus/TTL. The final check and OS input
cannot be made globally atomic with another application's foreground changes;
external hooks/latch reduce that race, without claiming an OS-level guarantee.

`DISARMED → ARMING → ARMED → FILLING → FILLED` for manual Fill.
Optional Enter continues `SENDING → VERIFYING → ARMED/SENT_CONFIRMED`.
Loss, expiry, unknown topology or delivery error invalidates ARM and disables AUTO;
cleanup failure enters ERROR and blocks re-arm until backend restart. No focus
restoration, automatic re-arm, clipboard, Ctrl+A/Delete, mouse, WM_PASTE or WM_SETTEXT.
UTF-16 uses KEYEVENTF_UNICODE down/up; surrogate pairs stay in one guarded batch.
One VK_RETURN down/up batch is the entire activation attempt. Partial/zero API
returns and cancellation are never retried. No helper or additional package is installed.

## Composer and delivery result

There is no readback. Initial EMPTY is an **operator attestation**, not a native proof.
Fill-only sets `FILLED_UNVERIFIED`; the journal records `not_sent / filled_only` because
the backend did not send. Its suggestion/comment dedup is consumed before typing,
even when filling later fails. The operator may manually send/clear the field, but
the backend never retries that identity. Any uncertain input can leave partial text:
inspect and clear it manually before an explicit new ARM/new suggestion.

The first verified Enter still leaves composer UNKNOWN. Within 60 seconds, the
operator can switch to Admin and explicitly confirm that this VERIFIED send cleared
the field. This acceptance binds to the same Studio process/root/renderer and LIVE
session/runtime. It survives ordinary focus loss only to permit review and fresh
manual ARM; it grants no input while disarmed. Restart/APPLY/session/security/error
invalidates it. Subsequent verified sends may then assume expected EMPTY for AUTO.

The listener passes receipts to an observer **before queueing or AI work**, even if
the AI queue is full. Confirmation requires all of: configured numeric broadcaster
UID, SDK `user.display_id` matching the connected own handle (never nickname), exact
NFC-normalized/trimmed text, fresh nonzero SDK `common.msg_id`, current session/account,
local monotonic receipt and wall receipt after attempt, and SDK `common.create_time`
strictly after the send attempt within the verification window. Receipt dedup is
bounded to 1,000 session/message pairs. Reconnect duplicates and other users' same
text never confirm. Internal reconnect retains a session; full reconnect replaces it.

The installed SDK exposes int64 create_time but does not document its precision here.
The adapter accepts plausible Unix seconds or milliseconds; absent/implausible time
is unavailable. **A second-resolution timestamp in the same second as the send is
insufficient evidence and yields UNKNOWN**, even if the operator sees the message.
No clock rollback or late/replayed event can revive an attempt. Missing UID blocks
Enter/AUTO; missing handle/time or ambiguous receipt cannot confirm. WEAK/UNAVAILABLE
is not successful delivery. Strong own echo, not SendInput return, yields confirmed.
This is stream correlation, not cryptographic attribution: the broadcaster must not
simultaneously type the identical reply from another client during verification.

Timeout (default 10 seconds) or cancellation after possible input records UNKNOWN,
disarms, pauses delivery and forbids retry. Reserved rows after a crash also block retry.
The fixed ignored journal is `STATE_DIR/windows-delivery.sqlite3`, bounded to 10,000
metadata rows, never reply/original text. Manual and AUTO mode are recorded. Shutdown
owns only this service's tasks/hooks/mutex/coordinator and settles despite repeated
caller cancellation. Incoming comments never depend on sender readiness.

## Startup configuration (PowerShell)

Set trusted local startup values, using the actual installed executable, running PID
and verified numeric own broadcaster UID. A UID is not a display name; if it is not
known reliably, use fill-only. Browser cannot set any of these values.

```powershell
$env:TIKTOK_TRANSPORT = 'windows_live_studio'
$env:WINDOWS__EXECUTABLE_PATH = 'C:/absolute/path/to/TikTok LIVE Studio.exe'
$env:WINDOWS__PROCESS_ID = '<actual PID>'
$env:WINDOWS__BROADCASTER_USER_ID = '<verified own numeric UID>'
$env:WINDOWS__MANUAL_FILL_ENABLED = 'true'
$env:WINDOWS__ARM_TTL_SECONDS = '600'
$env:WINDOWS__FOCUS_WATCH_SECONDS = '15'
$env:WINDOWS__VERIFICATION_SECONDS = '10'
$env:WINDOWS__COOLDOWN_SECONDS = '3'
$env:ENABLE_PHYSICAL_WINDOWS_SEND = 'false'
$env:ENABLE_PHYSICAL_TIKTOK_SEND = 'false'
$env:ADMIN_LIVE_ENABLED = 'false'
$env:STATE_DIR = 'work/pc/data'
$env:LOG_DIR = 'work/pc/logs'
& ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

PID is optional if exactly one executable instance matches; ambiguous instances fail.
All time limits are bounded at validation. Manual fill defaults true, Enter defaults
false and AUTO always defaults OFF. Enabling Enter requires setting the startup flag
true and restarting the backend; this does not arm or enable AUTO. Connect remains an
explicit operator action for the own handle. Serve only loopback; existing Host,
same-origin, CSRF and sanitized UI/SSE guards remain in force.

## Admin workflow

The Windows panel shows state, instance/PID, armed age, foreground guard, composer,
last send/verification and Enter gate. **Arm Windows chat** is a standalone focus test.
**Disarm** stops the lease and AUTO. For an approved LIVE card, **Fill approved reply**
queues that immutable identity and starts its own explicit ARM watch. Clicking Admin
necessarily takes focus away from Studio, so Fill does not reuse an old ARM.
After Focus watch READY, click the empty composer within the displayed interval.
Fill inserts text only. **Guarded send once** uses the same workflow plus one Enter
and bounded echo verification; it requires the startup gate and own UID.

**Confirm empty after VERIFIED** is explicit first-send acceptance. **Auto-send approved
replies** requires that acceptance and the Enter gate. Toggle ON, then use fresh manual
ARM and click the empty composer; no automatic re-arm occurs. New approval while
disarmed cannot type. Switching back to Admin disarms; never click it while expecting
continuous armed operation. Inspect the result after the bounded verification finishes.

## One acceptance LIVE, performed only by the operator

Do all three phases in **one own LIVE**; no real discovery probe is needed. Before
starting, configure the actual executable/PID/UID, choose fake or the existing local
AI provider, and explicitly set `ENABLE_PHYSICAL_WINDOWS_SEND=true` for phases 2/3.
AUTO remains OFF. Connect the listener manually to the same own handle. Keep other
clients from sending identical text. Ensure no login/security/verification dialog.
There should be exactly one unused approved reply for each sending phase; do not
approve a backlog before enabling AUTO. Keep focus in Studio through verification.

1. **Fill-only:** obtain a LIVE suggestion through the existing pipeline and review/
   approve it. Click **Fill approved reply**, attest the field is empty, wait for READY
   and manually click the composer. Check exact text (including a supported non-ASCII
   example if available), no Enter and no sent message. Expected FILLED_ONLY. Manually
   send or clear the field; this reply cannot be used again by the backend. Separate
   offline tests cover fill with the physical gate false.
2. **Single guarded send:** obtain/approve a fresh LIVE suggestion. With empty field,
   click **Guarded send once**, attest, wait READY and click the composer manually.
   Observe exactly one Enter and one own chat message. Wait up to 10 seconds without
   changing focus, then inspect Admin: require SENT_CONFIRMED / VERIFIED. Inspect that
   the composer cleared, then click **Confirm empty after VERIFIED** within 60 seconds.
   Moving to Admin disarms and does not invalidate the reviewable confirmed evidence.
3. **One AUTO reply:** one viewer supplies one new test comment in the same LIVE.
   Existing AI generates the reply; operator reviews both translations and approves
   it. Enable AUTO, click **Arm Windows chat**, attest empty, wait READY and manually
   click the composer. Exactly this one unused approved suggestion should fill, Enter
   once and confirm by own echo. After verification inspect SENT_CONFIRMED / VERIFIED
   and one AUTO journal row. Turn AUTO OFF / **Disarm**, then disconnect as desired.

**Any phase failure, unexpected text/UI, WEAK/UNKNOWN or timeout: STOP / DISARM.**
Inspect manually; no automatic retry, extra Enter, automated probe or second acceptance
LIVE is part of this procedure. Unit/fixture success does not prove real IME behavior,
the observed window topology, SDK timestamp precision or composer clearing.

## PC-SEND-1H diagnostics

SendMessageTimeout's API return determines delivery: nonzero is completed even if
the target's out LRESULT is zero. Zero API return is timeout only for error 1460,
otherwise failed. The fake C# harness covers all three; no real Chromium retest is
required. This diagnostic fix is independent of keyboard sender readiness.

## Offline checks

`scripts/test-fast.ps1` includes the Windows native-discovery and sender harnesses.
`scripts/test-pc.ps1` adds Android, policy/approval, admin and runtime regression suites.
Also run `python -m pytest -q`, `python -m pip check` and finite fake CLI with all
physical gates false. `RUN_LIVE_STUDIO_E2E` stays unset/0. Sender tests trap WinDLL
loading and inject desktop/hooks/input/receipts; no OS keyboard action is allowed.


## Exact changed-file manifest

Paths are repository-relative; this list covers the single PC-SEND-2 commit.

- `.env.example`
- `backend/app/admin/events.py`
- `backend/app/admin/router.py`
- `backend/app/admin/runtime.py`
- `backend/app/admin/service.py`
- `backend/app/config.py`
- `backend/app/delivery/coordinator.py`
- `backend/app/delivery/factory.py`
- `backend/app/delivery/journal.py`
- `backend/app/delivery/models.py`
- `backend/app/delivery/service.py`
- `backend/app/live/listener.py`
- `backend/app/live/models.py`
- `backend/app/live/service.py`
- `backend/app/main.py`
- `backend/app/static/admin/app.js`
- `backend/app/static/admin/delivery.js`
- `backend/app/static/admin/index.html`
- `backend/app/windows/ChromiumDiscovery.cs`
- `backend/app/windows/native_input.py`
- `backend/app/windows/sender.py`
- `backend/app/windows/service.py`
- `backend/app/windows/target.py`
- `backend/tests/conftest.py`
- `backend/tests/fixtures/windows/FakeChromiumProbe.cs`
- `backend/tests/test_unknown_events.py`
- `backend/tests/test_windows_native_discovery.py`
- `backend/tests/test_windows_sender.py`
- `docs/PC_LIVE_STUDIO_ARCHITECTURE.md`
- `docs/PC_SEND_2.md`
- `README.md`
- `scripts/test-fast.ps1`
- `scripts/test-pc.ps1`

## Validation evidence

- Focused Windows sender/API/native-boundary suite: 68 passed.
- Tier A: 280 passed / 1 opt-in real UI test skipped; Tier B: 642 passed / 1 skipped.
- Subsequent final sender regressions (WEAK receipt and full owned queue) passed in the focused suite and final full suite.
- pip check: no broken requirements; finite fake CLI: two ready, one skipped, exit 0, no send.
- Secret/runtime scan: 33 changed text files; no credential-pattern findings or runtime paths.
- Existing unknown-event log tests were synchronized with the pre-existing safe instance_id field; strict metadata/redaction assertions remain.
- Real acceptance remains operator-only and unexecuted. Two existing SDK/websockets deprecation warnings remain.

Primary native API references: [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput), [KEYBDINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput), [SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook), [SendMessageTimeout](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw).

- Final full pytest suite: 1371 passed, 1 skipped; final focused sender suite after foreground-event refinement: 68 passed.
