# PC-first migration: architecture audit and PC-SEND-1F

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

Explicit developer CLI -> isolated UIA / HWND / MSAA read-only worker -> private report
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

### PC-SEND-1B: Chromium discovery extension (2026-10-08)

The operator reports Windows 11, PowerShell 5.1, Python 3.12.10 and Studio 1.36.6
under `C:\Program Files\TikTok LIVE Studio`. Their observed main GUI PID was 11300;
this is transient evidence, never a hardcoded process selector. Their control/raw
UIA captures expose only `Chrome_WidgetWin_1` and `Chrome_RenderWidgetHostHWND`, no
patterns or edit/button/chat candidates. This supersedes the earlier no-window
observation above for their running installation. No new real probe was run by the
agent for PC-SEND-1B; real MSAA availability remains unverified.

`--backend uia` preserves the existing default and schema 1. Explicit `hwnd`, `msaa`
and `all` require a positive selected main PID. `native_discovery.py` launches stock
Windows PowerShell in STA with fixed encoded code; `Add-Type` compiles the packaged
`NativeDiscovery.cs` locally against built-in .NET `Accessibility.IAccessible`.
There is no new Python/Node dependency, downloaded helper or injection. Its narrow
read interface has no actions, values, focus, input, clipboard or browser-debug APIs.
The fake harness implements this same interface and executes the actual collectors
without constructing the native adapter or inspecting real windows.

The native adapter uses bounded `EnumWindows` to find the selected PID's roots,
then cycle-guarded child/sibling HWND traversal. Each HWND records its actual owner
from `GetWindowThreadProcessId`, independently of root ownership. Class hints mark
the observed Chromium shell classes but establish no selectors. Names are opt-in;
password-style edit HWNDs suppress names and descendants. Windows documents that
[`GetWindowText`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw)
cannot retrieve another application's edit-control contents. No `WM_GETTEXT` or
other content-reading fallback is used.

MSAA roots come from
[`AccessibleObjectFromWindow` with OBJID_CLIENT](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessibleobjectfromwindow).
The walker uses state/role/name/count/location reads and
[`AccessibleChildren`](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessiblechildren),
handling both integer simple-child IDs and accessible COM objects. It releases
owned COM references, bounds visits (including repeats/errors), and suppresses
protected names/children. Unreadable state prevents name/child access.
`WindowFromAccessibleObject` supplies an optional owner HWND; simple children report
the container owner explicitly. Unknown owners stay null. Role/state semantic hints
are evidence only. No `accValue`, default action or setter is called.

PC-SEND-1B schema 2 separates `hwnd.windows`, `msaa.nodes` and optional `uia` (schema 1 in
`all`). It includes backend, selected PID, optional executable ProductVersion,
bounded related-process metadata, truncation/unreadable counts, up to 16 static
error codes and `physical_action=false`. Toolhelp snapshot filtering retains only
the main PID and descendants (up to 64, ancestry depth 20); optional image path
uses limited-information access. A denied/empty image path is null and nonfatal.
Version is null if unavailable/non-numeric; it is never inferred from a path or
process name. `has_windows`/HWND references describe only the captured tree, not a
complete inventory of the desktop. The referenced HWND records contain classes.
Reports never include process command lines or private exception text.

Native enumeration caps desktop roots examined at 512, matching roots at eight,
process snapshot at 4096, child traversal at 2000, and output at 2 MB. CLI defaults
are 256 HWND, 500 nodes per accessibility backend, depth 12 and 20 seconds; hard
caps are 2000 HWND, 2000 nodes per backend, depth 20 and 60 seconds. `all` shares a
single deadline between native compilation/discovery and UIA; timeout kills only
its owned worker. Partial/cyclic/malformed results remain `unknown_ui_state`.
Bad CLI limits (including `--max-nodes 3000`) fail before any inspection with a
clear argparse `invalid_argument`. Existing ignored storage, 20 reports per
instance across backends, link rejection and private stdout rules are preserved.

The exact four manual capture commands are in README. PC-SEND-2 still requires
redacted real HWND/MSAA/UIA snapshots with panel state, version/locale, account/LIVE
markers, unique composer/submit ancestry and stable identity across reopening the
panel. Report all unknown/blocked states too. If native discovery still exposes
only a shell, there is no basis for input or SEND selectors. Accessibility
snapshots alone do not verify Unicode entry, keyboard behavior or publication.

### PC-SEND-1C: child variants, optional properties and scan reasons

The latest operator capture reports Studio 1.36.6.0, transient GUI PID 4620, 14 HWNDs
and a visible widget/render-widget pair. Other surfaces are hidden; geometry and
visibility alone do not establish a chat panel. MSAA exposed role 9 / Chrome Legacy
Window with child_count 7 but omitted its children, with the old aggregate codes
`msaa_unreadable`, `msaa_repeated`, `native_enumeration_limit`. The agent has not run
a real probe for this fix or claimed that the installed Studio tree is now complete.

Code audit found that VT_I4 was already wrapped as a simple child with identity
`parent COM identity + child ID`; this was not a missing VT_I4 branch. However,
`get_accChild` was never called, so an ID resolving to a full accessible object was
always treated as a leaf. A parent need not supply that object's properties; a
fake regression reproduces the previous loss on the mandatory state read. As
[Microsoft documents](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-iaccessible-get_accchild),
S_FALSE means no separate object and the parent supplies child metadata. This is
now a normal null/leaf outcome. Integer IDs and dispatch objects remain distinct.

Schema 3 is necessary for the new `child_id` field and explicit `simple_child` vs
`accessible_object` representation. Simple metadata uses the exact parent + child
ID. If that ID resolves to a full object, its own mandatory state is checked before
any name, and its metadata supplies the child-ID row. The row is preserved first,
then the full object is recorded beneath it with a proper local parent ID. Unknown
state fails closed; protected parent/child/object prevents names and descendants.
There are no value or action calls. Both rows count against the unchanged total
MSAA node budget and depth; the budget is shared across all HWND roots.

Previously a role, name, owner or count exception entered one broad catch and could
drop the whole row. Those optional reads now retain the node with null fields and
static `msaa_role_unavailable`, `msaa_name_unavailable`, `msaa_owner_unavailable`,
`msaa_child_count_unavailable` or `msaa_location_unavailable`. Required state failure
is `msaa_state_unavailable`; child-object, children-enumeration failure and unsupported
variants have separate codes. `msaa_unreadable` remains a sanitized fallback for
unexpected identity/walker failures. Old reports did not retain a property/HRESULT source,
so their exact real COM failure cannot be reconstructed from the excerpt alone.

The previous `msaa_repeated` set spanned every HWND root, so a legitimate object
exposed through another HWND was flagged as a cycle and skipped. The set is now
scoped to one root traversal. Within that root, full-object identity remains
cycle-safe, and simple-child identity still includes its exact child ID. Seven
different IDs sharing one parent no longer risk being confused with a cross-root
repeat; genuine repeated IDs and full-object cycles still stop traversal.

`native_enumeration_limit` formerly merged four distinct sources: desktop scan
(512), matching roots (8), sibling HWND scan/repetition (2000), and process snapshot
(4096). The root check also stopped as soon as eight had been collected, even if
the next window was unrelated. The bounded scan now continues past unrelated windows
and flags a root cap only upon a ninth match. Reports identify `desktop_scan_limit`,
`target_root_limit`, `target_hwnd_scan_limit` and `process_scan_limit` separately.
No cap was raised. A desktop cap cannot prove that later target roots do not exist,
even if the captured root's children are complete, so that case remains conservative
unknown/truncated with a desktop reason. Exactly eight roots followed by unrelated
windows and a complete desktop scan is no longer truncated. The old aggregate code
does not reveal which cap triggered in the operator's 14-HWND report.

New fake tests inject raw integer/dispatch-like variants into the actual node
wrapper and verify parent property arguments, S_FALSE/null, object expansion,
protected/unknown states, optional HRESULT errors, per-root identity, cycles and
budgets. A fake bounded top-window scan tests exact-eight/ninth-root/desktop limits.
Compilation and tests call no native desktop APIs or real UI. The original UIA
worker, HWND metadata, report storage, sender and disabled preparation are preserved.

### PC-SEND-1D: native VARIANT evidence before IA2

The next operator capture of Studio 1.36.6.0 reports 18 nodes, no simple children,
role 9 / child_count 7, `msaa_child_unsupported` and `target_root_limit`. This proves
neither a broken x64 ABI nor a need for IA2. The 1C P/Invoke used an LPArray of
managed objects: original VARTYPE was unavailable after CLR conversion, null,
unsupported managed types and unsuccessful IAccessible casts shared one exception,
and the first bad element aborted later siblings. The loop already honored
pcObtained; iterating all requested slots was not the audited defect. The exact
real VARIANT/QI failure remains unproven until a new operator capture.

The diagnostic interop now follows the native
[AccessibleChildren contract](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessiblechildren):
an explicitly owned VARIANT buffer and LONG count/obtained, without object[]
conversion. The documented
[VARIANT union](https://learn.microsoft.com/en-us/windows/win32/api/oaidl/ns-oaidl-variant)
includes a two-pointer BRECORD and an overlapping DECIMAL. Pack 8 yields size 24
on x64 and 16 on x86, tag offset 0 and payload offset 8. Runtime size assertions
and offline fake harnesses in both stock PowerShell architectures verify these
layouts. They do not verify a real Chromium COM provider.

All bounded slots are zeroed and
[VariantInit](https://learn.microsoft.com/en-us/windows/win32/api/oleauto/nf-oleauto-variantinit)
initialized. Only 0..pcObtained-1 are classified. S_FALSE or a short obtained count
produces partial metadata; slots outside obtained are cleanup only. Inside obtained,
VT_EMPTY is a failure, VT_I4 must contain an exact positive int32 child ID, and
VT_DISPATCH must contain a non-null dispatch pointer. Other types are diagnosed
without reading BSTR/array/byref payloads or converting arbitrary values to IDs.
A bad child consumes the shared visit budget and does not abort valid siblings.

Dispatch pointers receive explicit QueryInterface(IID_IAccessible). The temporary
QI reference is released in finally. The returned
[GetObjectForIUnknown RCW](https://learn.microsoft.com/en-us/dotnet/api/system.runtime.interopservices.marshal.getobjectforiunknown?view=netframework-4.8.1)
is owned by the node adapter until Dispose; get_accChild's separate managed return
is also balanced. Finally,
[VariantClear](https://learn.microsoft.com/en-us/windows/win32/api/oleauto/nf-oleauto-variantclear)
releases each original initialized slot, including unvisited tail slots on early
termination, then the buffer is freed. Cleanup continues across individual clear
failures and reports a static failure. S_FALSE/null get_accChild remains a normal
simple-child leaf. Required protection state, per-root dedup, depth/node limits,
optional metadata semantics and UIA behavior remain unchanged.

Native schema 4 adds `msaa.child_diagnostics` (64 records) and
`msaa.diagnostics_dropped`. Records contain only `source_hwnd`, `parent_id`,
`child_index` (zero-based), numeric `vt`, normalized `category`, `reason`,
`hresult_category`, `requested` and `obtained`. A parent is the enumerating node
(or the current root for an optional root property); unavailable context is null.
Categories are i4/dispatch/empty/byref/array/other/unavailable. Reasons distinguish
unsupported_variant_type, dispatch_qi_failed, child_property_failed,
child_resolution_failed, empty_slot, partial_accessible_children and
malformed_variant. HRESULTs map to s_ok/s_false/no_interface/invalid_argument/
access_denied/disconnected/failed/unexpected_success, never raw exception text.
No diagnostic record includes names, values, BSTR contents or COM addresses.
Names opt-in applies only to the existing node label fields. The Python schema
forbids extra fields and bounds all numeric fields and collections.

Root selection now completes the bounded desktop scan before selecting at most
eight matching roots. Visible selected-PID roots precede hidden roots; numeric
HWND breaks ties independently of EnumWindows order. Depth-first traversal keeps
each root's render-widget descendants ahead of the next root. No HWND is hardcoded.
`hwnd.root_statistics` reports target_roots_total/selected/skipped and
`enumeration_complete`. With a desktop cap, total means observed matching roots,
not a complete desktop total. `hwnd.selected_roots` reports hwnd, observed visible,
hwnd_traversal_complete and msaa_traversal_complete (null for HWND-only). Failures,
protection omissions and budgets prevent local completeness, while a root cap can
leave a selected visible root complete and the overall report unknown/truncated.
No cap was raised and no arbitrary --root-hwnd filter was added.

The fake buffer harness covers seven I4/dispatch/mixed slots, partial S_FALSE,
empty tail versus populated empty slot, failed QI, malformed counts/IDs/tags,
unsupported BSTR/array/byref tags without payload access, sanitized metadata,
cleanup, diagnostic/visit budgets and visible-root priority/statistics. It never
instantiates NativeDesktop or invokes native accessibility/UI APIs. IA2 remains
deferred: review actual vt/category/HRESULT evidence first. There is no new helper,
binary dependency, selector, input, SEND or LIVE action.

### PC-SEND-1E: bounded IServiceProvider / IA2 capability experiment

The operator's Windows 11 / Studio 1.36.6.0 capture after 1D has 52 MSAA nodes.
Classic MSAA exposes Windows chrome/simple children, with no observed TikTok
composer, comments or Send. Manually enabling Narrator did not change the tree.
Four I4 diagnostics do not establish hidden TikTok controls. The 15 observed roots
(8 selected, 7 skipped) already include the visible widget/renderer; the root cap
does not explain the missing UI in that included renderer.

Sources checked 2026-10-09:

- Chromium's [ia2_api_all.idl](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/third_party/iaccessible2/ia2_api_all.idl),
  blob `c8b3f354d96d8e5317428ed6dab2b8fd3082aee9`: _changingInterfaces requires
  QI(IServiceProvider) then QueryService(IID_IAccessible, IID_IAccessible2).
  IA2 IID is `E89F726E-C4F4-4C19-BB19-B647D7FA8478`.
- Microsoft's [IServiceProvider declaration](https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.ole.interop.iserviceprovider?view=visualstudiosdk-2022)
  verifies IID `6D5140C1-7436-11CE-8034-00AA006009FA` and InterfaceIsIUnknown.
  The [managed signature](https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.ole.interop.iserviceprovider.queryservice?view=visualstudiosdk-2022)
  and [native contract](https://learn.microsoft.com/en-us/windows/win32/api/servprov/nf-servprov-iserviceprovider-queryservice(refguid_refiid_void))
  specify REFGUID service, REFIID interface, void** result, HRESULT return.
  Local declaration: PreserveSig int, ref Guid/ref Guid/out IntPtr.
- Service IID_IAccessible is `618736E0-3C3D-11CF-810C-00AA00389B71`, matching the
  stock Accessibility.IAccessible contract. No alternate service is tried.
- Microsoft's [simple elements](https://learn.microsoft.com/en-us/windows/win32/winauto/simple-elements)
  distinguish positive child identifiers from CHILDID_SELF, the object itself.
  The I4 reader records sign classification, skips zero as informational
  childid_self_reference, and fails closed on negative IDs. Negative IDs are
  not reinterpreted as IA2 uniqueID or action targets.

`Ia2Discovery.cs` declares only IServiceProvider's single COM method. It omits
the IAccessible2 vtable, IA2 metadata getters and IA2 traversal in this capability
slice. Allowed role/states/uniqueID/windowHandle/indexInParent can be reviewed later
if measured evidence warrants it. There is no new binary helper or SDK dependency,
inspector, debug port, process manipulation or Studio restart/accessibility flag.
IA2 is obtained only by QueryService, never ordinary QI(IA2).

The source IAccessible is borrowed and retained by the experiment. The native
bridge owns GetIUnknownForObject's ref, temporary QI IServiceProvider ref and a
separate service RCW, balancing each in finally/Dispose. QueryService's returned
pointer is released even on unsuccessful HRESULT, never converted to an IA2 RCW
or used to call a method. Each retained source root is disposed once at settlement.
Fake lifetime tests model these boundaries; they do not prove a real COM provider
or installed Studio compatibility.

Only Chromium hints in the existing bounded selected-PID HWND tree are candidates.
At most 32 checks are selected: visible top-level main widget, its renderer HWNDs,
then other in-scope Chromium HWNDs, with numeric HWND tie ordering. Hidden roots
cannot consume the visible renderer's priority. Root/desktop caps remain 8/512.
`primary_root_hwnd` identifies the first selected visible top-level main widget.
Its renderer checks precede other visible widgets and their renderer checks.
Source PID and optional accessible owner HWND/PID are reported separately, together
with visible-root relation; they establish no account or control selectors.
Source/root ownership is checked before acquisition, query and repeated snapshots.
Unknown protection prevents QueryService/name/children reads; protected objects
are skipped. State is reread before each query and inside each serializer.

All baseline MSAA snapshots precede the first QueryService, retaining exact source
COM objects. Each source is then queried once in capability priority order and
immediately reread once on that same object. There is no wait/retry or UI action.
Other root queries and Chromium asynchrony can affect later reads: a changed tree
is temporal evidence, not proof of one query's causal effect. Unchanged immediate
trees do not exclude later changes or a different context exposing more UI.
Names can themselves affect on-demand accessibility; the default command omits them.

The existing MSAA serializer handles new descendants with its normal protection,
optional properties, cycles and child-ID rules. The joint visit budget for both
phases never exceeds --max-nodes: half is baseline, the rest after, shared among
pairs. Visible renderer gets the first reserved pair when budgets are scarce.
Capability checks remain independently bounded even when snapshot budget runs out.
Depth and owned-worker timeout are preserved; the same deadline also stops C#
enumeration between reads. Python terminates only its owned worker if native calls
block. Combined diagnostics stay capped at 64 and output at 2 MB.

Schema 5 keeps `msaa` for after, adding `ia2.before_msaa` with the same safe section
shape. `capability_checks` contains source/root/owner handles, source/owner PID,
visibility/Chromium hint, nested provider/query capability and static HRESULT
categories, phase node/child counts, comparison completeness and changed bool/null.
Capability rows contain no names, values, BSTR, COM addresses or exception bodies.
`query_success_count`/`query_failure_count` count attempted QueryService calls;
missing IServiceProvider is separately visible, not a fictitious service call.
`visible_renderer_supported` and `visible_renderer_checked` distinguish support
from whether that primary root's visible renderer was checked; another visible
root's support cannot mask a primary renderer failure. Before/after parent IDs belong to separate
snapshot sections. Shared node and diagnostic bounds cover both phases.

Comparisons use relationships, owner kind, role, state and child count, not names
or COM identity, and run before aggregate ID remapping. Protection omissions,
limits, deadline or property errors make comparison incomplete and changed=null.
The aggregate comparison requires complete pairs for every selected check;
otherwise aggregate changed is null, while individual complete pairs remain usable.
General target_root_limit can coexist with a complete visible-renderer pair.
In ia2 mode, selected_roots' ordinary msaa_traversal_complete is null because each
phase pair owns completeness. Capability failure does not change other backend
execution or start inference/listeners. UIA stays the default; `all` retains its
UIA+HWND+MSAA meaning without IA2 queries.

For a complete visible-renderer pair: supported+expanded means more MSAA evidence;
supported+unchanged means no change in this immediate bounded observation; and
unavailable means this source/context did not provide the interface (inspect both
HRESULT categories). None establishes chat controls/input/sending or absence of
support in every object/version. Incomplete comparisons need operator review.
The real idle command is in README; the agent did not run it.

PC-SEND-1E validation (2026-10-09): focused native discovery tests **110 passed**;
Tier A **140 passed, 1 skipped**; Tier B **502 passed, 1 skipped** with the two
existing SDK/websockets deprecation warnings. The opt-in real UI test remained
disabled. Stock x64/x86 PowerShell compiled the C# sources and exercised fake
COM/UI boundaries only. `pip check` and the finite fake CLI passed. No real
Studio discovery, LIVE, provider generation or physical action was performed.

### PC-SEND-1F: explicit CLIENT / WINDOW acquisition evidence

Review of pre-change HEAD `06b697c7b7ae93e78d3745ca3bc15f18e6ee62b8` establishes
that NativeDesktop.Accessible already called AccessibleObjectFromWindow with
`unchecked((uint)-4)` and IID_IAccessible. This is OBJID_CLIENT (0xFFFFFFFC), not
OBJID_WINDOW (0). The repository neither called CreateStdAccessibleObject nor
sent WM_GETOBJECT itself. Thus the operator's 1E system/window chrome and
E_INVALIDARG IA2 result do not prove a wrong object ID in this code. The installed
provider's exact reason remains unverified; 1F adds explicit measurements.

Primary sources checked 2026-10-09:

- Microsoft [object identifiers](https://learn.microsoft.com/en-us/windows/win32/winauto/object-identifiers)
  distinguish the window itself from the application's client area.
- Microsoft [AccessibleObjectFromWindow](https://learn.microsoft.com/en-us/windows/win32/api/oleacc/nf-oleacc-accessibleobjectfromwindow)
  specifies HWND, DWORD object ID, REFIID and void** output, with S_OK on success.
- Chromium's current [legacy_render_widget_host_win.cc](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/content/browser/renderer_host/legacy_render_widget_host_win.cc),
  blob `ec0f5c93d005f9ae2cbd77ef6d83220eb49e4ed4`, handles OBJID_CLIENT by returning
  the web-content IAccessible through the standard MSAA mechanism. It separately
  creates a system window accessible internally. This upstream code is not proof
  of the installed Studio version/embedded Chromium implementation.
- Chromium's historical [renderer client test](https://chromium.googlesource.com/chromium/src.git/+/8ced33be1ee63e589afff6f9229b36fe73cfd0f9/src/chrome/browser/accessibility_win_browsertest.cc)
  obtains the renderer using AccessibleObjectFromWindow(OBJID_CLIENT, IID_IAccessible).
  No private activation behavior from other historical tests is copied.

The new explicit `--backend client` keeps old modes intact and reads both fixed
object IDs for at most 32 previously scoped Chromium HWNDs. The shared acquisition
boundary passes unchecked DWORD -4 or 0 and the stock IAccessible IID directly to
oleacc. It records HRESULT categories even on a non-null unsuccessful retval,
releasing that retval; a successful object has one owned NativeAccessible lifetime.
Failure never substitutes WINDOW for CLIENT. Native calls remain in the owned
hidden STA worker; no direct/custom messages, helper, flags or process changes exist.

CLIENT acquisition and IA2 queries precede WINDOW acquisition. Ordering is primary
visible main widget, its visible renderers, other visible Chromium HWNDs, then hidden
HWNDs. Hidden renderers of the same root cannot consume the primary visible renderer's
check priority. Root/desktop/check caps remain 8/512/32. Ownership, deadline and
protection checks remain mandatory; protected/unknown-protection CLIENT objects
suppress WINDOW probing as well. A failed acquisition is distinct from unknown
protection on an acquired object.

Schema 6 adds source_kind/object_id on each MSAA node and IA2 check. In dual mode,
`msaa_client` holds the CLIENT after tree, `ia2.before_msaa` its baseline, and `msaa`
is null. `client_objects.window_msaa` holds only independently serialized WINDOW
root metadata, never WINDOW descendants. `client_objects.checks` pairs each source
HWND/root/PID/visibility with objid_client/objid_window observations: available,
acquisition category/status, role/state/count/bounds, optional name and capabilities.
CLIENT pair metadata comes from the baseline and is labelled metadata_phase=before;
WINDOW uses metadata_phase=window. Missing metadata from a limit is not unavailability.

Only CLIENT executes the existing QI(IServiceProvider) then QueryService(IAccessible,
IAccessible2) contract. WINDOW checks QI(IServiceProvider) only; its IA2 availability
is null with not_queried_window, never a fabricated negative result. No IA2 metadata
getters, full vtable, traversal or actions are introduced. No Value/password/attributes
reads exist. Names require the same opt-in and protection policy in both trees and
paired metadata; pointers/COM identities/raw exceptions never enter the report.

The joint visit budget covers CLIENT before/after plus WINDOW roots. WINDOW reserves
min(candidate count, max-nodes/4) visits, CLIENT gets the remainder (at least 75%).
Client pairs retain the 1E exact-object comparison and renderer snapshot priority.
Bounded acquisition/provider capability can still run when metadata budget is exhausted.
Depth, shared deadline, owned-worker kill, output/storage bounds and joint 64 diagnostics
remain enforced. Existing negative child-ID behavior is unchanged.

`client_objects.visible_renderer_client_available`, visible_renderer_client_nodes
(after tree only) and visible_renderer_client_ia2_supported refer only to visible
renderers of primary_root_hwnd. They do not summarize other windows' success.
Available CLIENT plus content roles/descendants is evidence for review; available
CLIENT with only shell metadata is a shell-only observation, not a readiness grade.
Unavailable requires an attempted failed acquisition/category; skipped protected,
out-of-scope or timeout cases stay distinct. CLIENT IA2 true/s_ok proves capability
for that returned object/context. Incomplete/null comparisons cannot establish an
unchanged or shell-only tree. Standard reads can affect on-demand accessibility;
dual captures do not prove WINDOW/CLIENT COM identity or causality. No real probe
was run by the agent, and PC-SEND-2 remains outside this stage.

PC-SEND-1F validation (2026-10-09): focused native discovery **136 passed**;
Tier A **166 passed, 1 skipped**; Tier B **528 passed, 1 skipped**, with two
existing SDK/websockets deprecation warnings. Real UI opt-in remained disabled.
The stock x64/x86 PowerShell harness compiled the production C# sources and ran
fake acquisition/COM/UI boundaries only. `pip check` and the finite fake CLI
passed; no real Studio probe, LIVE, provider generation or input action was run.

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

1. **PC-SEND-1:** audit, transport selection, cached unavailable Windows
   sender, native read-only probe, offline tests and tiered commands. This is a
   discovery milestone, **not the first manual-prepare release candidate**.
   **PC-SEND-1B:** add bounded HWND and MSAA discovery to investigate
   the observed Chromium shell. No production controls or actions.
   **PC-SEND-1C:** fix child-ID/object traversal, per-root dedup,
   optional properties and precise scan limits. Still read-only evidence collection.
   **PC-SEND-1D:** diagnose native VARIANT tags/QI/partial results,
   prioritize visible roots and report per-root completeness. Exact real child failure
   still requires operator capture; IA2 was deferred in that slice.
   **PC-SEND-1E:** standard IServiceProvider/QueryService capability
   and paired MSAA experiment; no IA2 vtable/actions or input implementation.
   **PC-SEND-1F (current slice):** explicit dual OBJID_CLIENT/OBJID_WINDOW evidence,
   separate CLIENT tree and CLIENT-only IA2 queries. Previous code already used CLIENT.
2. **PC-SEND-2:** collect/redact real control/raw/HWND/MSAA trees and version/locale/account/LIVE
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

PC-SEND-1B validation: focused Windows/Tier A **67 passed, 1 skipped**; Tier B
**429 passed, 1 skipped**, with the same two existing SDK deprecation warnings.
The skip is the opt-in real UIA test; `RUN_LIVE_STUDIO_E2E=0` was explicit.
`pip check` found no broken requirements. The isolated wheel includes the C#,
native Python and original UIA workers and excludes runtime reports/logs. The real
native adapter was not executed: the C# collectors ran only against fake boundaries.
Full pytest and unrelated `test_unknown_events.py` fixes remain outside this slice.

PC-SEND-1C validation: focused native contracts **62 passed**; Tier A **92 passed,
1 skipped**; Tier B **454 passed, 1 skipped**, with the two existing SDK deprecation
warnings. The skip is the opt-in real UIA test (`RUN_LIVE_STUDIO_E2E=0`). `pip check`
passed; Windows-selected fake CLI processed three synthetic comments and exited 0.
No real UI probe, real LIVE, paid provider or physical action was executed. The
full suite and unrelated listener changes remain outside this discovery fix.

PC-SEND-1D validation: focused native contracts **84 passed**; Tier A **114 passed,
1 skipped**; Tier B **476 passed, 1 skipped**, with the same two existing SDK
deprecation warnings. Fake buffer harnesses ran in x64 and x86 stock PowerShell;
the real UI test remained disabled (`RUN_LIVE_STUDIO_E2E=0`). `pip check` passed,
and Windows-selected fake CLI processed three synthetic comments and exited 0.
No real probe, LIVE, paid API or physical action was executed. Full-suite and
unrelated listener changes remain outside this read-only diagnostic slice.

For real-machine collection, follow the exact commands in README's PC section.
Start Studio/login/open the comment panel yourself. Collect both idle and appropriate
practice/own-LIVE snapshots, both control and raw view if needed, UI locale/Studio
version/Windows build, password-free account/LIVE markers, unique edit/button ancestry,
ValuePattern availability, and whether comments/reply actions expose stable IDs.
Repeat after reopening the panel to measure stability. Review private reports locally.
Do not type/send merely to collect a tree. Until that review, prepare/submit methods
return `not_sent/not_configured`, and Admin cannot claim Windows readiness.
