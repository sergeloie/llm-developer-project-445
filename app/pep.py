"""PEP: оркестратор спрашивает PDP — сам решений о write-пути не принимает.

Субъект и guard_state приходят из сессии; каждая проверка несёт те же
факты, что политика видит в PDP. Недоступный сервис — fail-closed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


class Pep:
    def __init__(self, session_id: str, claims: dict | None = None, guard_state=None) -> None:
        self.session_id = session_id
        self.claims = claims or {}
        self.guard_state = guard_state if guard_state is not None else {"ok": True}
        self._opened = False

    def _subject(self) -> dict:
        return {
            "sub": self.claims.get("sub", ""),
            "role": self.claims.get("role", ""),
            "scopes": str(self.claims.get("scope", "") or "").split(),
        }

    def _input(self, card) -> dict:
        guard = self.guard_state.get("ok", True) if isinstance(self.guard_state, dict) else self.guard_state
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            "details": json.dumps(card.details or {}, ensure_ascii=False),
            "guard_ok": bool(guard),
            **self._subject(),
        }

    async def _call(self, path: str, payload: dict) -> Decision:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.post(f"{config.PDP_URL}{path}", json=payload)
            if response.status_code != 200:
                return Decision(False, "pdp_unavailable")
            body = response.json()
        except Exception:  # noqa: BLE001 — сбой PDP не открывает write-путь
            return Decision(False, "pdp_unavailable")
        return Decision(
            bool(body.get("decision") == "Allow"),
            str(body.get("reason") or ""),
        )

    async def _ensure_open(self) -> Decision | None:
        if self._opened:
            return None
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.post(
                    f"{config.PDP_URL}/session",
                    json={"session_id": self.session_id, "subject": self._subject()},
                )
            if response.status_code != 200:
                return Decision(False, "pdp_unavailable")
        except Exception:  # noqa: BLE001
            return Decision(False, "pdp_unavailable")
        self._opened = True
        return None

    async def prepare(self, card) -> Decision:
        failed = await self._ensure_open()
        if failed is not None:
            return failed
        return await self._call("/authorize", {
            "session_id": self.session_id,
            "action": "Prepare",
            "input": self._input(card),
        })

    async def confirm(self, card) -> Decision:
        failed = await self._ensure_open()
        if failed is not None:
            return failed
        return await self._call("/confirm", {
            "session_id": self.session_id,
            "input": self._input(card),
        })

    async def execute(self, card) -> Decision:
        failed = await self._ensure_open()
        if failed is not None:
            return failed
        return await self._call("/authorize", {
            "session_id": self.session_id,
            "action": "Execute",
            "input": self._input(card),
        })
