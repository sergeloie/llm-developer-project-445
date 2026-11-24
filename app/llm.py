"""Сборка агентов на модели из .env + учёт LLM-вызовов.

Шаг 2 (агенты): build_agent/run_agent. Структурированный выход подключается
нестрогой схемой: строгий JSON-режим требует от малых моделей слишком
много, план падает на валидации.

TODO(аудит): в run_agent — INSERT в llm_calls: trace_id, имя агента,
  модель, токены (result.context_wrapper.usage), латентность.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""
from __future__ import annotations

import time

from agents import Agent, AgentOutputSchema, Runner

from core import config


def build_agent(name, instructions, tools=None, output_type=None):
    """Агент с фиксированным набором инструментов (allowlist агента)."""
    kwargs = dict(
        name=name,
        instructions=instructions,
        model=config.MODEL_NAME or config.setup_model(),
        tools=list(tools or []),
    )
    if output_type is not None:
        kwargs["output_type"] = AgentOutputSchema(output_type, strict_json_schema=False)
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


async def run_agent(agent, task, *, trace_id="") -> dict:
    """Один Runner.run; возвращает доклад и разбор вызовов инструментов."""
    started = time.monotonic()
    result = await Runner.run(agent, task)

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
        "tools": list(calls.values()),
        "seconds": round(time.monotonic() - started, 2),
    }
