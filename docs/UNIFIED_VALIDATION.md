# Unified integration validation

Verdict: **A. UNIFIED BUILD READY FOR MOBILE ACCEPTANCE**

Validation date: 2026-10-13, Europe/Astrakhan. All tests are offline/synthetic.
Physical Android, Windows and real TikTok operations were not performed.

1. **Merge conflicts.** All 13 shared conflicts were resolved after comparing
   Android main and Windows release changes. Both implementations and their
   tests remain. The per-file resolution table is in [UNIFIED_TRANSPORTS.md](UNIFIED_TRANSPORTS.md).
   Duplicate mode fields/keywords introduced by an automatic merge were also fixed.
2. **Unified architecture.** One listener/AI/policy/suggestion/approval/Admin pipeline;
   one explicit active transport. The factory constructs only its adapter. Startup
   config rejects unsupported values. No discovery heuristic selects a transport.
3. **Android flow.** Real production components were exercised through Admin using
   a synthetic SDK comment and fake ADB: comment -> AI -> suggestion -> approval ->
   own Profile/host/composer readiness -> guarded fill -> simulated operator Send ->
   positive feed verification -> confirmed common journal row. MANUAL never taps
   Send. Own installation/account binding and security STOP rules remain intact.
   The real operator flow and reviewed device/profile limits are documented.
4. **Windows flow.** Native sender/input/discovery/target source remains identical
   to release `4da0a31ab95465df415bc25167fd564338739edb`. ARM/focus, own-account/UID,
   trusted provenance, sticky failed clock attempt, durable consumed protection,
   reapproval guard, one-shot and AUTO/gate default OFF remain covered. The service
   integrates the common journal without replacing Windows policy.
5. **Transport switching.** Disconnect listener and finish running device work,
   then Save/Apply. Apply revokes ARM/trust and AUTO, changes runtime revision,
   closes/settles old work and requires new readiness. Old approvals cannot cross.
   Sticky Windows failures/one-shot expenditure survive transport roundtrips.
   Old Mobile ingestion cannot be rejuvenated into a new Windows AUTO epoch.
6. **Cross-transport dedup.** Both adapters reserve atomically in the same durable
   registry. Source + normalized own account + stable SDK ID excludes transport
   and session. Both old journals remain read-only blockers, including legacy
   identities reconstructed from stored session/account. No destructive migration,
   row removal or consumed override exists. Unknown/filled/reserved identities stay
   consumed after switch/restart. Missing SDK IDs retain the documented fingerprint
   limitation. One account must retain a shared STATE_DIR and its legacy journals.
7. **UI.** Transport radio cards; separate saved/active selection; active transport,
   stream mode, delivery, AI and AUTO summaries; only relevant Android or Windows
   controls. SSE switches update other tabs, and stale delivery GETs cannot restore
   controls for the retired mode. Stale mutation revisions return 409.
8. **New tests.** 35 unified regressions + 14 Mobile acceptance/preflight regressions
   = **49 passed**. They cover adapter isolation, revocation, queued work, stale UI,
   account binding, startup validation, both legacy journals, reservation/UNKNOWN/
   filled consumption across sessions/restart, full Mobile Admin delivery/verification,
   optional AUTO one-shot and first-ingestion provenance. All physical APIs are fake.
9. **Android critical suites:** **369 passed** across android_delivery,
   android_foreground, guarded_delivery, manual_send_rc and tiktok_you.
10. **Windows critical suites:** **369 passed, 1 skipped** across the five Windows
    suites; acceptance launcher adds **33 passed**. The sole skip is explicit real
    read-only UI discovery (`RUN_LIVE_STUDIO_E2E=1`), intentionally not enabled.
11. **Tier A:** `./scripts/test-fast.ps1` — **532 passed, 1 skipped**.
12. **Tier B:** `./scripts/test-pc.ps1` — **1042 passed, 1 skipped**.
13. **FULL:** `.venv/Scripts/python.exe -m pytest -q
    --junitxml=work/validation/unified-full.xml` — **1621 passed, 1 skipped**.
    pip check: no broken requirements. Fake CLI: exit 0. JavaScript syntax,
    compileall, staged/working diff whitespace and credential/runtime artifact scans
    pass. Two existing TikTokLive/websockets deprecation warnings remain.
    Suite totals below are subsets of FULL, not additional distinct tests.
14. **Changed files:** listed below, including inherited Windows release additions.
15. **Git commits:** one integration merge commit with parents Android main
    `039cbdd936de572741bea2b120ca83ebaa93ad66` and reviewed Windows release
    `4da0a31ab95465df415bc25167fd564338739edb`. The resulting SHA is reported in
    the final response and can be read with `git log -1 --format=%H`.
16. **Git status/publication:** final clean status and origin branch synchronization
    are verified after commit/push and reported in the final response. Only
    `feature/unified-transports` is published; main and release refs are unchanged.

## FULL suite totals

| Suite | Passed | Skipped | Failed |
| --- | ---: | ---: | ---: |
| test_acceptance_launcher | 33 | 0 | 0 |
| test_admin | 51 | 0 | 0 |
| test_ai | 74 | 0 | 0 |
| test_ai_tuning | 35 | 0 | 0 |
| test_android_delivery | 102 | 0 | 0 |
| test_android_foreground | 71 | 0 | 0 |
| test_config_models | 11 | 0 | 0 |
| test_diagnostic | 32 | 0 | 0 |
| test_friendly | 36 | 0 | 0 |
| test_guarded_delivery | 81 | 0 | 0 |
| test_listener | 21 | 0 | 0 |
| test_lmstudio_provider | 116 | 0 | 0 |
| test_logging | 2 | 0 | 0 |
| test_manual_send_rc | 62 | 0 | 0 |
| test_mobile_acceptance | 14 | 0 | 0 |
| test_openai_provider | 46 | 0 | 0 |
| test_operator_runtime | 55 | 0 | 0 |
| test_predelivery_hardening | 75 | 0 | 0 |
| test_quality | 71 | 0 | 0 |
| test_replay | 33 | 0 | 0 |
| test_room_lookup_diagnostics | 119 | 0 | 0 |
| test_service | 4 | 0 | 0 |
| test_tiktok_you | 53 | 0 | 0 |
| test_unified_transports | 35 | 0 | 0 |
| test_unknown_events | 20 | 0 | 0 |
| test_windows_hardening | 28 | 0 | 0 |
| test_windows_live_studio | 30 | 1 | 0 |
| test_windows_native_discovery | 184 | 0 | 0 |
| test_windows_release_hardening | 59 | 0 | 0 |
| test_windows_sender | 68 | 0 | 0 |

## Changed files (72)

- `.env.example`
- `.gitignore`
- `README.md`
- `backend/app/admin/events.py`
- `backend/app/admin/models.py`
- `backend/app/admin/operator_config.py`
- `backend/app/admin/router.py`
- `backend/app/admin/runtime.py`
- `backend/app/admin/service.py`
- `backend/app/ai/models.py`
- `backend/app/config.py`
- `backend/app/delivery/coordinator.py`
- `backend/app/delivery/factory.py`
- `backend/app/delivery/journal.py`
- `backend/app/delivery/models.py`
- `backend/app/delivery/registry.py`
- `backend/app/delivery/service.py`
- `backend/app/delivery/suggestions.py`
- `backend/app/live/listener.py`
- `backend/app/live/models.py`
- `backend/app/live/service.py`
- `backend/app/logging_config.py`
- `backend/app/main.py`
- `backend/app/static/admin/app.js`
- `backend/app/static/admin/delivery.js`
- `backend/app/static/admin/index.html`
- `backend/app/static/admin/operator.js`
- `backend/app/static/admin/styles.css`
- `backend/app/windows/ChromiumDiscovery.cs`
- `backend/app/windows/ClientDiscovery.cs`
- `backend/app/windows/Ia2Discovery.cs`
- `backend/app/windows/NativeDiscovery.cs`
- `backend/app/windows/__init__.py`
- `backend/app/windows/native_discovery.py`
- `backend/app/windows/native_input.py`
- `backend/app/windows/preparation.py`
- `backend/app/windows/probe.py`
- `backend/app/windows/probe_uia.ps1`
- `backend/app/windows/sender.py`
- `backend/app/windows/service.py`
- `backend/app/windows/target.py`
- `backend/tests/conftest.py`
- `backend/tests/fixtures/windows/FakeChromiumProbe.cs`
- `backend/tests/fixtures/windows/FakeClientProbe.cs`
- `backend/tests/fixtures/windows/FakeDiscovery.cs`
- `backend/tests/fixtures/windows/FakeIa2Probe.cs`
- `backend/tests/fixtures/windows/FakeMsaaVariants.cs`
- `backend/tests/fixtures/windows/FakeNativeVariants.cs`
- `backend/tests/test_acceptance_launcher.py`
- `backend/tests/test_guarded_delivery.py`
- `backend/tests/test_mobile_acceptance.py`
- `backend/tests/test_predelivery_hardening.py`
- `backend/tests/test_tiktok_you.py`
- `backend/tests/test_unified_transports.py`
- `backend/tests/test_unknown_events.py`
- `backend/tests/test_windows_hardening.py`
- `backend/tests/test_windows_live_studio.py`
- `backend/tests/test_windows_native_discovery.py`
- `backend/tests/test_windows_release_hardening.py`
- `backend/tests/test_windows_sender.py`
- `docs/PC_LIVE_STUDIO_ARCHITECTURE.md`
- `docs/PC_SEND_2.md`
- `docs/UNIFIED_TRANSPORTS.md`
- `docs/UNIFIED_VALIDATION.md`
- `pyproject.toml`
- `scripts/acceptance-launch-helpers.ps1`
- `scripts/prepare-mobile.ps1`
- `scripts/probe_live_studio.py`
- `scripts/start-acceptance.ps1`
- `scripts/test-fast.ps1`
- `scripts/test-full.ps1`
- `scripts/test-pc.ps1`

LIVE NOT RUN.
ANDROID PHYSICAL SEND NOT RUN.
WINDOWS PHYSICAL SEND NOT RUN.
