"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

MCP-инструмент превращается в FunctionTool агента; субъект вызова — в
Bearer-токене соединения. Каждый вызов модели попадает в tool_calls с
обрезанными аргументами и исходом; кривой JSON становится машиночитаемым
ответом модели, а не падением сессии.
"""
from __future__ import annotations

import json
import time

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import audit, cards
from app.sessions import Session
from core import config, db


def _clip(value, limit: int = 200):
    """Аргументы журнала обрезаны: переписка в аудит не нужна."""
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in list(value)[:10]]
    if isinstance(value, str):
        return value[:limit]
    return value


def _log_tool(session: Session, name: str, args, outcome: str, started: float) -> None:
    try:
        db.execute(
            "insert into tool_calls(trace_id, tool, args, outcome, latency_ms)"
            " values (%s, %s, %s, %s, %s)",
            (
                session.trace_id or None,
                name,
                json.dumps(_clip(args), ensure_ascii=False),
                outcome[:60],
                int((time.monotonic() - started) * 1000),
            ),
        )
    except Exception:  # noqa: BLE001 — журнал не роняет вызов
        pass


def _log_prepare(session: Session, action: str, details: dict, pending_id: str) -> None:
    try:
        audit.append(
            session.user.sub, f"prepare:{action}", "prepared",
            trace_id=session.trace_id or None,
            session_id=session.session_id,
            args=details,
            detail=f"pending_id={pending_id}",
        )
    except Exception:  # noqa: BLE001
        pass


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""

    schema = session.pool.tool_schema(name)
    params = dict(schema)
    inject_sub = "sub" in (schema.get("properties") or {})
    if inject_sub:  # идентичность модели не видна: её подставляет код
        params["properties"] = {k: v for k, v in schema["properties"].items() if k != "sub"}
        if "sub" in (params.get("required") or []):
            params["required"] = [r for r in params["required"] if r != "sub"]

    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        started = time.monotonic()
        try:
            args = json.loads(input_json) if input_json and input_json.strip() else {}
        except (TypeError, ValueError):
            _log_tool(session, name, {"raw": input_json}, "bad_arguments", started)
            return json.dumps(
                {"error": "bad_arguments", "detail": "аргументы — не JSON; повтори вызов по схеме"},
                ensure_ascii=False,
            )
        if inject_sub and "sub" not in args:
            args["sub"] = session.user.sub
        session.note(name)
        try:
            result = await session.pool.call(session.pool.resource_of(name), name, args)
        except MCPError as e:
            data = e.data if isinstance(e.data, dict) else {}
            reason = getattr(e, "message", "tool_failed")[:60]
            _log_tool(session, name, args, reason, started)
            return json.dumps({"error": reason, **data}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001 — сбой сервера становится данными для модели
            _log_tool(session, name, args, "tool_failed", started)
            return json.dumps({"error": "tool_failed", "detail": str(e)[:160]}, ensure_ascii=False)

        if name.startswith("prepare_") and result.get("ok"):
            card = cards.create(session, result["action"], result["details"], result["view"])
            _log_tool(session, name, args, "prepared", started)
            _log_prepare(session, result["action"], result["details"], card.pending_id)
            return json.dumps({
                "card_created": True,
                "card": card.public_view(config.CARD_TTL_SEC),
                "next": "перескажи карточку и спроси подтверждение «да/нет»; execute_action не вызывай",
            }, ensure_ascii=False)
        _log_tool(session, name, args, "ok", started)
        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=session.pool.tool_description(name),
        params_json_schema=params,
        on_invoke_tool=invoke,
    )


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Собрать обёртки для перечня имён (какие имена давать — решает код)."""
    known = set(session.pool.tool_names())
    # execute_action не должен попадать в набор инструментов модели никогда
    return [wrap_mcp_tool(session, n) for n in names if n in known and n != "execute_action"]
