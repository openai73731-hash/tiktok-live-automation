# Unified transport integration

One backend/Admin UI owns one active delivery transport: `android` or
`windows_live_studio`. `TIKTOK_TRANSPORT` is the explicit startup default. The
allowlisted operator overlay can save `tiktok.transport`; Apply activates it.
Invalid transport values fail configuration validation. Discovery never selects
a transport. Only the selected adapter is constructed; status creates no physical
I/O, listener, provider calls or database.

The LIVE listener, LiveService, CommentProcessor, AI, translations, safety policy,
suggestions, immutable approvals, local Admin security and event bus remain shared.
Android and Windows retain their own readiness, trust, physical gates and AUTO
rules. Native adapters are lazy and all development tests replace physical APIs.

## Merge resolution

Integration starts at Android main `039cbdd`; the merged Windows release is
`4da0a31ab95465df415bc25167fd564338739edb`. No blanket ours/theirs resolution was
used. Each shared conflict was inspected against both branch implementations.

| File | Android changes | Windows changes | Combined resolution |
| --- | --- | --- | --- |
| `admin/events.py` | Delivery status SSE | Windows sender SSE | Keep both typed projections |
| `admin/router.py` | MANUAL/AUTOMATIC, fill, verify | ARM, disarm, fill, send-once, epoch | Keep both; reject inactive physical action families |
| `admin/service.py` | Fill/content eligibility, mode/status | Consumed/reapproval, Windows eligibility, receipt observer | Dispatch policy by active transport; common approvals/status |
| `ai/models.py` | Provider flag cannot authorize AUTO | Same invariant with Windows epoch | Keep advisory false; independent delivery policies |
| `delivery/models.py` | Filled status, Android private proof, AUTOMATIC | Provenance, stable key, AUTO and epoch metadata | Union schemas; preserve distinct audit modes |
| `delivery/journal.py` | Filled schema migration and unresolved fill | Stable durable key, legacy SDK matching | Preserve every row/unique constraint; stable-key schema plus filled; unresolved exclusion |
| `delivery/service.py` | Owned fill/AUTO queue and verification | Windows shared view/status fields | Keep Android behavior, add platform-neutral view fields |
| `delivery/suggestions.py` | Deterministic content proof/authorization | Trusted receipt, sequence, registration/approval clocks | Keep both private proofs, never deserialize authority |
| `main.py` | Delivery SSE and owned shutdown | Factory selection and Windows SSE | Runtime installs current adapter; shutdown closes current adapter |
| `static/admin/app.js` | Android eligibility display | AI advisory and Windows status | Preserve AI non-authority, active transport summaries |
| `static/admin/delivery.js` | Fill-phone and verification | ARM/focus/one-shot/consumed controls | Separate controls, dispatched by active transport |
| `static/admin/index.html` | Android delivery mode panel | Windows focus/preparation panel | Keep both with unique IDs and conditional visibility |
| `README.md` | Android release/operator instructions | Windows release/operator instructions | Retain both; unified instructions take precedence |

An automatic merge also duplicated the coordinator's `mode` argument and request
field. This was corrected explicitly: Android audit mode is `AUTOMATIC`, Windows
audit mode is `AUTO`, manual mode is `MANUAL`.

## Apply and authority

Save changes only ignored local configuration. Saved and active transports are
shown separately. Save/Apply require current opaque revision tokens; stale
browsers receive 409. Disconnect the owned listener and finish any running device
operation before editing/applying, preserving the existing operator locks.

Apply synchronously disables AUTO, drains pending Android candidates, revokes
Android device/account/install/profile trust or Windows ARM/accepted composer,
and changes the runtime revision. Transport switches mark the old service closed,
publish the new adapter/settings together without an await, then cancel and settle
old owned work using the cancellation-resistant lifecycle helper. Old suggestions
and approvals cannot authorize input in the new runtime. New readiness is required.
Windows one-shot expenditure, clock failure and cleanup failure cannot be cleared
by a roundtrip through Android. PRE-LIVE preparation evidence is invalidated on
Apply; a launcher must produce current preparation evidence in a fresh backend.

## Durable cross-transport dedup

Both factory-created adapters reserve in `STATE_DIR/unified-delivery.sqlite3`.
Reservation uses SQLite `BEGIN IMMEDIATE` plus unique suggestion, receipt,
idempotency and stable SDK delivery keys. A stable key hashes source, normalized
own account and SDK message ID; it deliberately excludes transport and session.
Every reserved/filled/confirmed/unknown/not-sent/needs-operator identity remains
consumed. UNKNOWN and unresolved fill never authorize retry after switching or
restart. Approval checks durable consumption server-side before mutation.

The old `delivery.sqlite3` and `windows-delivery.sqlite3` remain read-only blockers.
No row is deleted, unlocked, copied or rewritten automatically. Legacy rows without
a durable key are compared using their stored session/account and the incoming
stable SDK ID to reconstruct the original identity hash. Missing/unstable SDK IDs
retain the existing bounded fingerprint limitation; no permanent equivalence is
claimed. Existing malformed/unreadable journals fail closed. Keep all three files
in the same STATE_DIR for the same account, and stop old backend versions before
using the unified backend. Separate state directories do not share dedup.

## Mobile acceptance first

Android MANUAL preserves main's guarded fill contract: backend fills approved
ASCII text; the operator presses Send on the phone. No programmatic activation
occurs in MANUAL even when the startup physical gate is true. Unsupported Unicode
uses COPY. Positive read-only feed evidence is required for confirmation.

MANUAL readiness uses a fresh (90-second) reviewed focused-composer inspection
bound to the exact verified account/device/installation and current listener
session. Approval/runtime/session ownership is rechecked before every input action.
It does not require the physical SEND gate, a Send candidate, or the programmatic
UI host-LIVE lease, and never sets `live_verified` merely to allow typing. Status
shows `FILL READY` for this capability; programmatic SEND/AUTO retain their separate
physical, LIVE and Send-control guards. Missing or conflicting composer/security
evidence still rejects input. MANUAL fills only the immutable approved server text.

Supported readiness is still the reviewed Xiaomi M2101K9G / Android 13 / TikTok
You 46.8.3 installation/profile. Device selection is discovered, not hardcoded by
the acceptance script. Other devices/installations stop at NEED OPERATOR until a
separately reviewed profile exists. Exact own Profile handle, owner control and
installation binding remain mandatory; LIVE display name is never account proof.
Security/login/CAPTCHA/unknown screens require STOP/NEED OPERATOR.

1. Start the backend on loopback with `LIVE_MODE=fake`, `ADMIN_LIVE_ENABLED=false`,
   both physical gates false and `TIKTOK_TRANSPORT=android`.
2. In Settings choose Mobile / Android and your own username. Save, then Apply.
   Select the sole detected device if needed. Keep AUTO OFF.
3. Open your own TikTok Profile. In PowerShell, from the repository, run:
   `./scripts/prepare-mobile.ps1 -Username YOUR_OWN_HANDLE`.
   Optionally supply `-DeviceSerial` for an expected discovered device. This script
   checks exactly one online device, own Profile, initial disconnected listener,
   AUTO OFF, explicit expected gate and unchanged runtime. It reads only.
4. START LIVE ON PHONE manually. CONNECT LISTENER in Admin.
5. Send a NEW TEST COMMENT from a test viewer. Review AI REPLY and both Russian
   translations, then APPROVE. Fake AI accepts only its documented fixtures;
   `Is it automatic?` is an offline/acceptance fixture. LM Studio can be selected
   and explicitly checked separately; no paid provider is required.
6. Inspect the normal own host LIVE to establish its fresh lease. Open and focus
   the empty composer manually. Use guarded FILL PHONE. Press Send yourself.
7. Return to normal LIVE and click the read-only verification button. Require
   SENT CONFIRMED; UNKNOWN means STOP and no retry. Disconnect to STOP.

Only after successful manual acceptance, an optional Android AUTO one-shot test
uses startup `ENABLE_PHYSICAL_TIKTOK_SEND=true` and
`MOBILE_ACCEPTANCE_ONE_SHOT=true`. Restart, repeat own Profile/host/composer and
Send-control confirmation. Run preflight with `-ExpectedPhysicalGate ON`.
Explicitly arm AUTOMATIC for the current session, then admit one NEW SDK comment.
Existing content, freshness and authority rules still apply. One attempt spends
the one-shot latch and returns to MANUAL/AUTO OFF, draining the remaining queue;
UNKNOWN additionally pauses and never retries. No AUTO test is part of development.

Android MANUAL status and final input checks use the same fresh hierarchy
classifier. CAPTURE passes the server's current account/session into inspection,
so a read-only capture cannot revoke exact Profile trust by supplying no account.
An incomplete UI dump permits one read-only recheck (8-second bound) shared across
the whole Fill operation, only before its first input action. Unsafe/unknown UI,
foreground conflicts and transport failures do not trigger this recheck; text
insertion is never retried. Failed final checks log only structural evidence,
hashes and binding matches, never hierarchy text or account handles.

After backend restart, verify the exact own Profile again, then open/focus the
composer and CHECK DEVICE. Missing account trust blocks with `account_unverified`;
`COMMENT_INPUT_READY` recognizes a field and cannot substitute for account proof.

## Windows acceptance

Windows keeps release provenance, own-account/UID binding, sticky clock failure
per attempt, durable consumed protection, server reapproval protection, physical
gate default OFF, fresh ARM/focus, UNKNOWN no-retry, epoch/one-shot and preparation
generation/reuse guards. Native sender/input/discovery implementations are retained.
Use the Windows panel: preparation, explicit empty-composer review, ARM/focus,
approved fill or guarded send-once and echo verification. AUTO remains a separate
post-epoch approval workflow; Android's content authorization never enters it.

The original Windows acceptance launcher remains restricted to its reviewed branch
allowlist. This integration's first real acceptance is Mobile. Do not treat switching
to PC as launcher preparation or as permission for physical input.

## Validation commands

`./scripts/test-fast.ps1` is unified Tier A. `./scripts/test-pc.ps1` is the broader
Tier B, retaining its compatible filename. `./scripts/test-full.ps1` runs FULL.
Also run `.venv/Scripts/python.exe -m pip check`,
`.venv/Scripts/python.exe -m app.main --mode fake`, `git diff --check` and review
tracked/staged files for secrets and runtime artifacts before pushing only
`feature/unified-transports`. Main and Windows release refs must remain unchanged.

LIVE NOT RUN. ANDROID PHYSICAL SEND NOT RUN. WINDOWS PHYSICAL SEND NOT RUN.
