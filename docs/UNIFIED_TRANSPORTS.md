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
separately reviewed profile exists. Exact own account and installation binding
remain mandatory. The release proof is a fresh two-channel LIVE challenge;
strict Profile handle/owner verification remains an optional alternative.
LIVE display name is never account proof.
Security/login/CAPTCHA/unknown screens require STOP/NEED OPERATOR.

1. Start the backend on loopback with `LIVE_MODE=fake`, `ADMIN_LIVE_ENABLED=false`,
   both physical gates false and `TIKTOK_TRANSPORT=android`.
2. In Settings choose Mobile / Android and your own username. Save, then Apply.
   Select the sole detected device if needed. Keep AUTO OFF.
3. Open TikTok on the reviewed phone. In PowerShell, from the repository, run:
   `./scripts/prepare-mobile.ps1 -Username YOUR_OWN_HANDLE`.
   Optionally supply `-DeviceSerial` for an expected discovered device. This script
   checks exactly one expected online device, exact model/Android/install/version,
   disconnected listener, idle journal, AUTO OFF, expected gate and runtime. It
   reads only and leaves `account_verified=false`; hierarchy_unavailable is not
   device disconnection or account proof. Detectable security/login still blocks.
4. START LIVE ON PHONE manually. CONNECT LISTENER in Admin.
5. On reviewed normal host LIVE the backend displays a server-owned `MOB-…`
   challenge (or explicitly use LIVE ACCOUNT CHALLENGE after opening host LIVE).
   Type/send it manually from the host phone within 60 seconds. Require LIVE
   ACCOUNT VERIFIED: exact real SDK author_handle/token/message/receipt plus a
   second fresh host/device/install inspection. No challenge goes to AI or normal
   comment logs. Runtime/session/connection generation/device/install/account
   changes invalidate it; no raw auth secret is persisted. Then send a NEW
   EXTERNAL TEST COMMENT from a test viewer. Review AI REPLY and both Russian
   translations, then APPROVE. Fake AI accepts only its documented fixtures;
   `Is it automatic?` is an offline/acceptance fixture. LM Studio can be selected
   and explicitly checked separately; no paid provider is required.
6. Inspect the normal own host LIVE to establish its fresh lease. Open and focus
   the empty composer manually. Use guarded FILL PHONE. Press Send yourself.
7. Return to normal LIVE and click the read-only verification button. Require
   SENT CONFIRMED; UNKNOWN means STOP and no retry. Disconnect to STOP.

Only after successful manual acceptance, an optional Android AUTO one-shot test
uses startup `ENABLE_PHYSICAL_TIKTOK_SEND=true` and
`MOBILE_ACCEPTANCE_ONE_SHOT=true`. Restart, repeat device preparation, LIVE
challenge, successful manual Fill/human Send/VERIFY, and Send-control confirmation.
Run preflight with `-ExpectedPhysicalGate ON`. The challenge path cannot arm AUTO
until its current binding has a successful manual verification.
Explicitly arm AUTOMATIC for the current session, then admit one NEW SDK comment.
Existing content, freshness and authority rules still apply. One attempt spends
the one-shot latch and returns to MANUAL/AUTO OFF, draining the remaining queue;
UNKNOWN additionally pauses and never retries. No AUTO test is part of development.

Android MANUAL status and final input checks use the same fresh hierarchy
classifier. CAPTURE passes the server's current account/session into inspection,
so a read-only capture cannot revoke exact Profile trust by supplying no account.
An incomplete UI dump permits one bounded read-only recheck shared across
the whole Fill operation, only before its first input action. Unsafe/unknown UI,
foreground conflicts and transport failures do not trigger this recheck; text
insertion is never retried. Failed final checks log only structural evidence,
hashes and binding matches, never hierarchy text or account handles.

After backend restart, repeat preparation and the LIVE account challenge (or the
optional strict Profile proof), then inspect host LIVE and open/focus the composer.
Missing account trust blocks with `account_unverified`;
`COMMENT_INPUT_READY` recognizes a field and cannot substitute for account proof.

MANUAL `REJECTED` pauses delivery: Admin shows **DELIVERY PAUSED — RE-PREPARE
REQUIRED**. Neither VERIFY/Profile, reconnect nor new comments clear it. Disconnect
the listener, open TikTok on the reviewed phone, then use **PREPARE MOBILE /
RECOVER** or `prepare-mobile.ps1`. The existing `/api/admin/delivery/prepare` checks
AUTO OFF, idle native/coordinator work, no pending fill, fresh current-runtime
device/install evidence, configured own account and all current/legacy journals. Only a
settled MANUAL rejection on the reviewed Android device is recoverable. UNKNOWN/reserved/filled, other pause
causes or unreadable journals fail with `operator_paused / recovery required`.
Successful preparation creates a new runtime revision, retains every consumed
row and the acceptance one-shot latch, and keeps AUTO OFF. The script rechecks
final pause and revision; it cannot report success while paused. Start a new
listener session, complete the fresh LIVE account challenge and use a new external
comment/approval; never retry the old identity. Preparation never proves the account.

MANUAL entry emits `android_manual_text_entry` in `logs/app.jsonl` with structural
target/focus, command category/exit code, output-presence flags and exact rejection
codes/readback outcome. It never includes reply, stdout or stderr bodies. Stderr
alone is not failure; nonzero exit, timeout, invalid output and unsafe readback
still reject. No text insertion is retried.

Fresh UI acquisition requires a successful transport, a newly created remote
dump, nontrivial UTF-8 XML and a bounded valid `hierarchy` root. The fixed temporary
dump is removed before each attempt; old diagnostic captures are never account
proof. UIAutomator's zero-exit `could not get idle state`, absent/empty file or
invalid XML yields `hierarchy_unavailable`, with one bounded normal-to-compressed
read-only retry shared across Profile verification or MANUAL Fill. No retry after
input starts. Each dump has a 12-second deadline; a dump/read timeout probes the
selected device through bounded ADB before reporting hierarchy_unavailable.
The compressed inspection deadline covers metadata/cleanup as well as the dump,
so it cannot cancel at the former 8-second boundary before the device's idle error.
A parsed Profile that fails exact configured-handle comparison is
`profile_mismatch`; Admin/preflight show these causes separately.
The observed SplashActivity alias requires the same reviewed device/version and
strict Profile layout, every existing normal-host LIVE marker or the exact reviewed
composer shape. Recognizing that surface grants no account or LIVE authority.
Challenge issuance still requires fresh normal-host evidence. After the real
exact-author/token SDK event, a fresh reviewed composer can complete verification
only on the unchanged device/install/runtime/session/connection and original host
lease. It never refreshes that lease; delivery still requires account binding and
an unexpired host lease. Unknown/security layouts fail closed. Boolean receipt
checks and structural final-readback reasons contain no token/author/SDK payload.
No new selector, device/version or AUTO/Send authority is introduced.

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
