# Project scope and rules

- Scope: stages one, two, Stage 3A, Stage 3B, Stage 3C, Stage 4A, Stage 4A.1, Stage 4A.2, Stage 4A.3 and Stage 4A.4 are authorized and implemented. Stage two
  provides CommentProcessor, ReplySuggestion, a provider-independent AI Reply
  Engine, deterministic FakeAIReplyEngine, Russian operator translations,
  knowledge configuration and business/safety validation. Stage 3A is VERIFIED
  ON REAL OWN LIVE, per the operator's latest report for @antilame: room lookup,
  guarded API fallback, live-check, SDK start, CONNECTED, real CommentEvent,
  CommentProcessor/FakeAI ReplySuggestion, ignored UnknownEvent, LiveEndEvent,
  ENDED -> STOPPED. Stage 3B implements OpenAI; paid API quality/access remains
  unverified until the operator runs the separate manual AI smoke test.
- Stage 4A.4 hardens pre-delivery only. SDK cleanup and processor retirement are
  finite owned transitions that repeated caller cancellation must finish. SDK
  cleanup records complete/failed, attempts both HTTP pools, socket, heartbeat and
  waiters, and never cancels unrelated tasks. A failed cleanup blocks new CONNECT
  until operator intervention/backend restart. DisconnectEvent immediately revokes
  CONNECTED without declaring ENDED. APPLY builds first, atomically publishes all
  new references/revision, then retires the old runtime; active must never be closed.
  SAVE/RESET require expected_saved_revision; APPLY also requires the expected
  active_runtime_revision. Opaque tokens change on save/reset, apply and restart;
  stale browser mutations return 409. Pending SAVE does not invalidate approval.
  Each operator CONNECT owns one opaque live_session_id; internal reconnect keeps
  it, full disconnect/connect replaces it. Browser DISCONNECT requires the observed
  ID; lifespan uses internal stop_current. Terminal ENDED/NEED_OPERATOR and explicit
  disconnect permanently invalidate that session. Ignore callbacks from old sessions.
  QualityService has a fresh service_epoch per instance; compare JS revisions only
  inside one epoch and clear old rows on epoch change. Quality review requires both
  run_id and explicit run_number, identifying a unique trial within that run group.
  Overlay reads at most 16 KiB once, limits depth to 12/nodes to 256, rejects aliases,
  and maps parser/recursion/decoding errors to static LocalConfigError.
  Bounded server SuggestionStore (100 Playground/50 LIVE) owns immutable envelopes
  and approvals. Hash exact reply and full envelope; bind source/comment identity,
  account/session, active runtime, loaded knowledge/policy, provider/model and receipt
  expiry (five minutes). SDK CommentEvent.common.msg_id is verified in installed
  7.0.1; missing ID uses an explicitly limited 30-second receipt-bucket fingerprint.
  APPROVE is CSRF protected, accepts identity/expected revision plus explicit
  translation_review_confirmed only, and never sends. Non-ready, expired, modified
  or stale envelopes cannot be approved. Browser text is never delivery authority.
  DeliveryEligibility is pure, deterministic and uses allowlisted reasons; production
  delivery/device remain unavailable. ReplySenderProtocol and AndroidControllerProtocol
  are contracts only. DeliveryCoordinator has no web route or production sender;
  mocks exist only in tests. Results: confirmed/not_sent/unknown/needs_operator.
  Timeout/cancellation after dispatch is UNKNOWN, never an automatic retry.
  SQLiteDeliveryOperationStore uses ignored data/delivery.sqlite3 with transactional
  reservation and unique suggestion/comment/idempotency identities. Retain at most
  10,000 bounded metadata rows, no reply/original text/secrets; full journal fails
  closed, never evicts dedup records. Reserved rows surviving a crash mean UNKNOWN
  and block retry. Same key returns prior result; another key cannot resend the same
  suggestion/comment. Database construction is deferred; web startup/GET/SSE create
  no operation, database, listener or inference. Approval/history are in memory;
  restart invalidates them via a new active runtime token. APPROVE != SEND.
  ADB, Android implementation, physical TikTok SEND and AUTO remain absent and need
  a separate Stage 4B authorization. Preserve all existing local/security guards.
- Stage 4A implements a local operator Admin UI served by this FastAPI backend:
  HTML/CSS/vanilla JavaScript only, no CDN/npm/frontend server. Bind only to
  127.0.0.1; loopback peer/Host, same-origin JSON and per-lifespan CSRF guards apply.
  Never publish it or forward the port. API/SSE use explicit UI projections;
  never serialize Settings, keys, cookies, provider/SDK payloads or runtime logs.
  Business logic, approved knowledge, composition and safety remain backend-side.
  ADMIN_LIVE_ENABLED defaults false: web startup creates no listener and makes
  no AI calls. Enabling LIVE is an explicit startup action; CLI behavior is unchanged.
  Playground uses the production factory/processor/engine/safety pipeline with
  its own long-lived processor, lazy client and bounded history of 100 results.
  LIVE has a separate processor/history of 50; Playground never produces LIVE
  comments. Lifespan closes both processors and only their owned tasks.
  AdminEventBus fan-out is nonblocking, bounded to 16 subscribers/32 events each;
  overflow requests history resync, reconnect reads bounded histories, disconnect
  releases subscriptions. Histories/counters exist only in this server process.
  LM Studio health is an explicit cached GET models check, never generation.
  OpenAI status never makes paid requests; each explicit Playground analysis
  requires confirmation. Automated tests mock both providers.
  MANUAL is fixed; COPY REPLY writes only to the operator clipboard. SEND and
  AUTOMATIC are disabled. No TikTok delivery, sent state, Android or restart is
  implemented. Future SEND/AUTO require separate explicit authorization and
  backend delivery/eligibility design; ready still requires human approval.
- Stage 4A.1 adds Quality Lab under AI Playground. The canonical 75-case corpus
  is config/ai_quality_cases.yaml (JSON syntax, valid YAML); case IDs/expectations
  are version-controlled and validated against approved knowledge. Runtime
  results/manual review remain in memory, never written into the corpus.
  Runs use the same Playground processor/client and hold its lock for the
  entire sequential run. OpenAI is forbidden, even with paid confirmation;
  enabled LIVE input disables Quality Lab to avoid concurrent inference.
  No listener is created by Quality Lab. Start only through explicit RUN ALL or RUN STABILITY x3;
  reading snapshots/reconnecting never calls AI. STOP finishes the current
  case then prevents the next; shutdown cancels only the owned runner before
  closing the processor/client, including cancellation before its first step.
  Retain only the latest bounded run; SSE progress uses allowlisted models.
  Exports are explicit browser JSON downloads, not persistence or audit.
  Deterministic checks measure curated-case agreement, fact SET exact match,
  retained approved source/template binding, lexical/style/length restrictions,
  language detection and technical status; they do not prove semantic translation
  fidelity. Constrained translation evidence is checked by production policy;
  the final suggestion retains source binding/review flags, not that evidence.
  Provider failure has a static processing_error category so an expected
  abstention cannot accidentally PASS an unavailable provider. Errors never
  expose request/exception bodies. Critical classification/fact/claim/safety
  failures remain distinct from manual translation/naturalness grades.
  Awkward Russian grammar with correct meaning is operator-facing quality;
  wrong meaning, reply language or financial promises still require attention.
  Human grades never alter deterministic PASS. Tests use mocked providers only.
  A separate manual localhost Qwen run is authorized for this stage; never use
  real TikTok or paid OpenAI. Do not tune prompts before reviewing measurements.
- Stage 4A.2 tunes general local analysis instructions and unseen illustrative
  examples after reviewing measured failures; never map corpus strings to results.
  Corpus expectations and approved knowledge remain unchanged. Distinguish none,
  financial no_promises, minimal approved facts and on-topic knowledge_missing.
  Preserve full Russian translations even for skipped readable messages; language-less
  input uses und/null and operator review. Strict JSON parsing and final safety gates
  remain mandatory. processing_error and optional processing_error_detail are fixed
  allowlists, never provider content, validation inputs or exception messages.
  The shared reason enum remains fixed; do not simplify it without measured evidence.
  Stability holds the Playground lock across exactly three sequential full trials,
  retains only the latest group (up to 225 case results), and exports all completed
  trials plus the current partial trial. Agreement means and consistency use only
  complete trials with identical dataset/knowledge/AI/prompt/provider/model/temperature
  fingerprints; partial trials contribute only work/error counts. STOP finishes the
  current case and prevents the next, including the next trial. Review is trial-bound
  and cannot alter deterministic grades. Count processor invocations separately from
  actual model POST attempts (analysis plus constrained translation). Compare
  temperatures with identical other settings; no generation on GET/SSE/reconnect.
  Manual localhost Qwen measurements are authorized; TikTok and paid OpenAI are not.
  German wording remains constrained machine translation requiring review, never
  approved knowledge. Back-translation and curated PASS do not prove semantic fidelity.
  SEND/AUTO/Android remain disabled and auto_send_eligible=false.
- Stage 4A.3 adds a fixed ignored operator.local.yaml beside the selected project
  config. Safe YAML/schema validation and atomic temporary-file/replace writes
  allow only username, fake/lmstudio provider, model, temperature 0..1, timeout
  1..120, focused/friendly policy and bounded reply lengths. No credentials,
  endpoint/URL, facts, safety rules or generic path/file editor are permitted.
  Precedence is project config -> local overlay -> immutable web runtime; CLI is
  unchanged. SAVE persists without mutating active processors; APPLY explicitly
  replaces the idle Playground processor/client and Quality Lab. RESET removes
  only the overlay and needs APPLY to change active behavior. LIVE ownership,
  inference/Quality Lab and health checks block changes. Invalid local config
  exposes only a static error, uses safe project defaults and blocks connection.
  Default browser/server startup never constructs a listener. Explicit CSRF-protected
  CONNECT starts one read-only real LiveService asynchronously for the supplied own
  handle; DISCONNECT cancels/gathers only its owned task, frees SDK and LIVE AI
  resources, leaves Playground/FastAPI running. Terminal security states require
  explicit disconnect; no security retry/bypass is added. Existing explicit
  ADMIN_LIVE_ENABLED startup remains compatible. UI Connect forbids OpenAI;
  overlay cannot select it. Existing paid Playground confirmation remains mandatory.
  Friendly social responses use only whole-message matches from a separate
  reviewed en/ru/es bank, with original/Russian translations and stable social IDs.
  Mixed factual/financial messages and unknown questions never fall back to social.
  Provider classification schema/reason enum and factual composer are unchanged.
  Both policies use the same grounding, claim, language, style and length gates;
  social binding is rechecked against input/policy/bank, approval remains mandatory.
  The 75-case Quality Lab baseline requires FOCUSED; a separate small friendly
  corpus tests the social bank. No free social generation is enabled. Reply policy
  FOCUSED/FRIENDLY is separate from delivery mode MANUAL/AUTOMATIC. SEND/AUTO/Android
  remain absent. Tests mock LIVE and providers; manual UI/localhost smoke may not
  start real TikTok or paid OpenAI. Only the operator may press real CONNECT.
- Stage 3C adds LMStudioReplyEngine through the same AIReplyEngine/factory contract.
  Configuration selects provider/model; fake remains default. LM Studio is trusted
  only as a loopback local provider: http, explicit port, /v1, literal 127.0.0.1,
  localhost (canonicalized to 127.0.0.1) or ::1. Reject remote/LAN hosts, userinfo,
  query/fragment and redirects; disable environment proxies and automatic retries.
  Never use the OpenAI SDK or send OPENAI_API_KEY to LM Studio. No shared arbitrary
  base_url. Existing async httpx supports local chat/completions and models routes.
  GET /v1/models checks the configured model ID; never silently select another.
  The list may include downloaded/JIT models; generation verifies actual access.
  The 2026-10-06 real localhost qwen/qwen3-8b nine-comment smoke completed with
  eight ready and one skipped; all require approval, none are sent. This verifies
  finite integration behavior, not translation quality. Observed Russian mistranslations,
  incomplete injection translation and awkward German remain operator-review limitations.
- LM Studio analysis uses strict JSON schema for language/Russian comment translation,
  should_reply, intent and approved fact IDs. Never extract JSON from free text,
  markdown or thinking output. Unknown IDs, refusal, partial/malformed output and
  unavailable model/server require operator review for that comment. One lazy async
  client is reused and closed on CLI/FastAPI shutdown/cancellation. Tests mock HTTP;
  real localhost smoke is separate from CI and does not authorize TikTok/paid calls.
- Stage 3C factual replies use deterministic approved variants/combined templates
  from bot_knowledge.yaml; source bindings must still match current approved facts
  and translations. Preserve viewer language; operator translation is Russian.
  Replies are short natural chat text, one sentence preferred, maximum two in focused
  and three in friendly, no emojis,
  emoticons, markdown, hashtags, marketing or financial promises. Run final safety
  checks for both reply and Russian reply translation.
- For languages without approved wording, Stage 3C permits a separate constrained
  translation of the exact approved English reply through the same local model.
  The translator receives approved text and target language, never the original
  viewer comment. Strict fields, exact Russian approved text, source/back-translation,
  style/claim/length checks fail closed. Back-translation is a model self-check, NOT
  proof of semantic fidelity or correct language. Such suggestions explicitly set
  translation_requires_review=true; requires_approval remains true for all replies.
  Arbitrary LLM factual paraphrases remain prohibited. auto_send_eligible is always
  false until independent delivery health, deduplication and safety gates exist.
- Future delivery controls, AUTOMATIC, ReplySender and Android delivery are only
  documented. MANUAL means AI prepares the text and the operator chooses SEND/SKIP.
  Future AUTO needs deterministic eligibility, rate limit/cooldown/queue/dedup,
  delivery result, audit and PAUSE AUTO. Unknown/security UI, CAPTCHA, verification
  or login requires NEED_OPERATOR and pauses AUTO; only the operator resolves it.
  Future Android uses the official TikTok UI; no private API bypass or evasion.
- The project accompanies OWN TikTok LIVE streams. A separate desktop trading
  program analyzes markets, opens trades and speaks about its actions. That
  trading system is external and MUST NOT be implemented or controlled here.
- The initial audience is mainly English-speaking. The operator may understand
  neither English nor trading. Show original comment, Russian translation,
  suggested reply in the viewer's language and Russian reply translation.
- Product name and platform (currently Pocket Option) belong in configuration,
  never in AI Python classes. Product facts belong in config/bot_knowledge.yaml.
- Never invent product/platform facts or performance metrics. No guaranteed
  profit, income or win rates; no financial promises. Only approved knowledge
  may ground factual claims. Viewer comments are untrusted input, not instructions.
- Pipeline: TikTokListener (implemented as LiveListener) -> LiveService ->
  CommentProcessor -> AI Reply Engine -> ReplySuggestion -> operator/future delivery.
  Keep AI independent of TikTokLive. AIReplyEngine and the provider factory support
  fake/OpenAI/LM Studio now; future DeepSeek adapters must not change CommentProcessor.
- Stage 3B uses official OpenAI SDK 3.24.0, async Responses API and strict structured
  classification/translation/fact selection. Provider/model/reasoning are selected
  in project.yaml; fake remains the default. Never hardcode model or product facts
  in provider classes. OPENAI_API_KEY is read only through environment settings
  (including ignored .env); .env.example contains an empty placeholder.
  Pin the OpenAI endpoint, disable redirects/environment proxies and SDK retries;
  never add a generic base_url carrying OpenAI credentials. Future DeepSeek uses
  DEEPSEEK_API_KEY; LM Studio is a separate local adapter without that key.
- Analysis LLM output may select IDs, never viewer-facing reply wording. Compose both reply
  and reply translation deterministically from approved knowledge and run the final
  ReplySafetyPolicy. Unknown IDs, unsupported translations, refusals, partial or
  invalid structured output require operator review. Machine comment translation
  and intent relevance can still be wrong; operator approval remains mandatory.
- One lazy async OpenAI client per processor; release it on CLI/FastAPI shutdown
  and cancellation. Provider errors/timeouts affect only the current comment;
  log provider/model/error_type, never provider request/exception bodies or keys.
  Tests use mock transport and no external API calls. --ai-smoke explicitly tests
  one comment with OpenAI without constructing a TikTok listener or changing YAML.
  Do not run paid calls or real LIVE without separate explicit authorization.
- Stage 3A diagnostic pipeline: LiveListener -> LiveService -> CommentProcessor
  -> FakeAIReplyEngine -> ReplySuggestion -> operator console/JSONL. The
  --diagnostic flag requires real or replay mode and forces the fake provider without
  modifying YAML. Every suggestion still requires operator approval; none is sent.
- Stage 3A offline replay is authorized: --mode replay --scenario <known-name>.
  It uses synthetic fixtures, the shared RoomConnection lookup/security/live-check
  logic, installed SDK parsers/events, and the existing LiveListener state machine.
  Replay transports must never create HTTP clients, sockets, DNS requests or
  signing/WebSocket connections, and must fail closed on unplanned fixture access.
  SDK imports remain confined to listener.py. Replay always uses @replay and the
  FakeAIReplyEngine; no delivery is implemented. Expected NEED_OPERATOR is a PASS
  only if the complete scenario expectations and cleanup checks match. Replay
  success never verifies current TikTok compatibility or replaces manual real LIVE
  smoke testing. The default mode remains fake.
- Diagnostic runtime states: STARTING, CONNECTING, CONNECTED, RECONNECTING,
  ENDED, NEED_OPERATOR, STOPPED. Preserve terminal_state after STOPPED and
  exit code 2 for operator intervention. Reconnect uses bounded exponential
  backoff and a finite attempt budget; log attempt, delay and error type only.
- Room ID strategy: HTML success -> live-check; only an observed HTTP 200 HTML
  FailedParseRoomIdError from the SDK HTML parser, without redirect or HARD
  security signals, permits one guarded API fallback via
  the installed fetch_room_id_from_api route. API success -> live-check; any API
  lookup failure -> NEED_OPERATOR -> STOPPED, without additional lookup/retries.
  This partially restores TikTokLive 7.0.1 behavior with security guards, never
  its unconditional fallback. Unknown categories and security events forbid
  fallback. Guards apply in all real sessions; SAFE_API_FALLBACK and allowlisted
  lookup telemetry are diagnostic-only. Never log room IDs or raw responses.
- Distinguish HARD response signals (401/403/429, explicit CAPTCHA/verification
  or challenge structure/active state, access restrictions) from SOFT text and
  generic auth UI. Generic login links, strings and script URLs do not block
  parsing or guarded fallback. A login wall/interstitial without valid LIVE
  state is HARD; a true CAPTCHA/challenge remains HARD with valid LIVE state.
  Never classify by response length. challenge_reason_codes must contain only
  predefined identifiers, never matched text, payload fragments or URLs.
- Transport/HTTP failures during room lookup require an operator; explicit
  HTML UserOfflineError and reconnects in other phases retain bounded backoff.
- CAPTCHA, verification, security challenge, login/access restrictions, HTTP
  403/429, signature errors and unknown/ambiguous control states require an operator
  without retries or bypass. Ctrl+C must release this client's socket, heartbeat
  and both HTTP pools without cancelling unrelated asyncio tasks or a traceback.
- TikTokLive 7.0.1 UnknownEvent means an unmapped webcast message, not a security
  state. Log UNKNOWN_EVENT_IGNORED and continue without creating Comment or AI work.
  Diagnostic/replay metadata is limited to unknown_method_candidates (up to 16
  unique ASCII identifiers of 1-96 characters; invalid values are redacted),
  payload_length (aggregate bytes of unmapped messages in the outer response),
  unknown_event_count (listener lifetime), and is_first_response. The SDK retains
  only the outer response: never claim a candidate is the exact triggering method.
  Never log bodies/envelopes or classify security by words in method names.
  AccessControlEvent, RoomVerifyEvent, PerceptionEvent, UnauthorizedMemberEvent,
  STREAM_SUSPENDED and unknown ControlAction still require NEED_OPERATOR.
- Real LIVE smoke tests are manual and only for the user's own stream. Do not
  start a LIVE or use an account handle unless the user supplies and authorizes it.
- Separate future control pipeline: LiveSupervisor -> AndroidController ->
  TikTok Android. AndroidController must not depend on AI; LiveSupervisor must
  depend on an AndroidController abstraction, not concrete device/UI code.
- Future LIVE status monitoring/restarts are not implemented now. CAPTCHA,
  security challenge, login request, unknown UI or ambiguous control state must
  enter NEED_OPERATOR and never trigger an attempted bypass. The existing
  listener's operator_required state expresses this requirement for LIVE input.
- Android automation, comment sending, LIVE restart and additional provider adapters
  require a separate explicit user instruction. Public/remote admin remains out
  of scope. Committing or pushing a completed stage never authorizes the next.
- Python 3.12+, asyncio, FastAPI, Pydantic Settings, pytest. Avoid extra technologies.
- Only backend/app/live/listener.py may import TikTokLive in production code.
  SDK contract tests may import it to verify the pinned API without network access.
- Consumers receive app.live.models.Comment via LiveService callback or its queue.
- Fake mode is the default. Unit tests must never connect to TikTok.
- FakeAIReplyEngine is a deterministic fixture engine, not a universal LLM or
  translator. Unsupported text/language must be identified honestly; never label
  an untranslated original as a Russian translation. Fixtures support en/ru/es.
- All factual replies must cite stable IDs in config/bot_knowledge.yaml. Current
  safety policy permits approved wording only; arbitrary future LLM paraphrases
  require an explicit grounding/semantic-verification design before enabling them.
- Validate replies AND their operator translations for business rules, language,
  sentence/character limits. No reply may bypass operator approval or be sent.
- config/project.yaml holds product/stream/AI settings; config/fake_ai_examples.yaml
  holds finite demo scenarios. Do not mix demonstration fixtures with market data.
- Before changing the SDK version, inspect the installed version's metadata,
  README and implementation; run adapter contract and shutdown tests.
- Unknown control/security states stop the listener and require operator intervention.
  Never solve or bypass CAPTCHA or verification.
- Never log credentials, cookies, signed connection URLs or raw SDK payloads.
- .env, virtual environments and runtime logs stay outside version control.
- Windows commands and configuration changes must be documented in README.md.
- Run: python -m pytest; python -m app.main --mode fake (after editable install).

## GitHub workflow

- Official local workspace: C:\Users\user\Documents\tiktok-live-automation.
- Official private repository: https://github.com/openai73731-hash/tiktok-live-automation.
- Use the single origin remote with URL
  https://github.com/openai73731-hash/tiktok-live-automation.git; default branch main.
  Correct origin in place if needed; do not create a second remote.
- Commits and pushes to this private repository are authorized. Before each push,
  inspect git status, git diff and git diff --cached, and run the local venv's
  python -m pytest -q, python -m pip check and python -m app.main --mode fake.
  Push only after all checks succeed and the files being published are reviewed.
- Never track or push .env, credentials, API keys, cookies, sessionid, access
  tokens, runtime logs, .venv, __pycache__ or .pytest_cache. Review tracked and
  staged contents for secrets; .env.example must contain placeholders only.
- Never force-push main or delete remote branches. Never reset --hard published
  history without separate explicit permission.
- If GitHub requires authentication, stop and ask the user to confirm login;
  do not bypass authentication or print credentials.
