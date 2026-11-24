-- Бонус (шаг 10): фактический провайдер и стоимость вызова по его цене.
alter table llm_calls add column if not exists provider text;
alter table llm_calls add column if not exists cost_rub numeric(12, 6);
