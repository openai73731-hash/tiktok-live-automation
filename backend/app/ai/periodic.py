"""Owned monotonic slots. Mint private periodic provenance, then use existing AUTO.

SQLite stores slot IDs, hashes and results only. A claimed slot is never resumed,
including a crash before generation/delivery; missed slots never catch up.
"""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import secrets
import sqlite3
from time import monotonic, time
from uuid import uuid4
from types import SimpleNamespace

from app.ai.generic import process_generic
from app.ai.local_knowledge import safe_path
from app.delivery.models import AndroidTarget
from app.live.models import Comment


class SlotStore:
    def __init__(self, state_dir): self.path = Path(state_dir) / 'periodic.sqlite3'

    def claim(self, identity, account, maximum, now):
        safe_path(self.path); self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=.1)
        try:
            db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS slots (id TEXT PRIMARY KEY,account TEXT NOT NULL,created REAL NOT NULL,result TEXT NOT NULL,text_hash TEXT)')
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM slots WHERE account=? AND created>=?', (account,now-3600)).fetchone()[0]
            if count >= maximum or db.execute('SELECT count(*) FROM slots').fetchone()[0] >= 10000:
                db.execute('ROLLBACK'); return False
            db.execute('INSERT INTO slots VALUES (?,?,?,\'claimed\',NULL)', (identity,account,now)); db.execute('COMMIT')
            return True
        finally: db.close()

    def finish(self, identity, result, text_hash=None):
        safe_path(self.path)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=.1)
        try: db.execute('UPDATE slots SET result=?,text_hash=coalesce(?,text_hash) WHERE id=?',(result,text_hash,identity))
        finally: db.close()

    def last_hash(self, account):
        safe_path(self.path)
        if not self.path.exists(): return None
        db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
        try:
            row=db.execute('SELECT text_hash FROM slots WHERE account=? AND text_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1',(account,)).fetchone()
            return row[0] if row else None
        finally: db.close()


@dataclass(frozen=True)
class PeriodicProof:
    slot: str
    owner: object
    token: object

    def valid(self, envelope, context): return self.owner.valid(self, envelope, context)


class PeriodicScheduler:
    def __init__(self, runtime, *, clock=monotonic, wall=time, jitter=None):
        self.runtime, self.clock, self.wall = runtime, clock, wall
        self.jitter = jitter or (lambda n: secrets.randbelow(2*n+1)-n if n else 0)
        self.store = SlotStore(runtime.settings.state_dir)
        self.task = None
        self.binding = self.due = None
        self.state, self.last_result, self.last_send = 'disabled', None, None
        self.capabilities = {}
        self.running = False

    def start(self): self.task = asyncio.create_task(self.run(),name='owned-periodic-scheduler')

    async def run(self):
        while not self.runtime.closed:
            await asyncio.sleep(1)
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception:
                self.state, self.last_result = 'blocked', 'periodic_storage_or_generation_unavailable'

    def view(self):
        remaining = max(0,self.due-self.clock()) if self.due is not None else None
        band = [int(remaining//30)*30, int(remaining//30)*30+30] if remaining is not None else None
        return dict(state=self.state,last_result=self.last_result,last_send=self.last_send,next_due_seconds_range=band)

    def blocked(self):
        r, s = self.runtime, self.runtime.admin.delivery
        if (r.closed or r.transport!='android' or not r.project.ai_profile or r.pending or r.config_error
                or s.closed or s.mode!='AUTOMATIC' or s.paused or s.pending_fill or s.busy or s.tasks
                or not s.auto_queue.empty() or s.cooldown or r.admin.live_pending
                or s.settings.mobile_acceptance_one_shot and s.acceptance_spent): return True
        scope = s.account_proof.scope()
        if (not scope or not s.account_proof.prepared_now() or s._verified_runtime_revision != scope[1]
                or s.armed_binding != (scope[2],scope[0],scope[1])
                or s.controller._account_binding != (s.controller._installation,scope[0])): return True
        from app.android.delivery_evidence import unresolved
        # Match PREPARE/ACK recovery: a settled same-device MANUAL rejection
        # stays consumed without blocking new identities. Ambiguous rows block.
        return unresolved(s, allow_rejected=True)

    def valid(self, proof, e, context):
        s = self.runtime.admin.delivery
        value = self.capabilities.get(proof.slot)
        return bool(value and proof.owner is self and proof.token is value[0]
            and e._periodic_id == proof.slot and e.source=='periodic' and e.message_purpose=='PERIODIC'
            and s.mode=='AUTOMATIC' and not s.paused and not s.closed
            and s.account_proof.scope()==value[1] and s.controller._installation==value[2]
            and s.armed_binding==value[3] and s.auto_generation==value[4]
            and (context.target_account,context.active_runtime_revision,context.live_session_id)==value[1][:3])

    async def tick(self):
        if self.running: return
        self.running = True
        try: await self._tick()
        finally:
            # A slow generation/send must not leave an overdue slot to catch up.
            p=self.runtime.project.ai_profile
            if p and self.due is not None and self.clock() >= self.due:
                c=p.periodic_comments
                self.due=self.clock()+c.interval_seconds+self.jitter(c.jitter_seconds)
            self.running = False

    async def _tick(self):
        r = self.runtime
        p = r.project.ai_profile
        if not p or not p.periodic_comments.enabled or r.transport!='android':
            self.binding=self.due=None; self.state='disabled'; return
        cfg = p.periodic_comments
        binding=(r.active_runtime_revision,cfg.model_dump_json())
        now=self.clock()
        if binding!=self.binding:
            self.binding=binding; self.due=now+cfg.interval_seconds+self.jitter(cfg.jitter_seconds)
            self.state='waiting'; return
        if now < self.due: return
        # Advance BEFORE any await; one overdue slot only, no catch-up burst.
        self.due=now+cfg.interval_seconds+self.jitter(cfg.jitter_seconds)
        if self.blocked(): self.state,self.last_result='blocked','skipped_unavailable'; return
        s=r.admin.delivery
        scope=s.account_proof.scope()
        install=s.controller._installation
        generation=s.auto_generation
        self.state='checking'
        state=await s.controller.inspect(AndroidTarget(account=scope[0],live_session_id=scope[2],device_serial=s.controller.serial))
        if (self.blocked() or s.account_proof.scope()!=scope or s.controller._installation!=install
                or s.auto_generation!=generation or state.security_state or not s.device_ready(s.context())):
            self.state,self.last_result='blocked','skipped_readiness'; return
        identity=uuid4().hex
        if not self.store.claim(identity,scope[0],cfg.max_per_hour,self.wall()):
            self.state,self.last_result='blocked','rate_limited'; return
        # Cap private in-memory proofs; never evict a still-live proof to admit more.
        if len(self.capabilities)>=1000:
            self.store.finish(identity,'capacity'); self.last_result='capacity'; return
        token=object()
        proof=PeriodicProof(identity,self,token)
        self.capabilities[identity]=(token,scope,install,s.armed_binding,generation)
        comment=Comment(username='Periodic scheduler',text='Scheduled neutral LIVE comment',source='periodic')
        comment.bind_ingestion(scope[0],scope[2],scope[1],None)
        comment._periodic_id,comment._periodic_proof=identity,proof
        previous=self.store.last_hash(scope[0])
        template=None
        if cfg.mode=='templates' or cfg.mode=='mixed' and secrets.randbelow(2)==0:
            choices=[v for v in cfg.templates if sha256(v.encode()).hexdigest()!=previous]
            if not choices:
                self.store.finish(identity,'repeat_skipped'); self.last_result='repeat_skipped'; return
            template=secrets.choice(choices)
        self.state='generating'
        processor=r.live.processor
        suggestion=await process_generic(processor,comment,purpose='PERIODIC',template=template)
        if self.blocked() or not proof.valid(SimpleNamespace(_periodic_id=identity,source='periodic',message_purpose='PERIODIC'),s.context()):
            self.store.finish(identity,'context_changed'); self.last_result='context_changed'; return
        if suggestion.status!='ready':
            self.store.finish(identity,'content_rejected'); self.last_result='content_rejected'; return
        text_hash=sha256(suggestion.suggested_reply.encode()).hexdigest()
        if text_hash==previous:
            self.store.finish(identity,'repeat_skipped',text_hash); self.last_result='repeat_skipped'; return
        self.store.finish(identity,'generated',text_hash)
        from app.admin.models import ReplyRecord, LiveReplyEvent
        record=ReplyRecord(id=identity,source='periodic',received_at=comment.timestamp,duration_ms=0,
            original_truncated=False,suggestion=suggestion,live_session_id=scope[2])
        record=r.admin.register_record(record,comment,processor,session_id=scope[2],target_account=scope[0])
        r.admin.live_history.appendleft(record)
        while len(r.admin.live_history)+len(r.admin.live_pending)>50:r.admin.live_history.pop()
        r.admin.events.publish('reply_suggestion',LiveReplyEvent(record=record,metrics=r.admin.metrics))
        self.state='sending'
        s.offer_auto(record.id)
        if s.auto_runner:
            try: await asyncio.shield(s.auto_runner)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling(): raise
        operation=s.journal.lookup('auto_'+identity) if s.journal else None
        self.last_result=operation.status if operation else 'skipped_delivery'
        self.store.finish(identity,self.last_result)
        if operation and operation.status=='confirmed':self.last_send=datetime.now(UTC).isoformat()
        self.state='waiting' if not s.paused else 'blocked'
        self.capabilities.pop(identity,None)

    async def aclose(self):
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)
