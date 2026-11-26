"""Контракты действий: схемы, нормализация, таксономия ошибок."""
from __future__ import annotations

import re

BUSINESS_RULE_CODES = frozenset({
    "same_tariff",
    "tariff_not_available",
    "service_already_on",
    "service_already_off",
    "same_contacts",
    "nothing_to_update",
})


class ActionError(Exception):
    """Отказ действия: код, человекочитаемое сообщение и подсказки модели."""

    def __init__(self, code: str, message: str, **hints) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints

    def to_tool_result(self) -> dict:
        return {"error": self.code, "detail": self.message, **self.hints}


def _names(catalog) -> list[str]:
    return [
        item["name"] if isinstance(item, dict) else str(item)
        for item in (catalog or [])
    ]


def _clean(raw) -> str:
    return str(raw or "").strip().strip("\"'«»").strip()


# Служебные слова, которые модель добавляет к названию: «на игровой тариф»,
# «услуга Музыка». Их снимаем до сравнения с каталогом.
TARIFF_NOISE = frozenset({"на", "тариф", "тарифа", "тарифу", "тарифом", "тарифе", "тарифы"})
SERVICE_NOISE = frozenset({
    "услуга", "услугу", "услуги", "услугой", "услуге", "услуг",
    "подключить", "подключи", "отключить", "отключи",
    "включить", "включи", "выключить", "выключи",
})


def _match(raw, available: list[str], noise: frozenset[str]) -> str | None:
    wanted = _clean(raw).casefold()
    for name in available:
        if wanted == name.casefold():
            return name
    words = [word for word in re.findall(r"[\w-]+", wanted) if word not in noise]
    candidate = " ".join(words)
    if candidate:
        for name in available:
            if candidate == name.casefold():
                return name
    return None


def normalize_tariff(raw, catalog) -> str:
    """«как расслышали» → точное название тарифа из каталога."""
    available = _names(catalog)
    matched = _match(raw, available, TARIFF_NOISE)
    if matched is not None:
        return matched
    raise ActionError(
        "unknown_tariff",
        f"Тариф {_clean(raw) or '(не указан)'} неизвестен",
        available=available,
    )


def normalize_service(raw, catalog) -> str:
    available = _names(catalog)
    matched = _match(raw, available, SERVICE_NOISE)
    if matched is not None:
        return matched
    raise ActionError(
        "unknown_service",
        f"Услуга {_clean(raw) or '(не указана)'} неизвестна",
        available=available,
    )


def normalize_phone(raw) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    raise ActionError(
        "bad_phone",
        "Номер телефона должен содержать 11 цифр",
        example="+7 900 111-22-33",
    )


def normalize_email(raw) -> str:
    email = _clean(raw).lower()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return email
    raise ActionError(
        "bad_email",
        "Почта указана некорректно",
        example="anna@example.ru",
    )
