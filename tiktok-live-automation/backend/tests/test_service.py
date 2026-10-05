import asyncio
import ast
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.live.models import LiveState
from app.live.service import LiveService
from app.main import create_app


async def test_service_delivers_all_comments_despite_callback_error(settings):
    received = []

    async def callback(comment):
        received.append(comment.username)
        if len(received) == 1:
            raise ValueError("downstream problem")

    service = LiveService(settings, on_comment=callback)
    await asyncio.wait_for(service.run(), 1)
    assert received == ["john92", "alice", "mike"]
    assert service.queue.empty()


async def test_service_cancel_leaves_no_owned_tasks(settings):
    service = LiveService(settings.model_copy(update={
        "fake_repeat": True, "fake_interval_seconds": 0.01,
    }))
    received = asyncio.Event()

    async def callback(comment):
        received.set()

    service.on_comment = callback
    task = asyncio.create_task(service.run())
    await asyncio.wait_for(received.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert service.status.state == LiveState.STOPPED
    assert not [t for t in asyncio.all_tasks() if t.get_name() in {
        "comment-consumer", "live-listener",
    }]


async def test_fastapi_lifespan_and_health(settings, tmp_path):
    app = create_app(settings.model_copy(update={"log_dir": tmp_path}))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app.state.live_task, 1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["live"]["mode"] == "fake"
        assert response.json()["live"]["state"] == "stopped"
        assert response.json()["listener_running"] is False


def test_sdk_import_boundary():
    root = Path(__file__).parents[1] / "app"
    for file in root.rglob("*.py"):
        if file.name == "listener.py":
            continue
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("TikTokLive"), str(file)
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith("TikTokLive") for item in node.names), str(file)

