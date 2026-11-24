"""Action-MCP: инструменты действий.

    uvicorn actions.server:app --host 127.0.0.1 --port 8220

Субъект берётся из проверенного Bearer-токена вызова: в контрактах нет
аргумента sub, подменить нечего. ActionError превращается в MCPError с
кодом -32002 и data-подсказками — они доезжают до модели.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from actions import service as actions_service
from actions.contracts import ActionError
from core import config
from core.server_auth import make_mcp_server

ERROR_CODE = -32002

server = make_mcp_server(
    "actions",
    resource=config.RESOURCES["actions"],
    instructions="Подготовка действий и исполнение по коду после подтверждения.",
)


def _subject() -> str:
    token = get_access_token()
    assert token is not None, "инструмент вызван без проверенного токена"
    return token.subject or ""


def as_tool(fn):
    """ActionError → MCPError с машиночитаемыми подсказками."""

    @wraps(fn)
    async def wrapped(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ActionError as error:
            raise MCPError(code=ERROR_CODE, message=error.code, data=error.to_tool_result()) from error

    return wrapped


async def prepare_tariff_change(tariff: str) -> dict:
    """Подготовить смену тарифа: проверить тариф и вернуть карточку для подтверждения."""
    return await actions_service.prepare_tariff_change(_subject(), tariff)


async def prepare_toggle_service(service: str, enabled: bool = True) -> dict:
    """Подготовить включение или отключение услуги; вернуть карточку для подтверждения."""
    return await actions_service.prepare_toggle_service(_subject(), service, enabled)


async def prepare_contacts_update(phone: str | None = None, email: str | None = None) -> dict:
    """Подготовить смену телефона или почты; вернуть карточку для подтверждения."""
    return await actions_service.prepare_contacts_update(_subject(), phone, email)


async def execute_action(
    action: str,
    details_json: str,
    pending_id: str = "",
    idempotency_key: str = "",
) -> dict:
    """Исполнить подготовленное действие (вызывает только код после «да»)."""
    return await actions_service.execute_action(
        _subject(), action, details_json, pending_id, idempotency_key
    )


async def create_ticket(subject: str, note: str) -> dict:
    """Эскалировать обращение: создать тикет без карточки подтверждения."""
    return await actions_service.create_ticket(_subject(), subject, note)


for _fn in (
    prepare_tariff_change,
    prepare_toggle_service,
    prepare_contacts_update,
    execute_action,
    create_ticket,
):
    server.add_tool(as_tool(_fn))


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "actions"})


@asynccontextmanager
async def lifespan(app: Starlette):
    manager = server.session_manager.run()
    await manager.__aenter__()
    try:
        yield
    finally:
        await manager.__aexit__(None, None, None)


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/actions", app=server.streamable_http_app()),
    ],
    lifespan=lifespan,
)
