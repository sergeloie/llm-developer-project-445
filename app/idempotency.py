"""Идемпотентность исполнения на стороне оркестратора.

Один ключ — одна операция. Неудачный исход ключ не занимает: сбой не должен
запирать повторную попытку. Для смены тарифа дедупликация есть ещё и в
биллинге — два независимых барьера.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass

from core import db


@dataclass
class StoredResult:
    duplicate: bool
    result: dict


BAD_OUTCOMES = {
    "error", "failed", "not_found", "daily_limit", "same_tariff",
    "unknown_tariff", "tariff_not_available", "unknown_service",
    "service_already_on", "service_already_off", "same_contacts",
    "nothing_to_update", "unknown_action", "bad_details",
}


def _ok(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    return result.get("outcome") not in BAD_OUTCOMES


async def execute(key: str, sub: str, action: str, fn) -> StoredResult:
    """Исполнить fn один раз для ключа; повтор вернёт сохранённый результат."""
    row = db.fetchone("SELECT result FROM idempotency WHERE key = %s", (key,))
    if row is not None:
        stored = row["result"] if isinstance(row["result"], dict) else {}
        return StoredResult(duplicate=True, result=stored)

    result = fn()
    if inspect.isawaitable(result):
        result = await result
    if _ok(result):
        db.execute(
            "INSERT INTO idempotency(key, sub, action, result) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key) DO NOTHING",
            (key, sub, action, json.dumps(result, ensure_ascii=False)),
        )
    return StoredResult(duplicate=False, result=result if isinstance(result, dict) else {})
