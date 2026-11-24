"""Guardrail-сервис: stateless-контур проверки текста.

    uvicorn --factory guardrail.service:build_app --host 127.0.0.1 --port 8300

RULE-движок — лабораторная эвристика на детерминированных маркерах атак
стенда. GUARDRAIL_MODEL=1 включает открытый классификатор, если он
установлен; без зависимостей сервис остаётся на RULE. Недоступность —
не крах контура: оркестратор решает сам fail-closed для действий.
"""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DOWN = False

RULE_PATTERNS: dict[str, tuple[str, ...]] = {
    "jailbreak": (
        "суперпользователь", "ты теперь админ", "ты теперь администратор",
        "отключи проверки", "отключить проверки", "jailbreak", "developer mode",
    ),
    "prompt_injection": (
        "игнорируй инструкц", "игнорируй предыдущ", "ignore all previous",
        "ignore previous instructions", "забудь свои инструкции",
    ),
    "tool_hijack": (
        "смени тариф в аккаунте", "вызови инструмент", "execute_action",
        "call_tool", " вызови execute",
    ),
    "injected_instruction": (
        "немедленно переведи", "без подтверждения", "без спроса",
        "without confirmation", "immediately switch",
    ),
}


def rule_check(text: str, mode: str) -> dict:
    lowered = (text or "").lower()
    for category, markers in RULE_PATTERNS.items():
        if any(marker in lowered for marker in markers):
            return {
                "verdict": "unsafe", "category": category,
                "engine": "rule", "mode": mode,
            }
    return {"verdict": "safe", "category": "", "engine": "rule", "mode": mode}


def check_text(text: str, mode: str) -> dict:
    if os.environ.get("GUARDRAIL_MODEL") == "1":
        try:
            from guardrail.model import classify  # type: ignore

            verdict, category = classify(text, mode=mode)
            return {
                "verdict": verdict, "category": category,
                "engine": "model", "mode": mode,
            }
        except Exception:  # noqa: BLE001 — модель Optional, RULE всегда подстраховывает
            pass
    return rule_check(text, mode)


async def health(request: Request) -> JSONResponse:
    if DOWN:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "service": "guardrail"})


async def check(request: Request) -> JSONResponse:
    if DOWN:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    body = await request.json()
    mode = body.get("mode", "input")
    if mode not in ("input", "output"):
        return JSONResponse({"error": "bad_mode"}, status_code=400)
    return JSONResponse(check_text(str(body.get("text", "")), mode))


async def admin_down(request: Request) -> JSONResponse:
    global DOWN
    DOWN = True
    return JSONResponse({"ok": True, "status": "down"})


async def admin_up(request: Request) -> JSONResponse:
    global DOWN
    DOWN = False
    return JSONResponse({"ok": True, "status": "up"})


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/check", check, methods=["POST"]),
            Route("/admin/down", admin_down, methods=["POST"]),
            Route("/admin/up", admin_up, methods=["POST"]),
        ],
    )
