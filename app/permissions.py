"""Права: инженерная спецификация вместо формулировки в промпте.

Allowlist строится из проверенных claims токена: роль задаёт набор
операций, scope его сужает. Два барьера: visible_tools — до сборки агента
(модель не видит запрещённого), admit — в момент вызова инструмента.
"""
from __future__ import annotations

from dataclasses import dataclass

READ_OPS = frozenset({
    "get_my_profile", "get_my_tariff", "list_tariffs",
    "list_my_charges", "search_kb",
})
WRITE_OPS = frozenset({
    "prepare_tariff_change", "prepare_toggle_service", "prepare_contacts_update",
})
OPERATOR_OPS = frozenset({"get_subscriber_overview"})
ALWAYS_OPS = frozenset({"create_ticket"})
EXECUTE_OP = "execute_action"

OPERATIONS = READ_OPS | WRITE_OPS | OPERATOR_OPS | ALWAYS_OPS | {EXECUTE_OP}

# Ключи идентичности субъекта: их нельзя принимать от модели — объект
# операции определяется токеном. phone — новое значение контакта, не id.
# subject — тема тикета в контракте create_ticket, а не идентификатор:
# в список ключей идентичности он не входит.
IDENTITY_KEYS = frozenset({
    "sub", "user_id", "msisdn", "subscriber_id", "client_id",
    "owner_id", "account_id", "user",
})
# Единственное легитимное исключение: оператор смотрит чужую карточку по id.
IDENTITY_ARG_ALLOWED = frozenset({"get_subscriber_overview"})


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def allowlist_from_claims(claims: dict) -> frozenset[str]:
    """Роль даёт операции, scope сужает; execute_action — никогда."""
    scopes = set(str(claims.get("scope", "") or "").split())
    role = claims.get("role", "")
    ops = set(READ_OPS) | set(ALWAYS_OPS)
    if role == "client" and "tariffs:write" in scopes:
        ops |= WRITE_OPS
    elif role == "operator":
        ops |= OPERATOR_OPS
    return frozenset(ops)


def visible_tools(allowlist, names) -> list[str]:
    """Барьер 1: модель видит только операции из allowlist сессии."""
    allowed = set(allowlist or ())
    return [name for name in names if name in allowed and name != EXECUTE_OP]


def admit(allowlist, tool_name: str, args: dict | None = None) -> Decision:
    """Барьер 2: допуск «субъект, операция, объект» в момент вызова."""
    allowed = set(allowlist or ())
    if tool_name not in allowed or tool_name == EXECUTE_OP:
        return Decision(False, f"operation_not_allowed:{tool_name}")
    for key in IDENTITY_KEYS:
        if (args or {}).get(key) is not None and tool_name not in IDENTITY_ARG_ALLOWED:
            return Decision(False, f"object_from_arguments:{key}")
    return Decision(True)
