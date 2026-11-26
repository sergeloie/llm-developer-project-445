# AI-оператор для сайта мобильного оператора связи

[![hexlet-check](https://github.com/sergeloie/llm-developer-project-445/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/sergeloie/llm-developer-project-445/actions)

Соберите агентный чат для сайта мобильного оператора. Он отвечает абоненту на вопросы о тарифе, списаниях и правилах со ссылками на базу знаний, а смену тарифа, услуг и контактов проводит через карточку подтверждения. По пути освоите оркестрацию нескольких агентов на OpenAI Agents SDK, MCP-серверы с JWT, контракты действий с идемпотентностью, журнал аудита, права через политики Dogwood и guardrail-сервис, при отказе которого действия закрываются.

Учебный проект Хекслета: https://ru.hexlet.io/programs/llm-developer
Как это должно работать: https://files.hexlet.app/a/i3355x
Отчёт приёмки контура: [ACCEPTANCE.md](ACCEPTANCE.md)

## Стек

- Python 3.12, FastAPI, OpenAI Agents SDK
- Postgres + pgvector (Docker Compose)
- MCP-серверы с JWT (RFC 8707)
- Dogwood (Cedar) как PDP, guardrail-сервис
- LLM: любой OpenAI-совместимый API, локально — LM Studio

## Установка

Нужны Python 3.12, Docker и запущенный OpenAI-совместимый LLM
(например, LM Studio на `http://127.0.0.1:1234/v1`).

```bash
git clone https://github.com/sergeloie/llm-developer-project-445.git
cd llm-developer-project-445
cp .env.example .env      # Windows: copy .env.example .env
```

В `.env` укажите `LLM_BASE_URL` и `LLM_MODEL` вашей модели. Остальные
переменные (порты, `PG_DSN`, TTL) можно оставить по умолчанию.

```bash
make install              # зависимости из requirements.txt
make infra                # Postgres + pgvector в Docker (:5434)
make migrate              # схема db/*.sql
make seed                 # демо-данные и база знаний
```

Всё сразу: `make infra && make setup` (infra + install + migrate + seed).

## Использование

Пять процессов контура запускаются в отдельных терминалах:

```bash
make stand                # :8100  issuer, read-MCP, моки, веб-чат
make actions              # :8220  action-MCP
make pdp                  # :9100  PDP (Dogwood)
make guardrail            # :8300  guardrail
make api                  # :8000  backend/оркестратор
```

Откройте чат: <http://127.0.0.1:8100>. Демо-пользователи: `anna`
(клиент с правом менять тариф), `boris` (только чтение), `olga`
(оператор поддержки). Страница оператора: <http://127.0.0.1:8100/operator.html>.

Пример диалога: «переведи меня на игровой тариф» → карточка → «да».
Повторное «да» не исполняет действие второй раз.

Проверки:

```bash
make smoke                # группы 0–5, без модели
make smoke G=4            # одна группа
make scenarios            # матрица диалогов на живой модели
make reset                # вернуть моки в демо-состояние
```

Отчёт приёмки контура — [ACCEPTANCE.md](ACCEPTANCE.md).

---

<details>
<summary>Автоматические тесты Хекслета</summary>

Тесты запускаются на каждый коммит. За запуск отвечает файл `.github/workflows/hexlet-check.yml` — не удаляйте и не переименовывайте ни его, ни репозиторий.

</details>

## О Хекслете

[Хекслет](https://ru.hexlet.io/) — школа программирования: авторские программы обучения с практикой, поддержкой наставников и реальными проектами, которые остаются в резюме. Этот репозиторий — один из таких проектов.
