# PC-first migration: architecture audit and PC-SEND-1

Audited 2026-10-08 from the actual checkout, starting on clean `main` at
`15e31f9d88688669b7bd430ebad2003f46ec53a7` (`Add TikTok You manual send release candidate`).
Previous commits include TikTok You profiles, foreground compatibility, Android
manual delivery and runtime hardening. Work continues on `feature/windows-live-studio`;
main is not modified or merged. The recent Android RC is the baseline, not the older
stage descriptions farther down README.

## Existing pipeline and ownership

```text
TikTokLive 7.0.1 -> LiveListener -> bounded asyncio.Queue[Comment] -> LiveService
    -> AdminLiveController / AdminService -> CommentProcessor
    -> fake / LMStudioReplyEngine / OpenAIReplyEngine
    -> approved deterministic composer + ReplySafetyPolicy -> ReplySuggestion
    -> immutable SuggestionStore envelope -> Russian-speaking operator APPROVE
    -> ManualDeliveryService -> DeliveryCoordinator -> ReplySenderProtocol
    -> AndroidTikTokReplySender -> ADBAndroidController -> reviewed Android UI
```

The reader is network/SDK-based and **does not depend on Android**. Only production
`live/listener.py` imports TikTokLive. Its guarded room lookup, bounded reconnects,
security stops and owned SDK cleanup are preserved. No new private API, network
protocol, credential extraction, or Windows comment reader is introduced.

| Concern | Actual baseline implementation |
| --- | --- |
| Normalization | `live/models.py:Comment`, `listener.py:comment_from_event`; display ID/nickname, optional user ID, validated SDK message ID, text, UTC local receipt and private monotonic receipt |
| Queue | `LiveService.queue`, default 1000; real listener drops new entries on overflow; a sequential consumer awaits AI; shutdown owns only its producer/consumer |
| Receipt dedup | Admin's current LIVE uses a bounded 1000-identity window; `delivery/suggestions.py:comment_identity` hashes source/account/session plus SDK message ID, or a limited 30-second receipt-bucket fingerprint |
| Reply target | Envelope binds the creator's target account and owned LIVE session; viewer username/original comment are retained. This is a standalone chat reply, not a native `reply_to` action |
| AI | Provider-independent factory/processor; LM Studio lazy async httpx, strict structured selection/translation, approved composer, total timeout, no automatic HTTP retries; failures affect the current comment |
| Content | `ai/safety.py` validates grounding, both reply languages/translations, claims, style and lengths; product facts live in knowledge YAML; every suggestion requires approval |
| Manual delivery | CSRF identity-only SEND input; server envelope supplies text; monotonic expiry includes queue/AI delay; coordinator reserves then rereads approval/context before dispatch and action |
| Rate limit | Android manual global interval default 3 seconds; one operation/device lock. No autonomous reply scheduler or separate outbound queue |
| Durable dedup | Lazy SQLite unique suggestion/comment/idempotency reservations, bounded 10,000 metadata rows; interrupted reservations/UNKNOWN block retry, never evict dedup |
| Unknown delivery | Possible action without proof is UNKNOWN and pauses delivery; no automatic send retry or resolution/deletion shortcut |
| In-memory state | Approvals/runtime tokens, device/account/install trust, LIVE session and bounded histories. Current pending/history share 50 slots; archive is separately bounded; Playground has 100 slots |
| UI/API | Same FastAPI process, local HTML/CSS/JS, GET snapshots/SSE, Playground, Quality Lab, SAVE/APPLY, explicit CONNECT/DISCONNECT, APPROVE, COPY, Android inspection/trust/capture, dry run and gated SEND |
| Persistence | Project/knowledge/fixtures YAML; ignored operator overlay; ignored JSONL logs and diagnostic captures; ignored lazy delivery DB. No raw UI tree in API/SSE/logs/DB |
| Operating modes | MANUAL exists, including AI suggestions plus approval. ASSISTED preparation and AUTO are not implemented; `auto_send_eligible=false` |

Baseline gaps for future AUTO: per-user cooldown, duplicate reply-text suppression,
explicit own-message filtering, a bounded outbound scheduler, pause/resume/kill and
manual override semantics for that scheduler. Receipt dedup is not unlimited
already-processed detection. Approved-only wording is strong content control, but
there is no generic configurable secret/banned-phrase detector. Do not claim these
protections exist or enable AUTO before implementing and measuring them.

## Smallest platform boundary

`ReplySenderProtocol.send(DeliveryRequest) -> DeliveryResult` already provides the
business-to-I/O seam; `DeliveryCoordinator` already consumes it. Keep it. Do not add
a second AI pipeline, a second policy implementation, or another source abstraction:
the existing Listener/LiveService contracts already separate comment acquisition.

This slice adds a startup `TIKTOK_TRANSPORT` factory and moves the Admin readiness/text
capability queries behind service methods. Core Admin no longer reaches into the
Android controller to derive those capabilities. Windows startup constructs no
ADB transport/controller and does not open a delivery DB. Android remains the
compatibility default and retains its reviewed profile, ASCII input, account/LIVE
lease, guarded navigation, SQLite reservation, cancellation and UNKNOWN behavior.

The legacy Admin bridge still owns Android-specific inspection/trust endpoints;
Windows disables them and refuses direct mutations. Its status projection has
`inspection=null`, an explicit Windows transport/stage and no false Android state.
The Windows bridge is intentionally small while no physical Windows actions exist.
Future PC preparation must use the existing authoritative store/coordinator rules;
it must not accept browser-supplied text or UI selectors as delivery authority.

```text
PocketWin (external) -> LIVE Studio video source -> TikTok LIVE

Existing SDK source -> shared Comment/AI/policy/approval/queue
    -> shared delivery coordination -> ReplySenderProtocol
         | AndroidTikTokReplySender (legacy, preserved)
         | WindowsLiveStudioReplySender (PC-SEND-1, refuses prepare/submit)

Explicit developer CLI -> isolated Windows UIA read-only worker -> private report
```

PocketWin is a video source only. There is no dependency on its trading logic,
process internals or APIs. LIVE video start/restart remains external/operator-owned.

## Discovery implementation and observed status

`scripts/probe_live_studio.py` / `python -m app.windows.probe` launches a bounded
Windows PowerShell child containing the bundled, fixed read-only worker. It uses
the .NET Framework `System.Windows.Automation` bindings shipped with Windows, so no
pywinauto, COM package, helper service or new runtime dependency is needed.
Only validated numeric limits, a fixed tree-view enum and a names boolean enter
the worker. No generic shell, browser selector, downloaded script or policy-changing
option is exposed. The worker performs no focus, pattern activation, input, clipboard,
window launch, account login, LIVE start or submit operation.

Discovery matches process-name candidates containing LIVE Studio (a diagnostic
heuristic, not authenticity/account proof); `--process-id` handles an unmatched
executable name. It enumerates at most 512 desktop children, eight matching windows,
500 nodes by default (max 2000) and depth 12 (max 20). Control view is default; use
raw view to investigate a sparse WebView tree. A 20-second child deadline (max 60)
terminates only the probe process, not Studio. Unreadable/truncated trees are marked
`unknown_ui_state`. A successful snapshot is discovery, never delivery readiness.

Each node records snapshot-local ID, parent/window/depth, process/native handle,
control type, automation ID, class, enabled/focusable/focused/offscreen state,
rectangle, supported pattern **names**, and candidate edit/button/chat-region flags.
Candidate flags are clues only; an Edit or Button is not a verified chat control.
There are no production selectors, account markers or targeted-reply selectors.
Names are omitted by default. `--include-names` includes private accessible labels;
password nodes' names and entire descendants are always omitted. Value/Text pattern
contents are never read. No passwords, cookies, sessions or browser storage are read.

Reports remain under ignored `diagnostics/windows/<instance-id>/<opaque-id>.json`,
at most 20 per instance; the probe refuses additional captures without deleting any.
It rejects existing symlink/junction paths. Parent directories remain trusted against
concurrent replacement. Stdout contains bounded event/status/count/path metadata only.
Review/redact accessible names, dynamic IDs and private comments before sharing.

On this machine the completed native read-only probe reported **window_not_found**:
zero candidate windows/nodes and no running matching process. Installation status
is not inferred. No real chat controls, account markers, Unicode entry, keyboard
behavior, targeted replies or submit confirmation have been verified. No LIVE/AI
network request or physical action was made. The initial native `-File` attempt hit
Windows' script execution policy; the final utility uses a fixed encoded command
with native bindings and does not change system/process execution policy.

## Platform evidence and limitations

Microsoft documents [UIA tree scoping and changing trees](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-tree-overview)
and [TreeWalker/property-based discovery](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/obtaining-ui-automation-elements).
These support the diagnostic approach, not a claim about TikTok's actual accessibility.
TikTok's public [Research video-comment API](https://developers.tiktok.com/doc/research-api-specs-query-video-comments)
retrieves video comments; it supplies no authority for LIVE chat submission. We have
no verified documented general public LIVE send endpoint and assume none. The user's
reported desktop reply panel must still be inspected on the installed Studio build.
No guessed endpoint, proprietary protocol reverse engineering, cookie extraction,
CAPTCHA bypass, rate-limit evasion, anti-detection or coordinate fallback is added.

Accessible child trees may depend on open panels, UI language, WebView version,
elevation, interactive VM desktop and LIVE state. No stable native comment ID has
been observed. Preserve SDK identity; future UI targeting must correlate exact
username/text/receipt/sequence uniquely or refuse it. Targeted reply support must
not block safe standalone manual preparation.

## Migration stages and release gates

1. **PC-SEND-1 (this commit):** audit, transport selection, cached unavailable Windows
   sender, native read-only probe, offline tests and tiered commands. This is a
   discovery milestone, **not the first manual-prepare release candidate**.
2. **PC-SEND-2:** collect/redact real control/raw trees and version/locale/account/LIVE
   evidence; review a backend profile. Implement an explicit approved-envelope
   preparation route that rechecks session/runtime/approval/expiry, uniquely locates
   input, verifies blank/exact text and prepares Unicode without submitting. No
   clipboard or Enter assumption. Operator confirms the resulting text and sends.
3. **PC-SEND-3:** only after PC-SEND-2 real verification, add startup-only
   `WINDOWS_ALLOW_SUBMIT=false`, one semantic action, observed state waits, authoritative
   final guard, durable dedup, timeout/cancellation/UNKNOWN settlement. Input clearing
   alone is not proof of publication. No automatic retry. Agent performs no real SEND.
4. Add targeted replies only with unique observed UI correlation; replace comment
   ingestion only if the existing independent SDK reader becomes insufficient.
5. Add ASSISTED and, separately, explicitly enabled AUTO only after outbound scheduling,
   all missing anti-spam controls, content hooks, pause/kill, lifecycle and real
   operator acceptance tests. Never make AUTO the default.
6. Add reviewed host/VM LLM networking as a separate configuration slice; then harden
   per-instance metrics and packaging. Do not weaken endpoint guards incidentally.

## LM Studio and multiple instances

The existing model, base URL, timeout and max-output tokens remain configurable in
project YAML. Its current contract explicitly restricts the endpoint to loopback
HTTP `/v1`; constructor revalidates it. This slice preserves that trust boundary and
does **not** yet enable a host/LAN IP. Remote LM Studio is an acknowledged deployment
gap, not an assumption that inference must run physically in the VM. A later slice
needs an explicit trusted endpoint/host allowlist, exposure/authentication/firewall
review, redirect/proxy/retry protections and mocked network regressions. No OpenAI key
may go to LM Studio. Alternatively an operator-managed local tunnel can preserve the
loopback client contract; the application does not establish one automatically.

Start with one VM / one interactive Studio / one account / one bot process. New
`INSTANCE_ID` tags runtime JSONL events and status; `ACCOUNT_LABEL` is an operator
display label, never account proof. `STATE_DIR` selects the lazy legacy journal
directory; existing `LOG_DIR` selects logs. Their default names preserve Android
behavior. Separate processes on one filesystem must use distinct state/log/project
directories (the operator overlay lives beside project YAML) and local HTTP ports.
Changing instance ID alone does not isolate files. Never share a device/account
journal or delete its dedup records. Across independent VMs filesystem isolation
is natural. Do not use multiple ASGI workers per instance. No global desktop selector
is persisted before real discovery; HWND/PID and diagnostic node IDs are transient.

## Test policy and manual evidence collection

Audit found 1089 collected tests, pytest/pytest-asyncio, socket/HTTP isolation, mock
providers/devices and existing optional Node browser tests; no pre-existing custom
pytest markers or lint/type command. No hundreds-of-tests marker rewrite is needed.
Added only `windows`/`live_studio` markers on the new offline contracts.

| Tier | Command | When |
| --- | --- | --- |
| A | `./scripts/test-fast.ps1 -TestPaths backend/tests/test_windows_live_studio.py` | Changed files/fast behavior only |
| B | `./scripts/test-pc.ps1` | Completed PC slice: Windows, settings/logs/source, approval/coordinator, Android/manual RC, nearby Admin/runtime tests |
| C | `./scripts/test-full.ps1` | Before merge or release candidate, major cross-cutting change, or unexpected global regression |

Scripts never call one another. Pass additional pytest file/selectors to Tier A;
the equivalent direct command is `./.venv/Scripts/python.exe -m pytest -q <files>`
where local script policy prevents launching `.ps1`. Ordinary tests use fake UI tree
reports and mocked process/provider/device boundaries and cannot post. One real
read-only discovery test skips unless `RUN_LIVE_STUDIO_E2E=1`; it never types/submits.
Do not enable it during normal development. Full suite runs at most once per logical
milestone unless a concrete failure requires another run. Baseline affected tests:
117 passed (configuration, Android delivery, source); collection is not execution.
No full baseline run was needed for this isolated discovery slice.

Session validation: initial Tier A settings/probe/logging check **37 passed, 1 skipped**;
Tier B script **386 passed, 1 skipped**; after final narrow hardening, PC contracts plus
mocked LM Studio, TikTok You and browser delivery regressions **200 passed, 1 skipped**.
Final `test-fast.ps1`: **30 passed, 1 skipped**. The repeated skip is the same opt-in
real Studio discovery test, not an unavailable unit dependency. No test failed.
`pip check` succeeded; Windows-selected fake CLI processed three comments and exited 0.
Wheel built with the declared isolated build backend and contains the native worker
and Windows modules, without runtime reports/logs. The venv itself has no setuptools,
so the initial `--no-build-isolation` packaging attempt could not import the backend;
standard isolated wheel build succeeded without changing runtime dependencies.
Full pytest was not run: this is an isolated discovery milestone, not a merge,
production release candidate or a major replacement of the core pipeline.

For real-machine collection, follow the exact commands in README's PC section.
Start Studio/login/open the comment panel yourself. Collect both idle and appropriate
practice/own-LIVE snapshots, both control and raw view if needed, UI locale/Studio
version/Windows build, password-free account/LIVE markers, unique edit/button ancestry,
ValuePattern availability, and whether comments/reply actions expose stable IDs.
Repeat after reopening the panel to measure stability. Review private reports locally.
Do not type/send merely to collect a tree. Until that review, prepare/submit methods
return `not_sent/not_configured`, and Admin cannot claim Windows readiness.
