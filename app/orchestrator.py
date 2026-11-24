"""Оркестратор хода: план → гейт → доменные агенты → редактор → SSE.

Ход — один запрос абонента. Модель предлагает план, код решает, какие шаги
исполнять. Живая карточка подтверждения обслуживается кодом до всякого
планирования: «да» ведёт к исполнению, «нет» — к отмене, «оператор» —
к эскалации, смена темы — к обычному ходу с сохранением карточки.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable

from app import cards, editor as editor_mod, idempotency, llm, planner, workers
from core import config, db

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


def _persist(session) -> None:
    try:
        db.execute(
            "update sessions set thread = %s where session_id = %s",
            (json.dumps(session.thread, ensure_ascii=False), session.session_id),
        )
    except Exception:  # noqa: BLE001 — сбой журнала не роняет ответ абоненту
        pass


def _describe(action: str, result: dict, card) -> str:
    if action == "tariff_change":
        before = result.get("before") or (card.view or {}).get("с")
        after = result.get("after") or card.details.get("tariff")
        return f"тариф изменён с {before} на {after}"
    if action == "service_toggle":
        details = card.details
        verb = "включена" if details.get("enabled") else "выключена"
        return f"услуга {details.get('service', '')} {verb}"
    if action == "contacts_update":
        return "контакты обновлены"
    return "действие выполнено"


async def _escalate(session, message: str) -> str:
    ticket = await session.pool.call("actions", "create_ticket", {
        "subject": "Эскалация из чата", "note": message,
    })
    ticket_id = ticket.get("ticket_id", "") if isinstance(ticket, dict) else ""
    return f"Передаю обращение оператору, тикет {ticket_id}." if ticket_id else "Передаю обращение оператору."


async def execute_pending(session, card) -> str:
    """Исполнить карточку после «да»: один ключ, один эффект."""
    key = f"{card.action}:{card.pending_id}"
    stored = await idempotency.execute(
        key,
        session.user.sub,
        card.action,
        lambda: session.pool.call("actions", "execute_action", {
            "action": card.action,
            "details_json": json.dumps(card.details, ensure_ascii=False),
            "pending_id": card.pending_id,
            "idempotency_key": key,
        }),
    )
    if stored.duplicate:
        cards.settle(session, card, "confirmed")
        return "Эта операция уже исполнена — повтор не выполнялся."

    result = stored.result or {}
    outcome = result.get("outcome") or result.get("error") or ""
    if outcome in {"executed", "duplicate"}:
        cards.settle(session, card, "confirmed")
        if outcome == "duplicate":
            return "Эта операция уже исполнена — повтор не выполнялся."
        return "Готово: " + _describe(card.action, result, card)
    if outcome in {"toggled", "updated"}:
        cards.settle(session, card, "confirmed")
        return "Готово: " + _describe(card.action, result, card)
    if outcome == "daily_limit":
        cards.settle(session, card, "denied")
        return "Дневной лимит смен тарифа исчерпан — попробуйте завтра."
    if outcome in _business_codes():
        cards.settle(session, card, "denied")
        return "Действие невозможно: " + str(result.get("detail") or outcome)
    # инфраструктурный сбой: карточку оставляем живой, ключ не занят
    return "Не удалось выполнить действие, попробуйте ещё раз."


def _business_codes() -> set[str]:
    from actions.contracts import BUSINESS_RULE_CODES

    return set(BUSINESS_RULE_CODES) | {"unknown_action", "bad_details"}


async def decide(session, card, answer: str) -> str:
    """Кнопка карточки или текст ответа: решение принимает код, не LLM."""
    verdict = cards.classify_answer(answer)
    if verdict == "yes":
        return await execute_pending(session, card)
    if verdict == "no":
        cards.settle(session, card, "rejected")
        return "Отменяю: действие не выполнено."
    if verdict == "operator":
        text = await _escalate(session, answer)
        cards.settle(session, card, "escalated")
        return text
    return "Не расслышал ответа: скажите «да», «нет» или «оператор»."


async def run_turn(session, message: str, emit: Emit) -> None:
    """Один ход: SSE-события plan → card|reply."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    active = cards.active(session)
    if active is not None:
        verdict = cards.classify_answer(message)
        if verdict != "topic":  # «да», «нет», «оператор» — карточку обслуживает код
            reply = await decide(session, active, message)
            session.note(f"card:{verdict}")
            await emit({"type": "reply", "text": reply, "sources": []})
            _persist(session)
            return
        old_card_id = active.pending_id
    else:
        old_card_id = ""

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
        _persist(session)
        return

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

    fresh = cards.active(session)
    if fresh is not None and fresh.pending_id != old_card_id:
        session.note("card:pending")
        await emit({
            "type": "card",
            "pending_id": fresh.pending_id,
            "card": fresh.public_view(config.CARD_TTL_SEC),
        })
        _persist(session)
        return

    editor = editor_mod.build_editor(session)
    answer = await llm.run_agent(
        editor, editor_mod.editor_task(message, runs), trace_id=session.trace_id
    )
    sources = sorted({source for run in runs for source in run["sources"]})
    session.note("reply:editor")
    await emit({"type": "reply", "text": answer["report"], "sources": sources})
    _persist(session)

# TODO(аудит) — аудит: audit.append на все исходы пути действия.
# TODO(права) — права: подтверждение и исполнение через PEP, Deny по reason.
# TODO(guardrail) — guardrail: input/output проверки, fail-closed для действий.
