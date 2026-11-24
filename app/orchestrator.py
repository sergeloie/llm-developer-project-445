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

from app import audit, cards, editor as editor_mod, guardrail_client, idempotency, llm, planner, workers
from app.sessions import pep_of
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


def _audit(session, what: str, outcome: str, **fields) -> None:
    """Запись в append-only журнал: сбой логгера не роняет ответ абоненту."""
    try:
        audit.append(
            session.user.sub, what, outcome,
            trace_id=session.trace_id or None,
            session_id=session.session_id,
            **fields,
        )
    except Exception:  # noqa: BLE001
        pass


def _answer_kind(message: str) -> str:
    """Короткий ответ по карточке; подстроки вроде «угу» в «услугу» не считаем."""
    text = message.strip().casefold().strip("!.,")
    if text in {"да", "yes", "y", "ага", "давай", "конечно", "подтверждаю", "угу"}:
        return "yes"
    if text in {"нет", "no", "n", "не надо", "отмена", "отменить", "стоп"}:
        return "no"
    if text in {"оператор", "operator", "человека", "менеджера"}:
        return "operator"
    return "topic"


UNSAFE_INPUT_REPLY = "Не могу выполнить эту просьбу: похоже на попытку изменить мои правила работы."
UNSAFE_OUTPUT_REPLY = "Ответ заблокирован проверкой безопасности — попробуйте переформулировать вопрос."
UNSAFE_CARD_REPLY = "Карточка не найдена или истекла."


async def _guard_input(session, message: str) -> bool:
    """Проверка реплики до LLM. False — ход не начинается."""
    verdict = await guardrail_client.check(message, mode="input")
    if not verdict.available:
        session.guard_state["ok"] = False
        _audit(session, "guardrail", "guardrail_degraded", detail="input")
        return True  # чтение продолжается, actions закроет политика
    session.guard_state["ok"] = True
    if verdict.safe:
        return True
    _audit(session, "guardrail", "guardrail_unsafe", detail=verdict.category)
    session.note(f"guardrail:unsafe:{verdict.category}")
    return False


async def _guard_output(session, text: str, card=None) -> tuple[bool, str]:
    """Проверка ответа/карточки до показа; unsafe-карточку уничтожаем."""
    verdict = await guardrail_client.check(text, mode="output")
    if not verdict.available:
        session.guard_state["ok"] = False
        _audit(session, "guardrail", "guardrail_degraded", detail="output")
        return True, text
    if verdict.safe:
        return True, text
    _audit(session, "guardrail", "guardrail_unsafe", detail=verdict.category)
    session.note(f"guardrail:unsafe:{verdict.category}")
    if card is not None:
        cards.settle(session, card, "denied")
        return False, UNSAFE_CARD_REPLY
    return False, UNSAFE_OUTPUT_REPLY


async def _emit_reply(session, emit: Emit, text: str, sources=None) -> None:
    ok, shown = await _guard_output(session, text)
    await emit({"type": "reply", "text": shown, "sources": (sources or []) if ok else []})


def _before_after(card, result: dict) -> tuple[str | None, str | None]:
    view = card.view or {}
    before = (
        result.get("before") or view.get("с") or view.get("было")
        or view.get("телефон_было") or view.get("почта_было")
    )
    after = (
        result.get("after") or card.details.get("tariff") or view.get("на")
        or view.get("станет") or view.get("телефон_станет") or view.get("почта_станет")
    )
    return before, after


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
        session.done_card = card  # повтор «да» узнаёт карточку и не идёт в LLM
        _audit(session, card.action, "duplicate_suppressed",
               args=card.details, idempotency_key=key, detail="повтор не исполнялся")
        return "Эта операция уже исполнена — повтор не выполнялся."

    result = stored.result or {}
    outcome = result.get("outcome") or result.get("error") or ""
    if outcome in {"executed", "duplicate", "toggled", "updated"}:
        cards.settle(session, card, "confirmed")
        session.done_card = card
        if outcome == "duplicate":
            _audit(session, card.action, "duplicate_suppressed",
                   args=card.details, idempotency_key=key, detail="дубль биллинга")
            return "Эта операция уже исполнена — повтор не выполнялся."
        before, after = _before_after(card, result)
        _audit(session, card.action, "executed", args=card.details,
               before=before, after=after, idempotency_key=key)
        return "Готово: " + _describe(card.action, result, card)
    if outcome == "daily_limit":
        cards.settle(session, card, "denied")
        _audit(session, card.action, "server_limit", args=card.details,
               idempotency_key=key, detail="daily_limit")
        return "Дневной лимит смен тарифа исчерпан — попробуйте завтра."
    if outcome in _business_codes():
        cards.settle(session, card, "denied")
        _audit(session, card.action, "rejected_by_contract", args=card.details,
               detail=str(result.get("detail") or outcome))
        return "Действие невозможно: " + str(result.get("detail") or outcome)
    # инфраструктурный сбой: карточку оставляем живой, ключ не занят
    _audit(session, card.action, "error", args=card.details, detail=str(outcome)[:120])
    return "Не удалось выполнить действие, попробуйте ещё раз."


def _business_codes() -> set[str]:
    from actions.contracts import BUSINESS_RULE_CODES

    return set(BUSINESS_RULE_CODES) | {"unknown_action", "bad_details"}


DENIAL_REPLIES = {
    "confirmation_expired": "Карточка устарела — повторите, пожалуйста, запрос.",
    "session_limit": "Лимит смен тарифа за сессию исчерпан.",
    "card_not_prepared": "Подтверждать нечего — повторите запрос.",
    "confirmation_missing": "Подтверждение не найдено — повторите запрос.",
    "confirmation_mismatch": "Подтверждение не совпало с карточкой — повторите запрос.",
    "pdp_unavailable": "Сервис подтверждений недоступен, попробуйте позже.",
    "guard_not_ok": "Не работает проверка безопасности — могу позвать оператора.",
    "no_write_scope": "У вас нет права менять тариф.",
    "role_has_no_write": "Ваша роль не позволяет менять тариф.",
    "identity_in_args": "Действие запрещено: идентичность уже взята из токена.",
}


def _deny_card(session, card, reason: str) -> str:
    state = "expired" if reason == "confirmation_expired" else "denied"
    cards.settle(session, card, state)
    _audit(session, card.action, state, args={"pending_id": card.pending_id}, detail=reason)
    return DENIAL_REPLIES.get(reason, "Действие запрещено политикой.")


async def decide(session, card, answer: str) -> str:
    """Кнопка карточки: «да» проходит ConfirmCard и Execute в PDP."""
    verdict = _answer_kind(answer)
    if verdict == "yes":
        pep = pep_of(session)
        confirmed = await pep.confirm(card)
        if not confirmed.allowed:
            return _deny_card(session, card, confirmed.reason)
        allowed = await pep.execute(card)
        if not allowed.allowed:
            return _deny_card(session, card, allowed.reason)
        return await execute_pending(session, card)
    if verdict == "no":
        cards.settle(session, card, "rejected")
        _audit(session, card.action, "rejected_by_user",
               args={"action": card.action, "pending_id": card.pending_id})
        return "Отменяю: действие не выполнено."
    if verdict == "operator":
        text = await _escalate(session, answer)
        cards.settle(session, card, "escalated")
        _audit(session, card.action, "escalated",
               args={"action": card.action, "pending_id": card.pending_id}, detail=text[:80])
        return text
    return "Не расслышал ответа: скажите «да», «нет» или «оператор»."


async def run_turn(session, message: str, emit: Emit) -> None:
    """Один ход: SSE-события plan → card|reply."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"

    if not await _guard_input(session, message):
        await _emit_reply(session, emit, UNSAFE_INPUT_REPLY)
        _persist(session)
        return

    active = cards.active(session)
    if active is None:
        active = getattr(session, "done_card", None)  # повтор «да» после исполнения
    if active is not None:
        verdict = _answer_kind(message)
        if verdict != "topic":  # «да», «нет», «оператор» — карточку обслуживает код
            reply = await decide(session, active, message)
            session.note(f"card:{verdict}")
            await _emit_reply(session, emit, reply)
            _persist(session)
            return
        old_card_id = active.pending_id
    else:
        old_card_id = ""
        if _answer_kind(message) == "yes":  # «да» без карточки — не исполняем
            session.note("card:yes_without_card")
            await _emit_reply(session, emit, DENIAL_REPLIES["card_not_prepared"])
            _persist(session)
            return

    if session.planner is None:
        session.planner = planner.build_planner()
    plan, history = await planner.next_plan(
        session.planner, session.thread, message, trace_id=session.trace_id
    )
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
        await _emit_reply(session, emit, gated.question)
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
        card_text = " ".join(str(v) for v in (fresh.view or {}).values() if not isinstance(v, dict))
        ok, shown = await _guard_output(
            session, f"Карточка {fresh.action}: {card_text}", card=fresh
        )
        if not ok:
            await emit({"type": "reply", "text": shown, "sources": []})
        else:
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
    await _emit_reply(session, emit, answer["report"], sources)
    _persist(session)

# Аудит, PEP/PDP и guardrail подключены: вход проверяется до LLM, выход —
# до показа; при недоступном guardrail чтение продолжается, действия закрыты.
