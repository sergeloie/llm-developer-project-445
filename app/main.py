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

from app import cards, orchestrator, permissions, sessions
from core import config, db


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
        "visible_tools": permissions.visible_tools(
            session.allowlist, session.pool.tool_names()
        ),
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


def _find_card(session, pending_id: str):
    active = cards.active(session)
    if active is not None and active.pending_id == pending_id:
        return active
    row = db.fetchone(
        "select pending_id, action, details, card, sub from pending_cards"
        " where pending_id = %s and session_id = %s",
        (pending_id, session.session_id),
    )
    if row is None:
        return None
    return cards.Card(
        pending_id=row["pending_id"], action=row["action"],
        details=row["details"], view=row["card"],
        session_id=session.session_id, sub=row["sub"],
    )


@app.post("/api/cards/{pending_id}/decision")
async def card_decision(pending_id: str, payload: dict):
    """Кнопки карточки: «да» исполняет, «нет» отменяет (решение кода)."""
    session = sessions.get(payload.get("session_id", ""))
    if session is None:
        return JSONResponse({"error": "неизвестная сессия"}, status_code=400)
    card = _find_card(session, pending_id)
    if card is None:
        return JSONResponse({"error": "карточка не найдена"}, status_code=404)
    text = await orchestrator.decide(session, card, str(payload.get("answer", "")))
    return {"text": text}
