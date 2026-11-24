"""Логика действий поверх REST-моков: предусловия, карточки, исполнение.

prepare_* ничего не исполняют — проверяют предусловия и собирают view для
карточки. execute_action вызывает только код оркестратора после «да».
"""
from __future__ import annotations

import json

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from actions.contracts import (
    ActionError,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)
from core import config

SERVICE_CATALOG = ["Музыка", "Роуминг", "Антивирус"]


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        response = await client.get(f"{config.STAND_URL}{path}", params=params)
    response.raise_for_status()
    return response.json()


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        response = await client.post(f"{config.STAND_URL}{path}", json=body)
    response.raise_for_status()
    return response.json()


async def _profile(sub: str) -> dict:
    profile = await _get("/mock/crm/subscriber", {"sub": sub})
    if profile.get("error"):
        raise ActionError("not_found", "Абонент не найден", sub=sub)
    return profile


async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Подготовка смены тарифа: валидация, предусловия, карточка."""
    profile = await _profile(sub)
    catalog = (await _get("/mock/crm/tariffs")).get("tariffs", [])
    target_name = normalize_tariff(tariff, catalog)
    if target_name == profile["tariff"]:
        raise ActionError(
            "same_tariff", "У вас уже этот тариф", current=profile["tariff"]
        )
    target = next(t for t in catalog if t["name"] == target_name)
    if not target.get("available", True):
        raise ActionError(
            "tariff_not_available", "Тариф недоступен для подключения", tariff=target_name
        )
    current = next((t for t in catalog if t["name"] == profile["tariff"]), {})
    price_before = int(current.get("price_rub", 0))
    price_after = int(target.get("price_rub", 0))
    delta = price_after - price_before
    return {
        "ok": True,
        "action": "tariff_change",
        "details": {"tariff": target_name},
        "view": {
            "что": "смена тарифа",
            "с": profile["tariff"],
            "на": target_name,
            "цена_сейчас": f"{price_before} ₽/мес",
            "цена_станет": f"{price_after} ₽/мес",
            "разница": f"{delta:+d} ₽/мес",
            "данные": f"{target.get('data_gb', '—')} ГБ",
        },
    }


async def prepare_toggle_service(sub: str, service: str, enabled: bool = True) -> dict:
    """Подготовка включения/выключения услуги."""
    profile = await _profile(sub)
    name = normalize_service(service, SERVICE_CATALOG)
    current = next(
        (s for s in profile.get("services", []) if s["service"] == name), None
    )
    if current is None:
        raise ActionError("unknown_service", "Услуга не подключена", available=SERVICE_CATALOG)
    now = bool(current.get("enabled"))
    if now == bool(enabled):
        code = "service_already_on" if now else "service_already_off"
        raise ActionError(code, f"Услуга уже {'включена' if now else 'выключена'}", service=name)
    price = current.get("price_rub")
    return {
        "ok": True,
        "action": "service_toggle",
        "details": {"service": name, "enabled": bool(enabled)},
        "view": {
            "что": "включение услуги" if enabled else "отключение услуги",
            "услуга": name,
            "было": "включена" if now else "выключена",
            "станет": "включена" if enabled else "выключена",
            "цена": f"{price} ₽/мес" if price is not None else "—",
        },
    }


async def prepare_contacts_update(
    sub: str, phone: str | None = None, email: str | None = None
) -> dict:
    """Подготовка смены контактов: нормализация и предупреждения."""
    profile = await _profile(sub)
    changes: dict = {}
    view: dict = {"что": "обновление контактов"}
    if phone is not None and str(phone).strip():
        changes["phone"] = normalize_phone(phone)
        view["телефон_было"] = profile.get("phone")
        view["телефон_станет"] = changes["phone"]
    if email is not None and str(email).strip():
        changes["email"] = normalize_email(email)
        view["почта_было"] = profile.get("email")
        view["почта_станет"] = changes["email"]
    if not changes:
        raise ActionError("nothing_to_update", "Нечего обновлять", example="+7 900 111-22-33")
    if all(profile.get(key) == value for key, value in changes.items()):
        raise ActionError("same_contacts", "Контакты уже такие", current=changes)
    return {
        "ok": True,
        "action": "contacts_update",
        "details": changes,
        "view": view,
    }


async def execute_action(
    sub: str,
    action: str,
    details_json: str,
    pending_id: str = "",
    idempotency_key: str = "",
) -> dict:
    """Исполнение после подтверждения: граница вызова — только код."""
    try:
        details = json.loads(details_json or "{}")
    except (TypeError, ValueError) as error:
        raise ActionError("bad_details", "Детали действия не JSON", detail=str(error)[:80])
    if not isinstance(details, dict):
        raise ActionError("bad_details", "Детали действия должны быть объектом")

    key = idempotency_key or f"{action}:{pending_id}"
    if action == "tariff_change":
        return await _post("/mock/billing/tariff-change", {
            "sub": sub, "tariff": details.get("tariff", ""), "idempotency_key": key,
        })
    if action == "service_toggle":
        return await _post("/mock/crm/toggle-service", {
            "sub": sub,
            "service": details.get("service", ""),
            "enabled": bool(details.get("enabled", True)),
        })
    if action == "contacts_update":
        return await _post("/mock/crm/contacts", {"sub": sub, **details})
    raise ActionError(
        "unknown_action",
        "Неизвестное действие",
        available=["tariff_change", "service_toggle", "contacts_update"],
    )


async def create_ticket(sub: str, subject: str, note: str) -> dict:
    """Эскалация: тикет без карточки подтверждения."""
    return await _post("/mock/tickets/create", {
        "sub": sub, "subject": subject or "Обращение абонента", "note": note or "",
    })
