"""One sequential run; share Playground's processor, lock and lazy client."""
import asyncio
from datetime import UTC, datetime
import logging
from time import perf_counter
from uuid import uuid4

from app.admin.service import PlaygroundBusy, record_for
from app.ai.models import ReplySuggestion
from app.live.models import Comment
from app.logging_config import LOGGER_NAME
from app.quality.dataset import fingerprint, load_dataset
from app.quality.evaluation import evaluate, summarize, summarize_stability
from app.quality.models import QualityRun, QualitySnapshot, QualityUpdate, RunView, TrialSummary

logger = logging.getLogger(LOGGER_NAME)


class QualityDisabled(RuntimeError):
    pass


class StaleRun(ValueError):
    pass


class QualityService:
    def __init__(self, playground, *, live_enabled=False, dataset=None):
        self.playground = playground
        processor = playground.processor
        self.dataset = dataset or load_dataset(processor.knowledge, processor.project.ai.max_comment_chars)
        self.results = {}
        self.completed_runs = []
        self._generation_start = self._generation_trial_start = None
        self.task = None
        self.closed = False
        self.stop_requested = False
        reason = ("Quality Lab запрещает OpenAI." if processor.engine.name == "openai" else
                  "75-кейсный baseline использует FOCUSED. Примените FOCUSED в настройках." if processor.project.ai.reply_policy != "focused" else
                  "Отключите LIVE перед запуском Quality Lab." if live_enabled else None)
        self.base_disabled_reason = ("Quality Lab запрещает OpenAI." if processor.engine.name == "openai" else
                  "75-кейсный baseline использует FOCUSED. Примените FOCUSED в настройках." if processor.project.ai.reply_policy != "focused" else None)
        if processor.project.ai_profile is not None:
            reason = self.base_disabled_reason = "Legacy trading corpus не оценивает generic profile. Используйте Playground со своей KB."
        self.run = RunView(service_epoch=uuid4().hex, revision=0, dataset_name=self.dataset.name, dataset_version=self.dataset.version,
            dataset_hash=fingerprint(self.dataset), knowledge_hash=fingerprint(processor.knowledge),
            ai_config_hash=fingerprint(processor.project.ai), provider=processor.engine.name,
            model=getattr(processor.engine, "model", processor.project.ai.model), enabled=reason is None, disabled_reason=reason,
            temperature=processor.project.ai.temperature,
            analysis_prompt_hash=getattr(processor.engine, "analysis_prompt_hash", None),
            translation_prompt_hash=getattr(processor.engine, "translation_prompt_hash", None),
            stability=summarize_stability(self.dataset, (), requested=1), summary=summarize(self.dataset, ()))

    def set_live_enabled(self, enabled):
        reason = self.base_disabled_reason or ("Отключите LIVE перед запуском Quality Lab." if enabled else None)
        self._update(enabled=reason is None, disabled_reason=reason)

    def snapshot(self):
        return QualitySnapshot(dataset=self.dataset, run=self.run, results=tuple(self.results.values()),
                               completed_runs=tuple(self.completed_runs))

    def _counter(self):
        value = getattr(self.playground.processor.engine, "generation_count", None)
        return value if type(value) is int else 0 if self.run.provider == "fake" else None

    def _calls_since(self, value):
        current = self._counter()
        return current - value if current is not None and value is not None else None

    def _update(self, *, result=None, **values):
        number = values.get("run_number", self.run.run_number)
        current = () if any(r.run_number == number for r in self.completed_runs) else tuple(self.results.values())
        self.run = self.run.model_copy(update={**values, "revision": self.run.revision + 1,
            "summary": summarize(self.dataset, tuple(self.results.values())),
            "completed_summaries": tuple(TrialSummary.model_validate(r.model_dump(exclude={"results"})) for r in self.completed_runs),
            "stability": summarize_stability(self.dataset, self.completed_runs,
                requested=values.get("runs_requested", self.run.runs_requested), current=current,
                model_calls=self._calls_since(self._generation_start))})
        self.playground.events.publish("quality_update", QualityUpdate(run=self.run, result=result))

    async def start(self, runs=1):
        if type(runs) is not int or runs not in {1, 3}:
            raise ValueError("Only one or three sequential runs")
        if not self.run.enabled:
            raise QualityDisabled(self.run.disabled_reason)
        if self.closed or self.task is not None or self.playground.closed or self.playground.lock.locked():
            raise PlaygroundBusy()
        # Acquire before returning: Playground cannot slip an inference between
        # scheduling the runner and its first iteration. No await while locked above.
        await self.playground.lock.acquire()
        self.playground.on_busy_change()
        self.results.clear()
        self.completed_runs.clear()
        self.stop_requested = False
        self._generation_start = self._counter()
        self._update(run_id=uuid4().hex, state="running", started_at=datetime.now(UTC),
                     finished_at=None, current_case_id=None, error_category=None, runs_requested=runs,
                     run_number=1, iteration_id=None, iteration_started_at=None)
        self.task = asyncio.create_task(self._run(), name="quality-lab")
        self.task.add_done_callback(self._finished)
        return self.run

    def _finished(self, task):
        # Also runs if shutdown cancels the task before its coroutine starts.
        if self.task is task:
            if self.run.state in {"running", "stopping"}:
                self._update(state="stopped" if task.cancelled() else "failed",
                             error_category=None if task.cancelled() else "run_failed")
            self.playground.lock.release()
            self.playground.on_busy_change()
            self.task = None
            self._update(current_case_id=None, finished_at=datetime.now(UTC))

    async def _run(self):
        try:
            for number in range(1, self.run.runs_requested + 1):
                if self.stop_requested:
                    break
                self.results.clear()
                self._generation_trial_start = self._counter()
                self._update(run_number=number, iteration_id=uuid4().hex, iteration_started_at=datetime.now(UTC))
                await self._trial()
                if len(self.results) == len(self.dataset.cases):
                    self.completed_runs.append(QualityRun(iteration_id=self.run.iteration_id, run_number=number,
                        started_at=self.run.iteration_started_at, finished_at=datetime.now(UTC),
                        dataset_hash=self.run.dataset_hash, knowledge_hash=self.run.knowledge_hash,
                        ai_config_hash=self.run.ai_config_hash, provider=self.run.provider, model=self.run.model,
                        temperature=self.run.temperature, analysis_prompt_hash=self.run.analysis_prompt_hash,
                        translation_prompt_hash=self.run.translation_prompt_hash,
                        model_generation_calls=self._calls_since(self._generation_trial_start),
                        summary=summarize(self.dataset, tuple(self.results.values())), results=tuple(self.results.values())))
                    self._update()
                if self.stop_requested:
                    break
            self._update(state="stopped" if self.stop_requested else "completed")
        except asyncio.CancelledError:
            self._update(state="stopped")
            raise
        except Exception as exc:
            logger.warning("quality_run_failed", extra={"fields": {"error_type": type(exc).__name__}})
            self._update(state="failed", error_category="run_failed")

    async def _trial(self):
        processor = self.playground.processor
        for case in self.dataset.cases:
            if self.stop_requested:
                break
            self._update(current_case_id=case.id)
            started = perf_counter()
            comment = Comment(username="quality_lab", text=case.viewer_text, source="playground")
            error = None
            try:
                suggestion = await processor.process(comment)
            except Exception as exc:
                logger.warning("quality_case_failed", extra={"fields": {"error_type": type(exc).__name__}})
                error = "processing_failed"
                suggestion = ReplySuggestion(username=comment.username, original_text=comment.text,
                    detected_language="und", operator_translation=None, should_reply=False,
                    suggested_reply=None, reply_translation=None, reason="Обработка не выполнена. Требуется оператор.",
                    status="needs_operator", provider=self.run.provider, model=self.run.model)
            record = record_for(comment, suggestion, "playground", round((perf_counter() - started) * 1000), case.id)
            result = evaluate(case, record, processor.project, processor.knowledge, pipeline_error=error)
            self.results[case.id] = result
            self._update(result=result)
            # Allow STOP/disconnection requests even when mocked/fake work
            # completes without suspending. This is not an inference delay.
            await asyncio.sleep(0)

    def _require_run(self, run_id):
        if run_id != self.run.run_id:
            raise StaleRun("Run replaced")

    def stop(self, run_id):
        self._require_run(run_id)
        if self.task is not None:
            self.stop_requested = True
            self._update(state="stopping")
        return self.run

    def review(self, run_id, case_id, review, run_number):
        self._require_run(run_id)
        if type(run_number) is not int or run_number != self.run.run_number:
            raise StaleRun("Trial replaced")
        if case_id not in self.results:
            raise StaleRun("Case not completed")
        result = self.results[case_id].model_copy(update={"review": review})
        self.results[case_id] = result
        for index, completed in enumerate(self.completed_runs):
            if completed.run_number == self.run.run_number:
                self.completed_runs[index] = completed.model_copy(update={
                    "results": tuple(self.results.values())})
        self._update(result=result)
        return result

    async def aclose(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
