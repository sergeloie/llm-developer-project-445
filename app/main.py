"""Backend + оркестратор: HTTP-граница чата.

    uvicorn app.main:app --host 127.0.0.1 --port 8000

  POST /api/session                      {"user": "anna"} → открыть сессию
  POST /api/chat                         {"session_id", "message"} → SSE-поток хода
  POST /api/cards/{pending_id}/decision  подтверждение карточки (с контрактов)

События SSE хода: plan / card / reply / error / done.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app import orchestrator, sessions
from core import config


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.setup_model()
    # TODO(failover): llm_pool.setup_pool(), если задан LLM_SECONDARY_BASE_URL.
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "backend"}


@app.post("/api/session")
async def open_session(payload: dict):
    login = payload.get("user")
    try:
        session = await sessions.open_session(login)
    except KeyError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {
        "session_id": session.session_id,
        "name": session.user.name,
        "role": session.user.role,
        # TODO(права): permissions.visible_tools(session.allowlist, pool.tool_names()).
        "visible_tools": session.pool.tool_names(),
    }


@app.post("/api/chat")
async def chat(payload: dict):
    session = sessions.get(payload.get("session_id", ""))
    if session is None:
        return JSONResponse({"error": "неизвестная сессия"}, status_code=400)
    message = str(payload.get("message", "")).strip()
    if not message:
        return JSONResponse({"error": "пустое сообщение"}, status_code=400)

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            await orchestrator.run_turn(session, message, emit)
        except Exception as error:  # noqa: BLE001 — сбой хода не роняет SSE-поток
            await emit({"type": "error", "detail": str(error)[:240]})
        finally:
            await emit({"type": "__done__"})

    task = asyncio.create_task(run())

    async def stream():
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "__done__":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# TODO(контракты): POST /api/cards/{pending_id}/decision — кнопка карточки:
#   «да» → orchestrator.execute_pending, «нет» → отмена.
