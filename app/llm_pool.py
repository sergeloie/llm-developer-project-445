"""Пул OpenAI-совместимых провайдеров (бонус, без внешних библиотек).

Переключение встроено на уровне клиента: SDK видит один «default client»,
агенты не пересобираются. Политика повторяет только 429, 5xx, таймаут и
сетевую ошибку; 400/401/403 — ошибка вызова, её не лечит другой провайдер.
"""
from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

from core import config


@dataclass
class Provider:
    name: str
    model: str
    client: AsyncOpenAI
    price_rub_per_1m: float


_current: contextvars.ContextVar[Provider | None] = contextvars.ContextVar(
    "llm_provider", default=None
)
_pool: "LLMPool | None" = None


def enabled() -> bool:
    return bool(config.SECONDARY_BASE_URL and config.SECONDARY_MODEL)


def current() -> Provider | None:
    return _current.get()


async def call(fn):
    """Один обход настроенных провайдеров для вызова агента."""
    if _pool is None:
        raise RuntimeError("пул LLM не инициализирован")
    return await _pool.call(fn)


def is_retriable(exc: Exception) -> bool:
    """429, 5xx, таймаут и сетевая ошибка — да; 400/401/403 — нет."""
    if isinstance(exc, (APITimeoutError, APIConnectionError, APIStatusError)):
        status = getattr(exc, "status_code", None)
        if isinstance(exc, APITimeoutError) or isinstance(exc, APIConnectionError):
            return True
        return status == 429 or (status is not None and status >= 500)
    return False


def _cooldown(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 429 or (status is not None and status >= 500)


class LLMPool:
    def __init__(self, providers: list[Provider], cooldown_sec: int) -> None:
        self.providers = providers
        self.cooldown_sec = cooldown_sec
        self.disabled_until: dict[str, float] = {}

    def _next(self, tried: set[str]) -> Provider | None:
        now = time.monotonic()
        return next(
            (
                p for p in self.providers
                if p.name not in tried and self.disabled_until.get(p.name, 0) <= now
            ),
            None,
        )

    async def call(self, fn):
        """Один обход пула; fn(client, model) вызывается заново при переключении."""
        last_error: Exception | None = None
        tried: set[str] = set()
        for _ in self.providers:
            provider = self._next(tried)
            if provider is None:
                break
            tried.add(provider.name)
            token = _current.set(provider)
            try:
                result = await fn(provider.client, provider.model)
                return result, provider
            except Exception as error:  # noqa: BLE001 — политика решает, повторять ли
                last_error = error
                if not is_retriable(error):
                    raise
                if _cooldown(error):
                    self.disabled_until[provider.name] = time.monotonic() + self.cooldown_sec
            finally:
                _current.reset(token)
        if last_error is not None:
            raise last_error
        raise RuntimeError("нет доступного провайдера LLM")


class _Completions:
    async def create(self, **kwargs):
        provider = _current.get()
        if provider is None:
            raise RuntimeError("вызов вне контекста llm_pool.call()")
        kwargs["model"] = provider.model  # у второго провайдера своя модель
        return await provider.client.chat.completions.create(**kwargs)


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class PoolClient:
    """Один клиент для SDK; маршрутизирует вызов в активного провайдера."""

    def __init__(self) -> None:
        self.chat = _Chat()
        self.base_url = config.STAND_URL  # только для конфигурации трассировки SDK
        self.api_key = "pool"


def setup_pool(*, primary_client: AsyncOpenAI, primary_model: str) -> PoolClient:
    """Собрать пул и вернуть клиента, которого SDK будет считать основным."""
    global _pool
    providers = [
        Provider("primary", primary_model, primary_client, config.LLM_PRICE_RUB_PER_1M),
    ]
    if enabled():
        secondary = AsyncOpenAI(
            base_url=config.SECONDARY_BASE_URL,
            api_key=config.SECONDARY_API_KEY,
            http_client=httpx.AsyncClient(trust_env=False, timeout=180),
        )
        providers.append(
            Provider("secondary", config.SECONDARY_MODEL, secondary,
                     config.SECONDARY_PRICE_RUB_PER_1M)
        )
    _pool = LLMPool(providers, config.FAILOVER_COOLDOWN_SEC)
    return PoolClient()
