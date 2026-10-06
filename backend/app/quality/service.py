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
from app.quality.evaluation import evaluate, summarize
from app.quality.models import QualitySnapshot, QualityUpdate, RunView

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
        self.task = None
        self.closed = False
        self.stop_requested = False
        reason = ("Quality Lab запрещает OpenAI." if processor.engine.name == "openai" else
                  "Запустите панель с ADMIN_LIVE_ENABLED=false." if live_enabled else None)
        self.run = RunView(revision=0, dataset_name=self.dataset.name, dataset_version=self.dataset.version,
            dataset_hash=fingerprint(self.dataset), knowledge_hash=fingerprint(processor.knowledge),
            ai_config_hash=fingerprint(processor.project.ai), provider=processor.engine.name,
            model=getattr(processor.engine, "model", processor.project.ai.model), enabled=reason is None, disabled_reason=reason,
            summary=summarize(self.dataset, ()))

    def snapshot(self):
        return QualitySnapshot(dataset=self.dataset, run=self.run, results=tuple(self.results.values()))

    def _update(self, *, result=None, **values):
        self.run = self.run.model_copy(update={**values, "revision": self.run.revision + 1,
            "summary": summarize(self.dataset, tuple(self.results.values()))})
        self.playground.events.publish("quality_update", QualityUpdate(run=self.run, result=result))

    async def start(self):
        if not self.run.enabled:
            raise QualityDisabled(self.run.disabled_reason)
        if self.closed or self.task is not None or self.playground.closed or self.playground.lock.locked():
            raise PlaygroundBusy()
        # Acquire before returning: Playground cannot slip an inference between
        # scheduling the runner and its first iteration. No await while locked above.
        await self.playground.lock.acquire()
        self.results.clear()
        self.stop_requested = False
        self._update(run_id=uuid4().hex, state="running", started_at=datetime.now(UTC),
                     finished_at=None, current_case_id=None, error_category=None)
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
            self.task = None
            self._update(current_case_id=None, finished_at=datetime.now(UTC))

    async def _run(self):
        processor = self.playground.processor
        try:
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
            self._update(state="stopped" if self.stop_requested else "completed")
        except asyncio.CancelledError:
            self._update(state="stopped")
            raise
        except Exception as exc:
            logger.warning("quality_run_failed", extra={"fields": {"error_type": type(exc).__name__}})
            self._update(state="failed", error_category="run_failed")

    def _require_run(self, run_id):
        if run_id != self.run.run_id:
            raise StaleRun("Run replaced")

    def stop(self, run_id):
        self._require_run(run_id)
        if self.task is not None:
            self.stop_requested = True
            self._update(state="stopping")
        return self.run

    def review(self, run_id, case_id, review):
        self._require_run(run_id)
        if case_id not in self.results:
            raise StaleRun("Case not completed")
        result = self.results[case_id].model_copy(update={"review": review})
        self.results[case_id] = result
        self._update(result=result)
        return result

    async def aclose(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
