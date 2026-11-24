# Вход и выход агента

Точка входа: `run(request, ctx) -> AgentResult`.

Текущий вход: request — строка, ctx — AgentContext или словарь с registry,
run_id, необязательными request_id и allow_run. Пути и UUID задаёт вызывающий
код, а не LLM. run() вызывается из синхронного Python-кода.

AgentResult содержит answer, status, request_id, trace_id, task,
execution_status, numerical_validation, physical_validation, evidence и error.
В evidence сохранены имя инструмента, аргументы и полный проверенный MCP-ответ.
Agent status: SUCCESS, CHECK_REQUIRED, REFUSED или ERROR; он не заменяет статус
расчёта и проверок. Числа и предупреждения в answer берутся из evidence.
Физическая проверка остаётся NOT_ASSESSED.

Запуск и ограничения: [agent_setup.md](agent_setup.md).

## Примеры

- [request.json](../examples/request.json) — запрос на базовый расчёт.
- [result.json](../examples/result.json) — ожидаемый формат результата
  для того же сценария.
- [error.json](../examples/error.json) — отказ для отдельного сценария:
  давление на выходе задано числом 12 без единицы измерения.

Это прежние проектные примеры формата, а не результаты запуска PIPESIM
и не актуальная схема AgentResult из src/agent/schemas.py.
Числа, конфигурации и trace_id условные.
request.json описывает сценарий, а не готовые аргументы вызова run().

## Статусы

Состояние расчёта и результат проверки хранятся отдельно:

- SUCCESS — расчёт выполнен.
- NOT_RUN — расчёт не запускался.
- OK — результат в пределах утверждённого допуска.
- OUT_OF_TOLERANCE — допуск превышен.
- NOT_ASSESSED — соответствие допуску не оценено.

Поэтому SUCCESS может сочетаться с NOT_ASSESSED:
расчёт завершён, но допуск ещё не задан.

## Результаты и ошибки

Каждая расчётная величина содержит объект, значение и единицу.
Для давления MCP явно указывает абсолютную шкалу.

При отказе до MCP evidence остаётся пустым, а error содержит код и причину.
При частичном расчёте доступные данные сохраняются в evidence; недостающие
значения не заменяются нулями. Agent trace_id связывает ответ с журналом агента;
вложенные MCP trace_id связывают evidence с журналами инструментов.

## Контракт MCP-инструментов

Инструменты `read_results` и `run_steady_state` описаны отдельно:
[mcp_contract.md](mcp_contract.md).
Контракт MCP не заменяет точку входа агента `run(request, ctx)`
и не меняет существующие примеры бенчмарка.
