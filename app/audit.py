"""Аудит: append-only журнал в Postgres — основа разбора инцидентов.

Только append и чтение: журнал не редактируется. Записываются действия,
отказы и деградации; текст реплик в журнал не попадает.
"""
from __future__ import annotations

import json

from core import db

OUTCOMES = frozenset({
    "executed",
    "duplicate_suppressed",
    "prepared",
    "rejected_by_user",
    "rejected_by_contract",
    "expired",
    "escalated",
    "denied",
    "guardrail_unsafe",
    "guardrail_degraded",
    "token_refreshed",
    "server_limit",
    "budget_exhausted",
    "error",
})


def _json(value):
    if value is None:
        return None
    # jsonb-колонка: даже скаляр before/after должен быть валидным JSON
    return json.dumps(value, ensure_ascii=False)


def append(
    who: str,
    what: str,
    outcome: str,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    args=None,
    before=None,
    after=None,
    idempotency_key: str | None = None,
    detail: str | None = None,
) -> dict:
    """Дописать событие в журнал и вернуть сохранённую запись."""
    with db.connect() as conn, conn.cursor() as cur:
        row = cur.execute(
            "insert into audit_log(trace_id, session_id, who, what, outcome,"
            " args, before, after, idempotency_key, detail)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *",
            (
                trace_id, session_id, who, what, outcome,
                _json(args), _json(before), _json(after), idempotency_key, detail,
            ),
        ).fetchone()
        conn.commit()
        cols = [d.name for d in cur.description or []]
    return dict(zip(cols, row))


def tail(n: int = 20, who: str | None = None) -> list[dict]:
    """Последние события (по возрастанию) — для быстрого разбора."""
    if who is None:
        rows = db.fetchall(
            "select * from (select * from audit_log order by id desc limit %s) t order by id",
            (n,),
        )
    else:
        rows = db.fetchall(
            "select * from (select * from audit_log where who = %s order by id desc limit %s) t"
            " order by id",
            (who, n),
        )
    return rows


def trace(trace_id: str) -> list[dict]:
    """Полная хронология одного хода: audit_log по сквозному trace_id."""
    return db.fetchall("select * from audit_log where trace_id = %s order by id", (trace_id,))
