"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента; оркестратор управляет запуском агентов и
собирает события хода для фронтенда. Модель предлагает план, код решает,
какие шаги исполнять.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

from app import editor as editor_mod
from app import llm, planner, workers
from core import db

Emit = Callable[[dict], Awaitable[None]]


def _sources(tools: list[dict]) -> list[str]:
    """Источники базы знаний из результатов инструментов агентов."""
    found: list[str] = []
    for call in tools:
        output = call.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except (TypeError, ValueError):
                continue
        if not isinstance(output, dict):
            continue
        for hit in output.get("results") or []:
            if isinstance(hit, dict) and hit.get("source"):
                found.append(hit["source"])
        for source in output.get("sources") or []:
            if isinstance(source, str):
                found.append(source)
    return found


def _dropped(before: list[str], after: list[str]) -> list[dict]:
    """Шаги, которые гейт или код не пустили в исполнение."""
    missing = Counter(before) - Counter(after)
    result = []
    for name in missing.elements():
        known = name in planner.READ_AGENTS or name in planner.ACTION_AGENTS
        result.append({
            "agent": name,
            "why": "no_worker_for_agent" if known else "unknown_agent",
        })
    return result


async def run_turn(session, message: str, emit: Emit) -> None:
    """Один ход: SSE-события plan → reply, шаги и источники в трейсе."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, history = await planner.next_plan(session.planner, session.thread, message)
    session.thread = history

    proposed = [step.agent for step in plan.steps]
    gated = planner.gate(plan, session=session)
    kept = [step.agent for step in gated.steps]
    dropped = _dropped(proposed, kept)
    for item in dropped:
        if item["why"] == "no_worker_for_agent":
            session.note(f"gate:dropped:{item['agent']}:{item['why']}")

    session.note(f"plan:{','.join(kept) or 'empty'}")
    await emit({
        "type": "plan",
        "agents": kept,
        "steps": [
            {"agent": s.agent, "task": s.task, "why": s.why, "action": s.action}
            for s in gated.steps
        ],
        "dropped": dropped,
    })

    if gated.question and not gated.steps:
        session.note("reply:question")
        await emit({"type": "reply", "text": gated.question, "sources": []})
    else:
        built = workers.build_workers(session, tuple(dict.fromkeys(kept)))
        runs: list[dict] = []
        for step in gated.steps:
            agent = built.get(step.agent)
            if agent is None:
                continue  # гейт уже оставил причину в трейсе
            task = f"Вопрос абонента: {message}\nЗадача: {step.task}"
            if step.why:
                task += f"\nКонтекст: {step.why}"
            outcome = await llm.run_agent(agent, task, trace_id=session.trace_id)
            sources = _sources(outcome["tools"])
            runs.append({
                "agent": step.agent,
                "report": outcome["report"],
                "sources": sources,
                "tools": outcome["tools"],
                "seconds": outcome["seconds"],
            })
            session.note(f"{step.agent}:tools={len(outcome['tools'])}")

        editor = editor_mod.build_editor(session)
        answer = await llm.run_agent(
            editor, editor_mod.editor_task(message, runs), trace_id=session.trace_id
        )
        sources = sorted({source for run in runs for source in run["sources"]})
        session.note("reply:editor")
        await emit({"type": "reply", "text": answer["report"], "sources": sources})

    # История планировщика живёт между ходами и в базе сессии.
    try:
        db.execute(
            "update sessions set thread = %s where session_id = %s",
            (json.dumps(session.thread, ensure_ascii=False), session.session_id),
        )
    except Exception:  # noqa: BLE001 — сбой журнала не роняет ответ абоненту
        pass

# TODO(контракты) — карточка действия:
#   - в начале хода: cards.active(session) → cards.classify_answer(message):
#     «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
#     «оператор» → escalate, смена темы — обычный ход с живой карточкой;
#   - после агентов: свежая карточка → событие {"type": "card"} и ход
#     завершается (ждём подтверждение);
#   - execute_pending(session, card): settle("confirmed"), ключ
#     f"{action}:{pending_id}", idempotency.execute → вызов MCP execute_action
#     → человекочитаемый ответ (executed / duplicate / daily_limit / отказ).
#
# TODO(аудит) — аудит: audit.append на все исходы пути действия.
# TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
#   человекочитаемый отказ по reason.
# TODO(guardrail) — guardrail: input/output проверки, fail-closed для действий.
