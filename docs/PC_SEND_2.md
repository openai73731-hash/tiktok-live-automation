# PC-SEND-2B: release hardening of guarded Windows delivery

## CURRENT WINDOWS RELEASE STATE

Release scope: own LIVE only, one operator, one Studio instance, EN validated path,
manual review/approval, one-shot acceptance and limited load. AUTO defaults OFF;
physical gates default OFF. The launcher explicitly opts into the single operator
acceptance profile. Local Windows VM and its filesystem are trusted; Admin stays
loopback-only. The OS focus-check/SendInput race remains. Echo correlation is not
cryptographic proof. Real LIVE and physical input are NOT part of offline validation.
Long-running log retention, FR/DE model timeouts and sustained-load/stale AI work
remain scoped limitations (audit M7–M9), not claims of production validation.

PC-SEND-2B supersedes the 2A findings below. Mandatory audit H1/H2 and M1–M6 are
addressed by ingestion provenance, terminal clock failure, own-account binding,
per-attempt preparation evidence and consumed/UNKNOWN UI. Historical validation
counts later in this guide describe their named stages only.

Final readiness recovery: `latest.json` is the only current attempt authority;
`last-ready.json` is an untrusted-as-readiness reusable candidate. UAC cancellation
preserves the candidate but publishes current CANCELLED. A later attempt revalidates
process PID/start/executable/command/ancestry, port ownership, HEAD, Studio identity,
current integrity, health/account/gates and the backend startup fingerprint/nonce.
It publishes a new READY with `reused_backend=true` and `reused_from_launch_id`.
Dead/incompatible candidates are invalidated; foreign port owners are untouched.
One bounded publication lock orders generation changes and atomic latest/last-ready
updates; late or cancelled children cannot overwrite a newer generation.
An open Admin receives changed `preparation_status` via existing SSE, with read-only
checks at most once per two seconds per connected page. PENDING/FAILED/CANCELLED
remove PREPARED without reload; disconnect also removes it. `last-ready` alone can
never show PREPARED. There is no browser polling or inference on this refresh.

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
AUTO consumes only new, approved, server-authorized LIVE suggestions in the current
AUTO epoch. Mandatory translation review/approval remains in force. AI's legacy
`ReplySuggestion.auto_send_eligible` is an advisory literal false and grants no
physical authority; AI JSON cannot set it true. The authoritative replacement is
`SuggestionView.auto_delivery: AutoDeliveryEligibility`, computed by
`WindowsManualDeliveryService.auto_eligibility` after policy, approval, current LIVE,
account/runtime, freshness, epoch, unused identity, physical gate, composer acceptance
and ARM checks. Only this server policy may return eligible=true; no browser mutation
can supply it. It is rechecked before reservation and each input preflight. Manual
fill/send-once uses its own existing approval policy and does not require AUTO rights.

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
checks before each input enforce topology/focus/TTL. Every native emission marshals
its batch, performs authoritative coordinator/SQLite preflight, then performs the
FINAL EMISSION GUARD: fresh process/start identity, secondary/renderer, visible/enabled/
uncloaked topology, foreground/active/focus roots, latch, TTL, session/runtime and
pending task cancellation. The latch/cancellation are reread after native inspection.
There is no await or further preflight after this guard before SendInput. Focus loss
inside SQLite/preflight means zero subsequent physical input, including Enter.
There is still an unavoidable residual OS race between the last GetForegroundWindow/
GetGUIThreadInfo check and SendInput: Windows provides no transaction tying these
operations together. Hooks/latch reduce that race; they do not make it atomic.

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
An ARM clock anchor and an attempt bridge each capture monotonic + wall time.
The attempt is checked against ARM, and receipt/current wall progress against the
attempt's monotonic progress. Absolute divergence above 250 ms is CLOCK_UNSTABLE:
no VERIFIED, no correction/retry. This catches jumps before and after Enter, including
a stale unseen message arriving after rollback. Same-second server timestamps still
cannot confirm. Stable local clocks do not independently prove server clock alignment
or cryptographic attribution; ambiguous/missing server evidence remains UNKNOWN.
Missing UID blocks
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

The history's `comment_identity` remains session-bound. A separate versioned
`durable_delivery_key` hashes source kind/version + case-folded own account + stable
SDK msg_id, never the transient session/runtime UUID. A unique SQLite index consumes
it transactionally before typing. New session/runtime/approval cannot unlock
filled_only, UNKNOWN, reserved/crashed or confirmed comments. Without a stable SDK ID,
the existing conservative session-bound receipt fingerprint remains in use; no global
dedup claim is made. Migration adds a nullable durable-key column without deleting
rows. For legacy rows, the journal recomputes the old SDK hash using each stored
session/account and the incoming SDK ID. Matching old reservations remain blocking;
no source IDs or reply text are persisted by the migration.

AUTO OFF -> ON creates an opaque epoch ID, monotonic activation time, a trusted
ingestion watermark and a store/approval watermark. Listener conversion stamps the
private monotonic receipt and ingestion sequence BEFORE queue/AI. The owned receipt
callback binds configured account, numeric broadcaster UID, LIVE session, runtime
and SDK message ID. The immutable private receipt travels with Comment through the
real processor into SuggestionStore; JSON cannot supply or serialize it. Registration
admits it only to the then-current epoch when receipt time and sequence follow that
epoch. Enqueue, post-lock/cooldown reservation checks and every physical preflight
revalidate the receipt, registration epoch and current scope. Delayed AI cannot turn
a pre-epoch comment into a fresh one. Equal clock readings are ordered by the shared
trusted sequence. Creation and approval must also follow their store watermark.
The runtime retains first receipts by account/UID/SDK ID across APPLY and full
reconnect. This ledger never evicts: at 10,000 identities, new IDs lose AUTO eligibility
while manual review remains available. No-ID comments cannot obtain AUTO provenance.
This is bounded process-local provenance; consumed dedup is separately durable.
Idempotent
re-approval cannot freshen an old approval. ARM may scan the bounded group, but filters
all pre-epoch suggestions/approvals. Queued tasks carry their original epoch and are
rechecked after lock/cooldown waits; OFF -> ON, full reconnect, APPLY or restart never
transfers old AUTO authority into a new epoch. Internal reconnect invalidates ARM/AUTO
and does not reset the watermark to admit old work.

`WINDOWS__ACCEPTANCE_ONE_SHOT=true` adds a process-lifetime AUTO Enter budget of one.
The budget is spent immediately before the first possible AUTO Enter, including
partial/zero returns. AUTO becomes OFF at that boundary, so no second queued item can
reserve/type/send. Only the bounded observer/watch remains during VERIFYING; input
authority is unavailable. On settlement, DISARM occurs regardless of VERIFIED,
UNKNOWN, error or cancellation. No subsequent ARM, session or AUTO toggle replenishes
the budget. A backend restart requires fresh manual acceptance and a fresh epoch.
Default false retains continuous mode with the same new-approval epoch policy;
continuous production AUTO is outside this release's validated one-shot scope.

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
$env:WINDOWS__ACCEPTANCE_ONE_SHOT = 'false' # Set true BEFORE the single acceptance LIVE.
$env:WINDOWS__ARM_TTL_SECONDS = '600'
$env:WINDOWS__FOCUS_WATCH_SECONDS = '15'
$env:WINDOWS__VERIFICATION_SECONDS = '10'
$env:WINDOWS__COOLDOWN_SECONDS = '3'
$env:ENABLE_PHYSICAL_WINDOWS_SEND = 'false'
$env:ENABLE_PHYSICAL_TIKTOK_SEND = 'false'
$env:ADMIN_LIVE_ENABLED = 'false'
$env:STATE_DIR = 'work/pc/data'
$env:LOG_DIR = 'work/pc/logs'
& ./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

PID is optional if exactly one executable instance matches; ambiguous instances fail.
Run from the repository root. On the prepared Windows VM, use
`scripts/start-acceptance.ps1` for the next single acceptance instead of setting these
values manually; it enables the Windows Enter gate and one-shot mode, checks local
AI and the Admin APIs, and leaves the listener disconnected and AUTO OFF.
All time limits are bounded at validation. Manual fill defaults true, Enter defaults
false and AUTO always defaults OFF. Enabling Enter requires setting the startup flag
true and restarting the backend; this does not arm or enable AUTO. Connect remains an
explicit operator action for the own handle. Serve only loopback; existing Host,
same-origin, CSRF and sanitized UI/SSE guards remain in force.

Windows CONNECT requires the applied own handle to equal trusted startup
`TIKTOK_USERNAME` (launcher profile: `d3pp1s`, UID `7412662672502555649`). Whitespace,
optional leading @ and ASCII handle case are normalized. Browser CONNECT also
requires the current `expected_active_runtime_revision`. Foreign handles and stale
tabs return 409 before listener/processor/session mutation. Changing the applied
account invalidates ARM/AUTO; CONNECT stays blocked if it conflicts with startup
identity. Account changes require a reviewed matching startup handle/UID pair.

### Launcher result and ownership contract

`scripts/start-acceptance.ps1 [-NoBrowser] [-PreparationTimeoutSeconds 300]` is the
single operator entry point. Clean `feature/windows-live-studio` is permitted for
pre-release acceptance; clean reviewed `main` is permitted after integration. Other
branches and detached commits are rejected. The script never merges, commits or
resets a journal. It never starts LIVE, connects, arms or emits input.

Before UAC, each attempt creates a unique ID and PENDING at ignored
`work/acceptance/startup/<id>.json` and `latest.json`. Atomic temp/replace publication
records start/finish UTC, HEAD/branch, Studio/backend PID and creation FILETIME strings,
integrity, local model/health, own account, numeric-UID presence, AUTO OFF, one-shot
availability and physical gate. Results are PENDING/READY/FAILED/CANCELLED with a
static sanitized failure reason. UAC cancel supersedes prior READY. Terminal results
cannot be revived by late child completion. Parent waits for the preparation result
only, bounded by the chosen timeout, never backend descendants. A timed-out child
checks cancellation before backend spawn and READY and cleans its owned backend.

Existing 8000 is reused only with the prior READY matching HEAD and both process
instances, expected repo venv ancestry and idle Admin state. Otherwise `port_in_use`
fails without stopping that owner. Cleanup rechecks only captured processes created
by this attempt: exact PID/start, image, command and parent. No cleanup scans a port
and kills whichever Python it finds. PocketWin 9000/9002/9003 remain untouched.

Admin status/SSE includes an allowlisted preparation projection. PRE-LIVE PREPARED
requires latest READY, current on-disk AND backend-loaded HEAD, backend launch ID,
and current Studio/backend PID/start/image. Restart or revision change invalidates
old evidence. Missing/stale/PENDING means NOT PREPARED; FAILED/CANCELLED means
PREPARATION FAILED. This evidence proves preparation, not ARM, LIVE activity or send.

## Admin workflow

The Windows panel shows state, instance/PID, armed age, foreground guard, composer,
last send/verification, clock status (stable/unstable/unavailable), AUTO epoch/watermark,
one-shot status and Enter gate. Each card shows server-side AUTO eligibility/reason.
**Arm Windows chat** is a standalone focus test.
**Disarm** stops the lease and AUTO. For an approved LIVE card, **Fill approved reply**
queues that immutable identity and starts its own explicit ARM watch. Clicking Admin
necessarily takes focus away from Studio, so Fill does not reuse an old ARM.
After amber **ARM READY — CLICK COMPOSER NOW**, click the empty composer within the
displayed interval. Otherwise the panel says **WAITING FOR COMPOSER FOCUS**. The AI
card's green **REPLY READY** grants no focus or delivery authority.
Fill inserts text only. **Guarded send once** uses the same workflow plus one Enter
and bounded echo verification; it requires the startup gate and own UID.

**Confirm empty after VERIFIED** is explicit first-send acceptance. **Auto-send approved
replies** requires that acceptance and the Enter gate. Toggle ON FIRST, then receive
and approve a NEW comment, then use fresh manual ARM and click the empty composer.
Approving while disarmed cannot type; ARM schedules only post-epoch work. No automatic
re-arm occurs. Switching back to Admin disarms; inspect after bounded verification.

UNKNOWN/timeout/uncertain consumed results retain a prominent **STOP — DELIVERY
RESULT UNKNOWN / DO NOT RETRY THIS REPLY** banner. Durable journal reads restore it
after UI/backend restart without creating/migrating the DB on GET. Cards expose
CONSUMED / NO RETRY and disable Fill, Guarded send and reapproval; AUTO's durable
unused-identity check rejects them too. Another ARM or approval never unlocks them.
Historical MANUAL UNKNOWN stays visible and consumed: inspect those replies
manually and use fresh identities for a later authorized acceptance.

CLOCK_UNSTABLE is terminal for the pending attempt. Its sticky latch clears verified,
revokes AUTO/ARM immediately and ignores all later otherwise-perfect echoes. After
possible input, coordinator/journal settle UNKNOWN (or reserved-safe on storage
failure); verification never returns VERIFIED. Cancellation does not release dedup
or replenish an AUTO one-shot SPENT budget. A clock bridge failure is never retried.

## One acceptance LIVE, performed only by the operator

Do all three phases in **one own LIVE**; no real discovery probe is needed. Before
starting, configure the actual executable/PID/UID, choose fake or the existing local
AI provider, explicitly set `ENABLE_PHYSICAL_WINDOWS_SEND=true` for phases 2/3 and
`WINDOWS__ACCEPTANCE_ONE_SHOT=true` BEFORE starting the backend for this LIVE.
AUTO remains OFF. Connect the listener manually to the same own handle. Keep other
clients from sending identical text. Ensure no login/security/verification dialog.
Require one-shot status AVAILABLE. Old approvals cannot enter AUTO. Keep focus in
Studio through verification and supply only one new test comment in phase 3.

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
3. **ONE-SHOT AUTO:** enable AUTO while in Admin; verify ACTIVE epoch and AVAILABLE
   one-shot budget. Only THEN one viewer supplies one new comment in the same LIVE.
   The existing AI/policy pipeline creates a new suggestion. Review both translations
   and approve it; server eligibility remains blocked until fresh ARM. Click
   **Arm Windows chat**, attest empty, wait READY and manually click the composer.
   The server authorizes only the new approved reply: fill -> at most one Enter ->
   bounded own-echo verification. AUTO turns OFF immediately at the attempt; after
   verification inspect SENT_CONFIRMED / VERIFIED, stable clock, SPENT budget,
   DISARMED state and exactly one AUTO journal row. Disconnect as desired. Do not
   enable AUTO again or restart to repeat a failed phase.

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

## PC-SEND-2A offline hardening evidence

The figures above describe the original PC-SEND-2 commit. PC-SEND-2A adds 28
regressions in `backend/tests/test_windows_hardening.py`, using the production
CommentProcessor, SuggestionStore/approval, DeliveryCoordinator, SQLite journal and
Windows sender together with a fake desktop/input boundary. Real WinDLL loading is
forbidden. The native encoder/fill/Enter path is shared with production.

Critical integration regressions include:

- `test_focus_loss_inside_real_sql_preflight_blocks_next_emission`: foreground loss
  inside real journal lookup at ARMED/FILLING/SENDING; zero first Unicode input or
  zero subsequent Enter, with settlement and no retry.
- `test_clock_jump_with_stale_unseen_sdk_echo_never_verifies` and
  `test_clock_bridge_keeps_strong_echo_requirements`: rollback before/after attempt,
  forward jump, unseen old SDK event, same-second time, missing time and old receipt.
  `test_stable_clock_bridge_and_explicit_composer_acceptance` covers normal success.
- `test_old_approved_backlog_never_crosses_auto_epoch_and_arm`,
  `test_new_epoch_rejects_old_queue_and_old_approvals`, and
  `test_trusted_auto_decision_is_rechecked_during_production_preflight`: old backlog
  stays untouched; fresh server-created/approved suggestions can be authorized;
  stale epochs or revoked server decisions cannot type. AI true is rejected.
- `test_one_shot_auto_consumes_budget_before_enter_and_leaves_second_unused` and
  `test_one_shot_cancel_after_enter_never_replenishes_budget`: confirmed/timeout/
  partial/cancelled attempts consume one budget, disable AUTO and disarm; the second
  queued suggestion remains unreserved. Acceptance setup itself uses a production
  manual send + observe + confirm_empty, without directly seeding accepted_target.
- `test_new_service_runtime_session_same_sdk_comment_remains_consumed`: service
  shutdown/recreation, new runtime/session, same SDK ID after filled_only/UNKNOWN/
  reserved, each against both current and migrated legacy SQLite schema.
  `test_two_real_journal_connections_new_session_keys_race_only_one_types` opens both
  connections before reservation and verifies one durable consumption/one writer,
  with no leaked transaction. Account/source separation and weak fallback are covered.
- `test_epoch_and_watermark_are_published_together` prevents a transient inconsistent
  SSE projection. API regressions reject browser-supplied eligibility/epoch/one-shot/
  executable/PID/UID/send-gate fields. Pending cancellation is checked before input.

Final offline results:

| Check | Result |
| --- | --- |
| Windows sender + hardening modules (included in tiers/full) | 96 passed |
| Pre-delivery / Admin / native discovery modules (included in full) | 75 / 51 / 184 passed |
| Tier A `scripts/test-fast.ps1` | 310 passed, 1 skipped |
| Tier B `scripts/test-pc.ps1` | 672 passed, 1 skipped |
| Full `python -m pytest -q` | 1399 passed, 1 skipped |
| Final Admin/JS check after wording cleanup | 52 passed |
| `pip check` | No broken requirements |
| Finite Windows-selected fake CLI | Exit 0; 2 ready, 1 skipped, STOPPED; no send |
| Secret/runtime scan and `git diff --check` | No findings |

The opt-in real UI test is skipped (`RUN_LIVE_STUDIO_E2E=0`). Physical Windows and
Android gates are false during validation; true-gate cases exist only with fake native
boundaries. Two existing SDK/websockets deprecation warnings remain. The default
Windows SQLite/WAL/SHM path is now ignored alongside the existing runtime paths.
REAL LIVE NOT RUN. Real SendInput, Chromium AX probe and discovery were not run.
Additional discovery LIVE required: NO. Real acceptance is still operator-only.

### PC-SEND-2A changed files

- `.env.example`
- `.gitignore`
- `README.md`
- `backend/app/admin/router.py`
- `backend/app/admin/service.py`
- `backend/app/ai/models.py`
- `backend/app/config.py`
- `backend/app/delivery/coordinator.py`
- `backend/app/delivery/journal.py`
- `backend/app/delivery/models.py`
- `backend/app/delivery/service.py`
- `backend/app/delivery/suggestions.py`
- `backend/app/static/admin/app.js`
- `backend/app/static/admin/delivery.js`
- `backend/app/static/admin/index.html`
- `backend/app/windows/native_input.py`
- `backend/app/windows/sender.py`
- `backend/app/windows/service.py`
- `backend/tests/test_predelivery_hardening.py`
- `backend/tests/test_windows_hardening.py`
- `backend/tests/test_windows_sender.py`
- `docs/PC_SEND_2.md`
- `scripts/test-fast.ps1`
- `scripts/test-pc.ps1`

## PC-SEND-2B delta-review evidence — 2026-10-12

Baseline audited HEAD: `e33fc6b87f36b3f00bc98ace1033829fb9365147`.
The delta stays on `feature/windows-live-studio`; main is not merged or modified.
Development validation uses `ENABLE_PHYSICAL_WINDOWS_SEND=false`,
`ENABLE_PHYSICAL_TIKTOK_SEND=false`, `RUN_LIVE_STUDIO_E2E=0`. Tests inject virtual
native boundaries; no real LIVE, SendInput, Enter, Send, messages, accessibility
discovery or UAC was run. Actual elevated launcher preparation remains an operator
action; these tests do not claim that the current VM is PRE-LIVE PREPARED.

New regressions (55 cases) live in `test_windows_release_hardening.py` and
`test_acceptance_launcher.py`, alongside the existing production sender suites:

| Finding | Exact exercised path / assertion |
| --- | --- |
| H1 A/B | Real SDK event conversion → LiveService consumer → delayed real CommentProcessor → store/approval → fresh ARM → real coordinator/SQLite/virtual native. Pre-epoch: zero AUTO reservation, Unicode or Enter; post-epoch: eligible and confirmed |
| H1 C/D | Equal monotonic readings ordered by trusted ingestion sequence; old receipts blocked after serial lock/cooldown and after a new epoch while waiting |
| H1 E/F | Full reconnect/APPLY replay of the same SDK ID retains first receipt; scope cannot be rebound; bounded ledger never evicts; browser cannot supply provenance/epoch; serialized envelope loses private authority |
| H2 | Unstable receipt before deadline → one/multiple perfect echoes, with/without cancellation, in MANUAL and AUTO one-shot. Never VERIFIED/confirmed; journal UNKNOWN, no retry, OFF/DISARM, AUTO SPENT; normal strong echo still confirms |
| M1 | Actual Admin API accepts normalized d3pp1s/@/case/whitespace, binds UID/session/runtime; foreign/stale/missing-token CONNECT leaves runtime untouched; APPLY revokes ARM/AUTO and old token; existing echo-mismatch tests remain |
| M2–M5 | Stock PowerShell mocks test branch policy, PENDING before mocked UAC cancel, latest/per-attempt CANCELLED, immutable terminal failure against late READY, bounded wait, exact cleanup identities/PID reuse/ancestry races, and occupied foreign 8000 without stop |
| M3/M6 | Preparation projection requires current loaded/on-disk HEAD and exact Studio/backend instances; failed/cancelled/restarted evidence cannot show PREPARED. Read-only durable journal restores UNKNOWN/consumed; owned pending verification is not UNKNOWN. Node checks persistent STOP, disabled Fill/Guarded, CONSUMED badge, distinct focus state and no VERIFIED with unstable clock |

Validation totals: focused critical **332 passed**; Tier A **365 passed / 1 skipped**;
Tier B **727 passed / 1 skipped**; FULL **1454 passed / 1 skipped**. The skip is the
explicit real Studio E2E opt-in. Two existing SDK/websockets deprecation warnings
remain. `pip check`, finite fake CLI, `git diff --check` and secret/runtime scan pass.
An earlier VM timing interruption caused a Node subprocess timeout with a negative
remaining timeout; isolated and complete reruns passed without changing test limits.

### PC-SEND-2B changed files

- Contract/runtime: `backend/app/live/models.py`, `backend/app/delivery/models.py`,
  `backend/app/delivery/suggestions.py`, `backend/app/delivery/journal.py`,
  `backend/app/delivery/service.py`, `backend/app/admin/models.py`,
  `backend/app/admin/router.py`, `backend/app/admin/runtime.py`, `backend/app/admin/service.py`.
- Windows: `backend/app/windows/sender.py`, `backend/app/windows/service.py`,
  `backend/app/windows/preparation.py`.
- UI: `backend/app/static/admin/app.js`, `delivery.js`, `operator.js`, `index.html`, `styles.css`.
- Launcher/validation: `scripts/start-acceptance.ps1`, `scripts/acceptance-launch-helpers.ps1`,
  `scripts/test-fast.ps1`, `scripts/test-pc.ps1`.
- Tests: `backend/tests/test_windows_sender.py`, `backend/tests/test_windows_release_hardening.py`,
  `backend/tests/test_acceptance_launcher.py`.
- Documentation: `.env.example`, `README.md`, `docs/PC_SEND_2.md`,
  `docs/PC_LIVE_STUDIO_ARCHITECTURE.md`.

## Final readiness recovery fix — 2026-10-12

M1 (stale preparation in an open Admin page) and M2 (backend reuse after UAC
cancel) are closed by the offline regression paths below. The earlier totals
above describe the preceding hardening review; the final totals include this fix.

- M1: the existing SSE connection checks the authoritative preparation projection
  at most once every two seconds. The same open page handles PENDING, CANCELLED,
  FAILED and a subsequent READY without reload; connection loss clears PREPARED.
- M2: `latest.json` records only the current attempt. `last-ready.json` retains a
  reuse candidate across cancellation, but cannot establish readiness. A fresh
  attempt revalidates process creation identity, executable/command/venv ancestry,
  repository/HEAD, port owner, health, current Studio identity and integrity,
  account/UID and immutable physical startup configuration before publishing its
  own READY with `reused_backend` and `reused_from_launch_id`.
- The shared publication lock orders generation changes and atomic writes.
  Terminal attempts cannot revive; late children and stale candidate invalidation
  cannot overwrite a newer generation. Unknown port owners are never stopped.

Added 25 offline cases: 20 launcher cases covering A READY → B CANCELLED → C
revalidation, 15 invalid runtime/contract bindings, late-child generations, mocked
parent UAC cancellation and concurrent PowerShell workers; five preparation/UI
cases covering the actual SSE stream, the same browser document and three startup
fingerprint changes. No test timeout limits were increased.

Final validation: launcher/Admin focused **131 passed**; critical Windows
**176 passed**; Tier A **390 passed / 1 skipped**; Tier B **752 passed / 1 skipped**;
FULL **1479 passed / 1 skipped**. H1 receipt-before-epoch, H2 CLOCK_UNSTABLE
bad-to-good, account binding and UNKNOWN/consumed regressions pass. The opt-in real
Studio discovery test remains skipped; two existing SDK/websockets deprecation
warnings remain. `pip check`, finite fake CLI, diff whitespace and secret/runtime
checks pass. Sender, listener, AI and delivery implementation hashes match the
start of this narrow pass.

Validation environment: `RUN_LIVE_STUDIO_E2E=0`,
`ENABLE_PHYSICAL_WINDOWS_SEND=false`, `ENABLE_PHYSICAL_TIKTOK_SEND=false`,
`ADMIN_LIVE_ENABLED=false`. LIVE NOT RUN. PHYSICAL INPUT NOT RUN. Real UAC and
accessibility discovery NOT RUN. The commit remains on
`feature/windows-live-studio`; main is neither changed nor merged.
