# Android preliminary release — 2026-10-19

Branch: `feature/unified-transports`. Base: `d215ab41d2b51c908bacaee73d3dec049b01e7ec`.
The ZIP's `BUILD_INFO.json` identifies the packaged commit and check results.

**PRELIMINARY — continuous physical AUTO is not accepted for customer handoff.**

Today's requested acceptance: two NEW periodic template messages, each with
`sdk_echo_confirmed`, continuous AUTOMATIC retained after both, then STOP.

- 1/2: NOT RUN — blocked before arming AUTO.
- 2/2: NOT RUN — blocked before arming AUTO.
- No additional physical Fill, Send, viewer comment or AUTO enable was performed.
- AUTO is OFF; physical startup gates are false; listener is disconnected.
- Production periodic configuration is templates / 300 seconds / 30 jitter / 12 per hour.

The current backend runtime has no valid manual send admission. Existing
`ManualDeliveryService.set_mode()` rejects AUTOMATIC after preparation unless
`account_proof.manual_accepted` or valid explicit UNKNOWN recovery authorizes it.
Manual admission is scoped to the runtime, LIVE session, connection and installation;
it is set only after successful read-only verification of a manual send. Restart
does not retain it. Seeing text inserted, or a rejected consumed Fill, is not that
confirmation. The previous two durable operations are rejected and unchanged.
Account Profile verification and Send-control alone cannot substitute for this gate.
The operator explicitly prohibited repeating manual Fill today, so acceptance was
stopped instead of bypassing the gate or inventing an SDK receipt.

Earlier physical evidence: the operator observed SwiftKey change `We're` to `Where`.
ADB quoting roundtrips preserved apostrophes/spaces. SwiftKey prediction, correction
and auto-space were then disabled by the operator. The previous Fill additionally
rejected fresh evidence before comparison; its exact authority failure was not
established. New safe diagnostics distinguish observed readback from LIVE authority
expiry. Keyboard settings and offline tests do not prove two successful real sends.

Included: existing Android recovery fixes, strict automatic own-Profile/SDK binding,
readback diagnostics, simpler Russian readiness UI, constrained periodic generation,
three exact operator-approved templates and empty-context model schema correction.
Windows transport, main, old operations, UNKNOWN and consumed dedup are unchanged.

Before customer handoff: resolve the runtime-bound admission requirement through
the legitimate operator flow or a separately approved, reviewed design change;
then complete exactly two new real periodic sends with `sdk_echo_confirmed` and
verify continuous AUTO stays on after each. No old identity may be retried.

See `INSTALL_RU.md` for setup, safe startup, stopping and private backups.

Offline checks for this package:

- Affected focused suite: 390 passed.
- One FULL run: 1997 passed, 1 failed, 1 skipped. The sole failure was the old
  assertion that preparation must leave an exact visible own Profile unverified.
  The expectation was updated to the approved automatic Profile behavior, with
  explicit assertions that manual admission and Send-control are still absent.
  The entire affected hierarchy-acquisition file then passed: 24 passed.
- No production code changed after FULL; a second FULL was not run.
- Earlier Tier A: 578 passed, 1 skipped; not repeated.
- pip check and git diff check: passed.
- Clean-directory source import, safe example validation, fake CLI, real loopback
  backend HTTP startup, UI/assets and AUTO OFF / physical gates false: passed.
- Runtime databases, personal settings, secrets and diagnostics are not packaged.
