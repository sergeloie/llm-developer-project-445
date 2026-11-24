"""Планировщик: план запусков доменных агентов до всякого исполнения.

План — структурированный выход LLM, но исполнению он не принадлежит:
гейт оркестратора оставляет только шаги известных агентов. Действия
(смена тарифа, услуги, контакты, тикет) идут через карточку подтверждения
и политику прав, а не напрямую.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app import llm

READ_AGENTS = ("data", "kb")
ACTION_AGENTS = ("tariff", "tickets")


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent: str = Field(alias="agentName")
    task: str = Field(alias="job")
    why: str = ""
    action: bool = False


class Plan(BaseModel):
    steps: list[Step] = Field(default_factory=list)
    question: str | None = None


PLANNER_PROMPT = """Ты планировщик агентного чата мобильного оператора. Ты НЕ отвечаешь абоненту:
твоя задача — составить план из шагов для доменных агентов. Отвечай только по-русски.

Каталог агентов (agentName — строго одно из этих имён, без дополнений и кавычек):
- data — факты абонента: профиль, текущий тариф, каталог тарифов, история списаний.
  Задачи: «какой у меня тариф», «что входит в тариф», «сколько стоит», «покажи списания»,
  «какие у меня услуги», «почему списание 440» (только цифры и факты списания).
- kb — правила и база знаний: условия тарифов, правила списаний, как сменить тариф,
  роуминг, услуги, контакты. Всегда нужен для вопросов «почему», «как», «какие правила»,
  «что будет, если», а также вместе с data для «почему списание 440».
- tariff — просьба изменить тариф, услугу или контакты (смена тарифа, подключение услуги).
  Ставь action=true. Изменения исполняются только после карточки подтверждения.
- tickets — жалоба, претензия, просьба позвонить оператору или открыть тикет.
  Ставь action=true.

Правила:
1. Поля шага: agentName, job, why, action. job — краткая задача по-русски, без выдуманных чисел.
   why — одно предложение, зачем нужен агент. action — true только для tariff и tickets.
   У агентов data и kb ставь action=false всегда, даже если вопрос звучит как действие.
2. Вопрос о данных абонента — один шаг data. Вопрос о правилах — один шаг kb.
   Цена услуги, баланс, сумма или дата списания — это данные, нужен шаг data;
   если одновременно спросили про правила — добавь шаг kb.
   «Почему списание 440» — два шага: data (найти списание) и kb (объяснить правила).
3. Названия тарифов пиши строчными буквами, без кавычек: базовый, игровой, семейный, про.
4. Если вопрос не относится к оператору — один шаг tickets с action=true.
5. Если в сообщении мало данных для плана, верни question с уточняющим вопросом и пустой steps.

Пример. Вопрос: «какой у меня тариф и почему списание 440?»
Ответ: {"steps":[{"agentName":"data","job":"Назвать текущий тариф и найти списание 440","why":"Нужны факты абонента","action":false},{"agentName":"kb","job":"Объяснить правила списаний и платы за услуги","why":"Нужны правила из базы знаний","action":false}],"question":null}
"""


def build_planner():
    """Планировщик со структурированным выходом Plan."""
    return llm.build_agent("planner", PLANNER_PROMPT, output_type=Plan)


def gate(plan: Plan, session=None) -> Plan:
    """Гейт плана: неизвестные агенты отбрасываются, read не исполняют действия.

    Причина отброшенного шага пишется в трейс сессии, чтобы было видно,
    что модель предложила, а код не допустил до исполнения.
    """
    kept: list[Step] = []
    for step in plan.steps:
        name = (step.agent or "").strip().lower()
        if name in READ_AGENTS:
            kept.append(step.model_copy(update={"action": False, "agent": name}))
        elif name in ACTION_AGENTS:
            kept.append(step.model_copy(update={"agent": name}))
        else:
            reason = f"gate:dropped:{step.agent}:unknown_agent"
            if session is not None:
                session.note(reason)
            else:
                import logging

                logging.getLogger(__name__).info(reason)
    return Plan(steps=kept, question=plan.question)


async def next_plan(planner, history, message, *, trace_id: str = "") -> tuple[Plan, list]:
    """Один вызов планировщика по истории диалога; возвращает историю хода."""
    outcome = await llm.run_agent(planner, message, trace_id=trace_id, history=history)
    plan = outcome["output"]
    if not isinstance(plan, Plan):
        plan = Plan.model_validate(plan)
    return plan, outcome["history"]
