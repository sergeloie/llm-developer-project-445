"""Клиент guardrail-сервиса: вердикт вероятностный, решения — за кодом.

Недоступность сервиса — не исключение: Verdict(safe=True, available=False).
Чтение продолжается, а флаг guard_ok сессии становится false — политика
закрывает действия (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Verdict:
    safe: bool
    category: str = ""
    available: bool = True
    engine: str = "rule"


async def check(text: str, mode: str = "input") -> Verdict:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.post(
                f"{config.GUARDRAIL_URL}/check",
                json={"text": text, "mode": mode},
            )
        if response.status_code != 200:
            return Verdict(safe=True, available=False)
        body = response.json()
    except Exception:  # noqa: BLE001 — деградация, не падение хода
        return Verdict(safe=True, available=False)
    return Verdict(
        safe=body.get("verdict") == "safe",
        category=str(body.get("category") or ""),
        available=True,
        engine=str(body.get("engine") or "rule"),
    )
