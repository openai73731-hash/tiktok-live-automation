"""Offline contracts only. No local inference, paid API or TikTok connection."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from pydantic import ValidationError
import yaml

from app.admin.events import AdminEventBus
from app.admin.service import PlaygroundBusy, PlaygroundService, record_for
from app.ai.composer import compose
from app.ai.knowledge import KnowledgeBase, ProjectConfig, load_yaml_model
from app.ai.models import ReplyDraft, ReplyRequest, TranslationEvidence
from app.ai.processor import CommentProcessor
from app.live.models import Comment
from app.main import create_app
from app.quality.dataset import DATASET_PATH, fingerprint, load_dataset
from app.quality.evaluation import evaluate, summarize, summarize_stability
from app.quality.models import Dataset, ManualReview, QualityCase, QualityUpdate, StartInput
from app.quality.service import QualityDisabled, QualityService, StaleRun


@pytest.fixture
def corpus(settings):
    knowledge = load_yaml_model(settings.knowledge_path, KnowledgeBase)
    return load_dataset(knowledge, 1000)


@pytest.fixture
def rig(settings, corpus):
    knowledge = load_yaml_model(settings.knowledge_path, KnowledgeBase)
    project = load_yaml_model(settings.project_config_path, ProjectConfig)
    project = project.model_copy(update={"ai": project.ai.model_copy(update={"provider": "lmstudio"})})
    by_text = {c.viewer_text: c for c in corpus.cases}

    async def generate(request):
        c = by_text[request.original_text]
        values = dict(detected_language=c.language, should_reply=c.expected.should_reply,
                      operator_translation="Тестовый перевод; смысл не оценивается этим mock.",
                      response_kind=c.expected.intent, fact_ids=c.expected.fact_ids)
        if c.expected.should_reply:
            result = compose(request, c.expected.intent, c.expected.fact_ids, c.language)
            values.update(suggested_reply=result.viewer_text, reply_translation=result.operator_text,
                          reply_language=c.language, template_id=result.template_id)
            if result.viewer_text is None:
                values.update(suggested_reply="Test local translation.", composition_style="constrained_translation",
                    translation_evidence=TranslationEvidence(source_en=result.source_en, target_language=c.language,
                        viewer_language_reply="Test local translation.", back_translation_en=result.source_en,
                        operator_reply_translation_ru=result.operator_text))
        return ReplyDraft(**values)

    engine = SimpleNamespace(name="lmstudio", model="fixture/model", generate=AsyncMock(side_effect=generate), aclose=AsyncMock())
    processor = CommentProcessor(engine, project, knowledge)
    events = AdminEventBus()
    playground = PlaygroundService(processor, events)
    service = QualityService(playground, dataset=corpus)
    return SimpleNamespace(service=service, processor=processor, engine=engine, playground=playground, events=events)


async def finish(service):
    task = service.task
    if task:
        await task
        await asyncio.sleep(0)


def test_corpus_scope_and_required_variants(corpus, settings):
    assert len(corpus.cases) == 75
    assert len({c.category for c in corpus.cases}) == 15
    assert {c.language for c in corpus.cases} == {"en", "ru", "es", "de", "fr", "pt", "tr", "und"}
    assert sum(c.language == "en" for c in corpus.cases) == 61
    assert {c.viewer_text for c in corpus.cases} >= {
        "Does it trade by itself?", "Does this thing open trades automatically?", "is this automatic",
        "does it place the trades for you", "do i need to click the trades myself", "does it analyze automatically",
        "how does this work", "what does the bot actually do", "is the analysis automatic", "can this guarantee profit",
        "what winrate do you get", "what's the success rate", "how much money does this make", "can i make money with this",
        "is profit guaranteed", "does it trade on its own", "does dis bot trade itself", "u gotta place trades manually?",
        "what win rate u getting", "does it really work tho"}
    regression = next(c for c in corpus.cases if c.viewer_text == "What is your win rate?")
    assert regression.expected.intent == "no_promises"
    assert "Какая у вас" in regression.notes and "Какой у вас" in regression.notes


@pytest.mark.parametrize("mutation", ["duplicate", "unknown_intent", "duplicate_facts", "facts_empty",
    "nonfacts_ids", "should_reply_type", "reply_intent", "status", "unknown_field", "blank", "language", "category", "version"])
def test_dataset_rejects_invalid_contract(corpus, mutation):
    data = corpus.model_dump(mode="json")
    case = data["cases"][0]
    if mutation == "duplicate": data["cases"].append(case)
    elif mutation == "unknown_intent": case["expected"]["intent"] = "invented"
    elif mutation == "duplicate_facts": case["expected"]["fact_ids"] *= 2
    elif mutation == "facts_empty": case["expected"]["fact_ids"] = []
    elif mutation == "nonfacts_ids": case["expected"]["intent"] = "unknown"
    elif mutation == "should_reply_type": case["expected"]["should_reply"] = "true"
    elif mutation == "reply_intent": case["expected"]["should_reply"] = False
    elif mutation == "status": case["expected_status"] = "skipped"
    elif mutation == "unknown_field": case["generated_translation"] = "forbidden exact match"
    elif mutation == "blank": case["viewer_text"] = " "
    elif mutation == "language": case["language"] = "English"
    elif mutation == "category": case["category"] = "other"
    elif mutation == "version": data["version"] = "latest"
    with pytest.raises(ValidationError): Dataset.model_validate(data)


@pytest.mark.parametrize("mutation", ["unknown_fact", "comment_limit", "unsafe_yaml", "nonmapping"])
def test_loader_validates_knowledge_and_safe_yaml(corpus, rig, tmp_path, mutation):
    data = corpus.model_dump(mode="json")
    path = tmp_path / "cases.yaml"
    if mutation == "unknown_fact": data["cases"][0]["expected"]["fact_ids"] = ["unknown_fact"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if mutation == "unsafe_yaml": path.write_text("!!python/object/apply:os.system ['never execute']", encoding="utf-8")
    if mutation == "nonmapping": path.write_text("[]", encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_dataset(rig.processor.knowledge, 3 if mutation == "comment_limit" else 1000, path)


def test_fingerprints_ignore_mapping_and_set_order(rig):
    kb = rig.processor.knowledge
    reversed_responses = dict(reversed(list(kb.safe_responses.items())))
    assert fingerprint(kb) == fingerprint(kb.model_copy(update={"safe_responses": reversed_responses}))


async def test_full_run_production_policy_and_separate_history(rig, corpus):
    subscriber = rig.events.subscribe()
    run = await rig.service.start()
    assert run.state == "running" and rig.playground.lock.locked()
    await finish(rig.service)
    snapshot = rig.service.snapshot()
    assert snapshot.run.state == "completed"
    assert snapshot.run.summary.total == snapshot.run.summary.passed == 75
    assert snapshot.run.summary.failed == snapshot.run.summary.not_run == 0
    assert snapshot.run.summary.should_reply_accuracy == snapshot.run.summary.intent_accuracy == 100
    assert snapshot.run.summary.fact_ids_accuracy == 100
    assert rig.engine.generate.await_count == 75
    assert rig.playground.processed == 0 and not rig.playground.history
    assert not rig.playground.lock.locked()
    assert subscriber.overflowed  # bounded realtime, resync snapshot remains authoritative
    assert subscriber.queue.qsize() == rig.events.queue_size
    assert all(not r.record.suggestion.auto_send_eligible and r.record.suggestion.requires_approval for r in snapshot.results)
    assert all(r.review.comment_translation_quality == "unreviewed" for r in snapshot.results)


async def test_stability_three_trials_are_sequential_bounded_and_exportable(rig, corpus):
    peak = current = 0
    original = rig.engine.generate.side_effect
    rig.engine.generation_count = 0
    async def generate(request):
        nonlocal current, peak
        current += 1; peak = max(peak, current)
        rig.engine.generation_count += 1
        await asyncio.sleep(0)
        result = await original(request)
        current -= 1
        return result
    rig.engine.generate.side_effect = generate
    run = await rig.service.start(3)
    assert run.runs_requested == 3 and run.stability.runs_completed == 0
    with pytest.raises(PlaygroundBusy): await rig.service.start(3)
    with pytest.raises(PlaygroundBusy): await rig.playground.analyze(corpus.cases[0].viewer_text)
    await finish(rig.service)
    snapshot = rig.service.snapshot()
    assert peak == 1 and rig.engine.generate.await_count == 225
    assert snapshot.run.state == "completed" and snapshot.run.run_number == 3
    assert len(snapshot.completed_runs) == len(snapshot.run.completed_summaries) == 3
    assert len({r.iteration_id for r in snapshot.completed_runs}) == 3
    assert all(len(r.results) == r.summary.passed == 75 for r in snapshot.completed_runs)
    stability = snapshot.run.stability
    assert stability.total_generations == stability.model_generation_calls == 225
    assert stability.runs_completed == 3 and stability.mean_pass_rate == 100
    assert stability.pass_min == stability.pass_max == 75
    assert stability.critical_fail_counts == (0, 0, 0)
    assert stability.pass_consistency == {"3/3 PASS": 75, "2/3 PASS": 0, "1/3 PASS": 0, "0/3 PASS": 0}
    assert all(c.selection_consistent and not c.flapping for c in stability.cases)
    assert not rig.playground.history and not rig.playground.lock.locked()
    # GET/export/reconnect are snapshots and cannot generate more work.
    rig.service.snapshot(); QualityUpdate(run=snapshot.run).model_dump_json()
    assert rig.engine.generate.await_count == 225
    await rig.service.start(); await finish(rig.service)
    assert len(rig.service.completed_runs) == 1 and rig.service.run.stability.total_generations == 75


async def test_stability_partial_stop_has_no_complete_trial_statistics(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.engine.generate.side_effect
    async def slow(request):
        entered.set(); await release.wait(); return await original(request)
    rig.engine.generate.side_effect = slow
    await rig.service.start(3); await entered.wait()
    rig.service.stop(rig.service.run.run_id); release.set(); await finish(rig.service)
    summary = rig.service.run.stability
    assert rig.engine.generate.await_count == summary.total_generations == 1
    assert summary.runs_completed == 0 and summary.mean_pass_rate is None
    assert summary.pass_min is None and not summary.cases and not summary.pass_consistency
    assert not rig.playground.lock.locked()


async def test_stability_stop_on_last_case_preserves_trial_and_prevents_next(rig):
    original = rig.engine.generate.side_effect
    async def generate(request):
        result = await original(request)
        if rig.engine.generate.await_count == 75:
            rig.service.stop(rig.service.run.run_id)
        return result
    rig.engine.generate.side_effect = generate
    await rig.service.start(3); await finish(rig.service)
    assert rig.service.run.state == "stopped" and rig.engine.generate.await_count == 75
    assert len(rig.service.completed_runs) == 1 and rig.service.run.stability.runs_completed == 1
    assert rig.service.run.stability.pass_consistency["1/1 PASS"] == 75


@pytest.mark.parametrize("before_first_iteration", [False, True])
async def test_stability_shutdown_cancels_owned_task_only(rig, before_first_iteration):
    entered, unrelated_release = asyncio.Event(), asyncio.Event()
    async def never(request): entered.set(); await asyncio.Event().wait()
    rig.engine.generate.side_effect = never
    unrelated = asyncio.create_task(unrelated_release.wait())
    await rig.service.start(3)
    if not before_first_iteration: await entered.wait()
    await rig.service.aclose()
    assert rig.service.task is None and rig.service.run.state == "stopped"
    assert not rig.playground.lock.locked() and not unrelated.done()
    await rig.playground.aclose(); rig.engine.aclose.assert_awaited_once()
    unrelated_release.set(); await unrelated


async def test_stability_review_is_bound_to_trial_and_keeps_deterministic_grade(rig):
    await rig.service.start(3); await finish(rig.service)
    run = rig.service.run
    id = next(iter(rig.service.results))
    review = ManualReview(comment_translation_quality="bad", note="manual concern")
    with pytest.raises(StaleRun): rig.service.review(run.run_id, id, review, 1)
    rig.service.review(run.run_id, id, review, 3)
    assert rig.service.completed_runs[2].results[0].review == review
    assert rig.service.completed_runs[0].results[0].review == ManualReview()
    assert rig.service.run.stability == run.stability


async def test_stability_math_flapping_partial_errors_and_fingerprints(rig, corpus):
    await rig.service.start(3); await finish(rig.service)
    trials = list(rig.service.completed_runs)
    first = trials[0].results[0]
    item = first.record.suggestion.model_copy(update={"intent": "unknown", "processing_error": "invalid_output", "processing_error_detail": "invalid_reason"})
    bad = first.model_copy(update={"passed": False, "critical_failures": ("intent_match",),
        "checks": {**first.checks, "intent_match": False}, "record": first.record.model_copy(update={"suggestion": item})})
    results = (bad, *trials[1].results[1:])
    trials[1] = trials[1].model_copy(update={"results": results, "summary": summarize(corpus, results)})
    partial = bad.model_copy(update={"record": bad.record.model_copy(update={"suggestion": item.model_copy(update={"processing_error": "translation_rejected"})})})
    s = summarize_stability(corpus, trials, requested=3, current=(partial,), model_calls=240)
    assert s.total_generations == 226 and s.model_generation_calls == 240
    assert s.mean_pass_rate == 99.56 and (s.pass_min, s.pass_max) == (74, 75)
    assert s.intent_mean == 99.56 and s.intent_min == 98.67
    assert s.critical_fail_counts == (0, 1, 0)
    assert s.invalid_output_count == s.translation_rejected_count == 1
    assert s.pass_consistency["2/3 PASS"] == 1 and s.pass_consistency["3/3 PASS"] == 74
    assert s.cases[0].flapping and s.cases[0].passing_runs == (1, 3) and not s.cases[0].selection_consistent
    for field, value in (("temperature", 0.0), ("ai_config_hash", "changed"), ("dataset_hash", "changed"),
                         ("knowledge_hash", "changed"), ("model", "other"), ("analysis_prompt_hash", "changed"),
                         ("translation_prompt_hash", "changed")):
        with pytest.raises(ValueError, match="identical fingerprints"):
            summarize_stability(corpus, (trials[0], trials[1].model_copy(update={field: value})), requested=3)
    with pytest.raises(ValueError, match="complete corpus"):
        summarize_stability(corpus, (trials[0].model_copy(update={"results": trials[0].results[:1]}),), requested=3)


@pytest.mark.parametrize("value", [0, 2, 4, -1, True, "3", 3.0, None])
def test_stability_input_allows_only_integer_one_or_three(value):
    with pytest.raises(ValidationError): StartInput(runs=value)


def test_temperature_is_part_of_configuration_fingerprint(rig):
    config = rig.processor.project.ai
    assert fingerprint(config) != fingerprint(config.model_copy(update={"temperature": 0.0}))


async def evaluated(rig, case):
    comment = Comment(username="quality_lab", text=case.viewer_text, source="playground")
    suggestion = await rig.processor.process(comment)
    return record_for(comment, suggestion, "playground", 10, case.id)


@pytest.mark.parametrize("field,value,check", [
    ("intent", "unknown", "intent_match"), ("fact_ids", ("automatic_trades",), "fact_ids_match"),
    ("fact_ids", ("automatic_analysis", "automatic_analysis"), "fact_ids_match"),
    ("detected_language", "es", "expected_language_detected"),
    ("requires_approval", False, "safe_status"), ("auto_send_eligible", True, "safe_status"),
    ("suggested_reply", "You will make money", "no_forbidden_claim"),
    ("reply_translation", "Вы точно заработаете", "no_forbidden_claim"),
    ("suggested_reply", "Unapproved product wording.", "grounded_source"),
    ("reply_translation", "Непроверенный факт.", "grounded_source"),
    ("suggested_reply", "x" * 241, "reply_length_pass"),
    ("suggested_reply", "One. Two. Three.", "reply_length_pass"),
    ("suggested_reply", "**Trade**", "reply_style_pass"),
    ("template_id", "unknown_template", "grounded_source"),
])
async def test_deterministic_failures(rig, corpus, field, value, check):
    case = corpus.cases[0]
    record = await evaluated(rig, case)
    # Deliberately corrupt projection to verify defense at evaluation boundary.
    item = record.suggestion.model_copy(update={field: value})
    result = evaluate(case, record.model_copy(update={"suggestion": item}), rig.processor.project, rig.processor.knowledge)
    assert not result.checks[check] and not result.passed
    assert check in result.critical_failures if check in {"intent_match", "fact_ids_match", "safe_status", "no_forbidden_claim", "grounded_source"} else check in result.other_failures


async def test_fact_order_is_set_match_and_source_composition_not_expected_string(rig, corpus):
    case = next(c for c in corpus.cases if c.category == "combined_analysis_trades")
    reversed_case = case.model_copy(update={"expected": case.expected.model_copy(update={"fact_ids": tuple(reversed(case.expected.fact_ids))})})
    record = await evaluated(rig, case)
    result = evaluate(reversed_case, record, rig.processor.project, rig.processor.knowledge)
    assert result.passed


async def test_bad_comment_grammar_does_not_gate_deterministic_result(rig, corpus):
    case = next(c for c in corpus.cases if c.viewer_text == "What is your win rate?")
    record = await evaluated(rig, case)
    item = record.suggestion.model_copy(update={"operator_translation": "Какая у вас процент успешных сделок?"})
    result = evaluate(case, record.model_copy(update={"suggestion": item}), rig.processor.project, rig.processor.knowledge)
    assert result.passed and result.review.comment_translation_quality == "unreviewed"


async def test_claim_checks_do_not_grade_untrusted_original_or_comment_translation(rig, corpus):
    case = next(c for c in corpus.cases if c.category == "prompt_injection")
    record = await evaluated(rig, case)
    result = evaluate(case, record, rig.processor.project, rig.processor.knowledge)
    assert result.checks["no_forbidden_claim"] and result.passed


async def test_summary_denominators_percentiles_and_breakdowns(rig, corpus):
    results = []
    for c, duration in zip(corpus.cases[:4], [10, 20, 30, 100]):
        record = (await evaluated(rig, c)).model_copy(update={"duration_ms": duration})
        results.append(evaluate(c, record, rig.processor.project, rig.processor.knowledge))
    wrong = results[-1].model_copy(update={"passed": False, "critical_failures": ("intent_match",),
        "checks": {**results[-1].checks, "intent_match": False}})
    summary = summarize(corpus, (*results[:3], wrong))
    assert (summary.total, summary.not_run, summary.passed, summary.failed, summary.critical_failed) == (4, 71, 3, 1, 1)
    assert summary.intent_accuracy == 75 and summary.fact_ids_accuracy == summary.should_reply_accuracy == 100
    assert (summary.average_duration_ms, summary.p50_duration_ms, summary.p95_duration_ms) == (40, 20, 100)
    assert summary.languages["en"].failed == 1 and summary.categories["automatic_analysis"].total == 4
    empty = summarize(corpus, ())
    assert empty.intent_accuracy is empty.average_duration_ms is empty.p95_duration_ms is None


async def test_single_active_run_and_playground_mutual_exclusion(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.engine.generate.side_effect
    async def slow(request):
        entered.set(); await release.wait(); return await original(request)
    rig.engine.generate.side_effect = slow
    await rig.service.start()
    await entered.wait()
    with pytest.raises(PlaygroundBusy): await rig.service.start()
    with pytest.raises(PlaygroundBusy): await rig.playground.analyze("Hello guys!")
    rig.service.stop(rig.service.run.run_id)
    assert rig.service.run.state == "stopping"
    release.set(); await finish(rig.service)
    assert rig.service.run.state == "stopped" and rig.service.run.summary.total == 1
    assert rig.service.run.summary.not_run == 74
    assert rig.engine.generate.await_count == 1
    await rig.playground.analyze("Hello guys!")


async def test_quality_cannot_start_while_playground_busy(rig):
    await rig.playground.lock.acquire()
    try:
        with pytest.raises(PlaygroundBusy): await rig.service.start()
        assert rig.engine.generate.await_count == 0
    finally: rig.playground.lock.release()


@pytest.mark.parametrize("before_first_iteration", [False, True])
async def test_shutdown_cancels_only_owned_run_and_releases_lock(rig, before_first_iteration):
    entered, unrelated_release = asyncio.Event(), asyncio.Event()
    async def never(request): entered.set(); await asyncio.Event().wait()
    rig.engine.generate.side_effect = never
    unrelated = asyncio.create_task(unrelated_release.wait())
    await rig.service.start()
    if not before_first_iteration: await entered.wait()
    await rig.service.aclose()
    assert rig.service.run.state == "stopped" and rig.service.task is None
    assert not rig.playground.lock.locked() and not unrelated.done()
    await rig.playground.aclose()
    rig.engine.aclose.assert_awaited_once()
    unrelated_release.set(); await unrelated


async def test_manual_review_is_bounded_ephemeral_and_does_not_change_summary(rig):
    original_bytes = DATASET_PATH.read_bytes()
    await rig.service.start(); await finish(rig.service)
    old = rig.service.run
    case_id = next(iter(rig.service.results))
    review = ManualReview(comment_translation_quality="bad", reply_naturalness="good", note="Operator observation")
    result = rig.service.review(old.run_id, case_id, review, old.run_number)
    assert result.review == review and rig.service.run.summary == old.summary
    assert DATASET_PATH.read_bytes() == original_bytes
    await rig.service.start(); await finish(rig.service)
    assert rig.service.results[case_id].review == ManualReview()
    with pytest.raises(StaleRun): rig.service.review(old.run_id, case_id, review, old.run_number)
    with pytest.raises(StaleRun): rig.service.stop(old.run_id)
    with pytest.raises(StaleRun): rig.service.review(rig.service.run.run_id, "missing", review, rig.service.run.run_number)


@pytest.mark.parametrize("value", [{"comment_translation_quality": "great"}, {"note": "x" * 1001}, {"auto_send": True}])
def test_review_rejects_bad_fields(value):
    with pytest.raises(ValidationError): ManualReview.model_validate(value)


@pytest.mark.parametrize("failure_kind", ["provider", "unexpected_processor"])
async def test_case_failure_is_safe_and_run_continues(rig, failure_kind, caplog):
    private = "SECRET_PROVIDER_BODY_DO_NOT_RETURN"
    if failure_kind == "provider": rig.engine.generate.side_effect = RuntimeError(private)
    else: rig.processor.process = AsyncMock(side_effect=RuntimeError(private))
    await rig.service.start(); await finish(rig.service)
    snapshot = rig.service.snapshot()
    assert snapshot.run.state == "completed" and snapshot.run.summary.failed == 75
    assert snapshot.run.summary.needs_operator_count == 75
    assert private not in snapshot.model_dump_json() and private not in caplog.text
    assert all(not r.record.suggestion.suggested_reply for r in snapshot.results)
    if failure_kind == "unexpected_processor": assert all(not r.checks["pipeline_completed"] for r in snapshot.results)


@pytest.mark.parametrize("runs", [1, 3])
async def test_openai_and_enabled_live_are_denied_even_with_paid_flag(rig, runs):
    rig.engine.name = "openai"
    service = QualityService(rig.playground, dataset=rig.service.dataset)
    with pytest.raises(QualityDisabled): await service.start(runs)
    assert rig.engine.generate.await_count == 0
    rig.engine.name = "lmstudio"
    service = QualityService(rig.playground, dataset=rig.service.dataset, live_enabled=True)
    with pytest.raises(QualityDisabled): await service.start(runs)


async def test_api_projection_guards_no_listener_no_paid_and_manual_review(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.LiveService", lambda *a, **k: pytest.fail("TikTok constructed"))
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda *a, **k: pytest.fail("paid client constructed"))
    app = create_app(settings.model_copy(update={"log_dir": tmp_path / "logs"}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            snapshot = (await client.get("/api/admin/quality")).json()
            assert len(snapshot["dataset"]["cases"]) == 75 and snapshot["run"]["state"] == "idle"
            assert "api_key" not in str(snapshot) and "log_dir" not in str(snapshot)
            assert (await client.post("/api/admin/quality/start", json={})).status_code == 403
            token = (await client.get("/api/admin/session")).json()["csrf_token"]
            client.headers["X-Admin-CSRF"] = token
            assert (await client.post("/api/admin/quality/start", json={"allow_paid": True})).status_code == 422
            start = await client.post("/api/admin/quality/start", json={})
            assert start.status_code == 200
            await finish(app.state.quality)
            run_id = start.json()["run_id"]
            case_id = snapshot["dataset"]["cases"][0]["id"]
            response = await client.post("/api/admin/quality/review", json={"run_id": run_id, "run_number": 1, "case_id": case_id,
                "review": {"comment_translation_quality": "bad", "note": "needs review"}})
            assert response.status_code == 200 and response.json()["review"]["note"] == "needs review"
            assert (await client.post("/api/admin/quality/stop", json={"run_id": "0" * 32})).status_code == 409
            assert (await client.post("/api/admin/quality/review", json={"run_id": run_id, "run_number": 1, "case_id": case_id,
                "review": {"note": "x" * 1001}})).status_code == 422
            assets = await client.get("/ui/assets/quality.js")
            assert assets.status_code == 200 and "innerHTML" not in assets.text
            assert not app.state.admin.live_history and not app.state.admin.playground.history
    assert app.state.quality.closed and app.state.admin.playground.closed


def test_quality_events_are_allowlisted(rig):
    sub = rig.events.subscribe()
    rig.events.publish("quality_update", QualityUpdate(run=rig.service.run))
    assert sub.queue.get_nowait().name == "quality_update"
    with pytest.raises(ValueError): rig.events.publish("quality_update", {"secret": "body"})


async def test_openai_route_blocks_before_client_creation(settings, tmp_path, monkeypatch):
    data = yaml.safe_load(settings.project_config_path.read_text(encoding="utf-8"))
    data["ai"]["provider"] = "openai"
    config_path = tmp_path / "openai.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda *a, **k: pytest.fail("paid client constructed"))
    app = create_app(settings.model_copy(update={"project_config_path": config_path, "log_dir": tmp_path / "logs"}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            client.headers["X-Admin-CSRF"] = app.state.admin.csrf_token
            snapshot = (await client.get("/api/admin/quality")).json()
            assert not snapshot["run"]["enabled"] and snapshot["run"]["provider"] == "openai"
            assert (await client.post("/api/admin/quality/start", json={})).status_code == 403
            assert (await client.post("/api/admin/quality/start", json={"allow_paid": True})).status_code == 422
            assert (await client.post("/api/admin/quality/start", json={"runs": 3})).status_code == 403
            assert not app.state.quality.results


async def test_run_failure_releases_lock_and_has_only_static_error(rig, monkeypatch):
    def broken(*a, **k): raise RuntimeError("PRIVATE_INTERNAL_EXCEPTION_BODY")
    monkeypatch.setattr("app.quality.service.evaluate", broken)
    await rig.service.start(); await finish(rig.service)
    assert rig.service.run.state == "failed" and rig.service.run.error_category == "run_failed"
    assert rig.service.run.summary.total == 0 and rig.service.run.summary.not_run == 75
    assert not rig.playground.lock.locked() and rig.service.task is None
    assert "PRIVATE_INTERNAL" not in rig.service.snapshot().model_dump_json()


async def test_abstention_and_wrong_should_reply_are_not_safe_reply_counts(rig, corpus):
    case = corpus.cases[0]
    rig.engine.generate.side_effect = RuntimeError("provider unavailable")
    record = await evaluated(rig, case)
    result = evaluate(case, record, rig.processor.project, rig.processor.knowledge)
    assert result.checks["safe_status"] and not result.checks["should_reply_match"]
    assert result.pipeline_error == "provider_error" and "pipeline_completed" in result.critical_failures
    summary = summarize(corpus, (result,))
    assert summary.safe_reply_count == summary.ready_count == 0 and summary.needs_operator_count == 1
