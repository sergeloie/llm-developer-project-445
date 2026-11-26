"""Сборка агентов на модели из .env + учёт LLM-вызовов.

Шаг 2 (агенты): build_agent/run_agent. Структурированный выход подключается
нестрогой схемой: строгий JSON-режим требует от малых моделей слишком
много, план падает на валидации.

Аудит (шаг 4): каждый запуск агента пишется в llm_calls с trace_id,
  именем, моделью, токенами и задержкой.

Бонус: запуск идёт через llm_pool.call(); в llm_calls пишутся фактический
  провайдер, его модель и стоимость по цене этой модели.
"""
from __future__ import annotations

import time

from agents import Agent, AgentOutputSchema, Runner

from app import llm_pool
from core import config, db


def build_agent(name, instructions, tools=None, output_type=None,
                tool_use_behavior=None, model_settings=None):
    """Агент с фиксированным набором инструментов (allowlist агента)."""
    kwargs = dict(
        name=name,
        instructions=instructions,
        # SDK трактует «provider/model» как префикс провайдера; реальное имя
        # подставляет llm_pool на уровне HTTP-клиента.
        model=(config.MODEL_NAME or config.setup_model()).split("/")[-1],
        tools=list(tools or []),
    )
    if output_type is not None:
        kwargs["output_type"] = AgentOutputSchema(output_type, strict_json_schema=False)
    if tool_use_behavior is not None:
        kwargs["tool_use_behavior"] = tool_use_behavior
    if model_settings is not None:
        kwargs["model_settings"] = model_settings
    return Agent(**kwargs)


def _arguments(raw_item) -> dict:
    """Аргументы tool-call'а в виде dict, независимо от dialect'а провайдера."""
    value = raw_item.get("arguments") if isinstance(raw_item, dict) else getattr(raw_item, "arguments", None)
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"raw": value}
    return value if isinstance(value, dict) else {}


async def run_agent(agent, task, *, trace_id="", history=None) -> dict:
    """Один Runner.run; возвращает доклад, структурированный выход и историю."""
    started = time.monotonic()
    inputs = [*history, {"role": "user", "content": task}] if history else task
    result, provider = await llm_pool.call(lambda *_: Runner.run(agent, inputs))
    model_used = provider.model
    provider_name = provider.name
    price = provider.price_rub_per_1m
    elapsed_ms = int((time.monotonic() - started) * 1000)

    try:
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost = round((input_tokens + output_tokens) * price / 1_000_000, 6)
        db.execute(
            "insert into llm_calls(trace_id, agent, model, input_tokens, output_tokens,"
            " latency_ms, provider, cost_rub) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                trace_id or None, agent.name, model_used, input_tokens, output_tokens,
                elapsed_ms, provider_name, cost,
            ),
        )
    except Exception:  # noqa: BLE001 — журнал не роняет агента
        pass

    calls: dict[str, dict] = {}
    for item in result.new_items:
        kind = getattr(item, "type", "")
        if kind == "tool_call_item":
            call_id = str(item.call_id or "")
            calls[call_id] = {
                "name": item.tool_name or "tool",
                "args": _arguments(item.raw_item),
                "output": "",
            }
        elif kind == "tool_call_output_item":
            call_id = str(item.call_id or "")
            entry = calls.setdefault(call_id, {"name": "tool", "args": {}, "output": ""})
            entry["output"] = item.output

    final = result.final_output
    report = final if isinstance(final, str) else str(final)
    return {
        "report": report,
        "output": final,
        "history": result.to_input_list() if history is not None else [],
        "tools": list(calls.values()),
        "seconds": round(time.monotonic() - started, 2),
        "provider": provider_name,
        "model": model_used,
    }
