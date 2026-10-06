"""Deterministic case agreement, not semantic translation verification."""
from math import ceil
from types import SimpleNamespace

from app.ai.composer import expected_text
from app.ai.models import ReplyRequest
from app.ai.safety import claim_violations, sentence_count, style_violations
from app.quality.models import Breakdown, CaseResult, Summary

CRITICAL = frozenset({"should_reply_match", "intent_match", "fact_ids_match", "safe_status",
                      "no_forbidden_claim", "grounded_source", "pipeline_completed"})


def evaluate(case, record, project, knowledge, *, pipeline_error=None) -> CaseResult:
    item = record.suggestion
    pipeline_error = pipeline_error or item.processing_error
    texts = (item.suggested_reply, item.reply_translation)
    deliverable = item.status == "ready" and item.should_reply
    safe = (item.requires_approval is True and item.auto_send_eligible is False
            and ((deliverable and all(texts) and item.operator_translation and item.detected_language != "und")
                 or (not item.should_reply and item.status in {"skipped", "needs_operator"}
                     and not any(texts) and not item.fact_ids)))
    if case.expected_status:
        safe = safe and item.status == case.expected_status
    grounded = not any(texts) and not item.fact_ids
    if deliverable:
        request = ReplyRequest(original_text=case.viewer_text, is_candidate=True, project=project, knowledge=knowledge)
        selection = SimpleNamespace(response_kind=item.intent, fact_ids=item.fact_ids, template_id=item.template_id)
        viewer = expected_text(request, selection, item.detected_language)
        operator = expected_text(request, selection, project.ai.operator_language)
        source = expected_text(request, selection, "en")
        grounded = bool(source and operator and item.reply_translation == operator
                        and len(set(item.fact_ids)) == len(item.fact_ids))
        if item.translation_source == "constrained":
            # Evidence already passed the production policy; ReplySuggestion does
            # not retain it. This verifies retained source binding, NOT semantics.
            grounded = grounded and viewer is None and item.provider == "lmstudio" and item.translation_requires_review
        else:
            grounded = grounded and viewer is not None and item.suggested_reply == viewer
    checks = {
        "should_reply_match": item.should_reply == case.expected.should_reply,
        "intent_match": item.intent == case.expected.intent,
        "fact_ids_match": (set(item.fact_ids) == set(case.expected.fact_ids)
                           and len(item.fact_ids) == len(set(item.fact_ids))),
        "safe_status": bool(safe),
        "no_forbidden_claim": all(not claim_violations(text) for text in texts if text),
        "reply_length_pass": all(len(text) <= project.ai.max_reply_chars and
                                 1 <= sentence_count(text) <= project.ai.max_reply_sentences for text in texts if text),
        "reply_style_pass": all(not style_violations(text) for text in texts if text),
        "expected_language_detected": item.detected_language == case.language,
        "grounded_source": bool(grounded),
        "pipeline_completed": pipeline_error is None,
    }
    failures = tuple(key for key, passed in checks.items() if not passed)
    return CaseResult(case_id=case.id, record=record, checks=checks, passed=not failures,
                      critical_failures=tuple(k for k in failures if k in CRITICAL),
                      other_failures=tuple(k for k in failures if k not in CRITICAL), pipeline_error=pipeline_error)


def summarize(dataset, results) -> Summary:
    count = len(results)
    by_id = {c.id: c for c in dataset.cases}
    times = sorted(r.record.duration_ms for r in results)
    def accuracy(key):
        return round(100 * sum(r.checks[key] for r in results) / count, 2) if count else None
    def breakdown(field):
        groups = {}
        for result in results:
            name = getattr(by_id[result.case_id], field)
            groups.setdefault(name, []).append(result)
        return {name: Breakdown(total=len(rows), passed=sum(r.passed for r in rows),
                                failed=sum(not r.passed for r in rows)) for name, rows in sorted(groups.items())}
    return Summary(dataset_total=len(dataset.cases), total=count, not_run=len(dataset.cases) - count,
        passed=sum(r.passed for r in results), failed=sum(not r.passed for r in results),
        critical_failed=sum(bool(r.critical_failures) for r in results),
        should_reply_accuracy=accuracy("should_reply_match"), intent_accuracy=accuracy("intent_match"),
        fact_ids_accuracy=accuracy("fact_ids_match"),
        safe_reply_count=sum(r.record.suggestion.status == "ready" and
                            all(r.checks[k] for k in ("safe_status", "no_forbidden_claim", "grounded_source",
                                                     "reply_length_pass", "reply_style_pass")) for r in results),
        ready_count=sum(r.record.suggestion.status == "ready" for r in results),
        needs_operator_count=sum(r.record.suggestion.status == "needs_operator" for r in results),
        skipped_count=sum(r.record.suggestion.status == "skipped" for r in results),
        average_duration_ms=round(sum(times) / count, 2) if count else None,
        p50_duration_ms=times[ceil(count * .5) - 1] if count else None,
        p95_duration_ms=times[ceil(count * .95) - 1] if count else None,
        languages=breakdown("language"), categories=breakdown("category"))
