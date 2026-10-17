"""Local profile model discovery and bounded draft knowledge management."""
import asyncio
import base64
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.admin.models import UIModel
from app.admin.runtime import RuntimeConflict
from app.ai.local_knowledge import KnowledgeError
from app.ai.lmstudio_provider import LMStudioReplyEngine

router = APIRouter()


@router.get('/api/admin/periodic/status')
async def periodic_status(request: Request): return request.app.state.operator_runtime.periodic.view()


class TextSource(UIModel):
    title: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=131072)


class FileSource(UIModel):
    filename: str = Field(min_length=1, max_length=120)
    content_base64: str = Field(min_length=1, max_length=174768)


class SourceEnabled(UIModel):
    enabled: bool = Field(strict=True)


@router.post('/api/admin/ai-profile/models')
async def models(request: Request, body: UIModel):
    runtime = request.app.state.operator_runtime
    engine = runtime.admin.playground.processor.engine
    if not isinstance(engine, LMStudioReplyEngine): raise HTTPException(409, detail='Apply a local OpenAI-compatible endpoint first.')
    if runtime.applying: raise HTTPException(409, detail='Wait for Apply to finish.')
    try:
        async with asyncio.timeout(8): values = await engine.list_models()
        return dict(models=values, configured_model=engine.model)
    except Exception:
        raise HTTPException(409, detail='Model discovery unavailable. The configured model is unchanged; manual entry is available.') from None


@router.get('/api/admin/knowledge')
async def knowledge(request: Request):
    try: return request.app.state.operator_runtime.knowledge.view()[0]
    except Exception: raise HTTPException(409, detail='Local knowledge index is unavailable.') from None


async def mutate(request, action):
    r = request.app.state.operator_runtime
    try:
        async with r.lock:
            r.ensure_idle()
            action(r.knowledge)
        r.publish()
        return r.knowledge.view()[0]
    except RuntimeConflict as exc: raise HTTPException(409, detail=str(exc)) from None
    except KnowledgeError as exc: raise HTTPException(422, detail=str(exc)) from None
    except Exception: raise HTTPException(409, detail='Local knowledge update failed; no source was partially indexed.') from None


@router.post('/api/admin/knowledge/text')
async def add_text(request: Request, body: TextSource):
    return await mutate(request, lambda store: store.add(body.title+'.txt', body.text.encode('utf-8')))


@router.post('/api/admin/knowledge/upload')
async def upload(request: Request, body: FileSource):
    try: raw = base64.b64decode(body.content_base64, validate=True)
    except ValueError: raise HTTPException(422, detail='Invalid UTF-8 text upload.') from None
    return await mutate(request, lambda store: store.add(body.filename,raw))


@router.post('/api/admin/knowledge/{identity}/enabled')
async def enabled(request: Request, identity: str, body: SourceEnabled):
    return await mutate(request, lambda store: store.update(identity,enabled=body.enabled))


@router.post('/api/admin/knowledge/{identity}/delete')
async def delete(request: Request, identity: str, body: UIModel):
    return await mutate(request, lambda store: store.update(identity,delete=True))
