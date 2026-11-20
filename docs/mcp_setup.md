# MCP: чтение сохранённого расчёта

Реализован инструмент `read_results`: `base`, `plus10`, `compare`.
Он читает отчёт, проверяет контрольные суммы и сохраняет журнал вызова.
PIPESIM и LLM для этой проверки не запускаются.

## Окружение

Используем отдельный Python 3.11+; проверено на Python 3.12.
В окружении PIPESIM Python 3.8 устанавливать MCP не нужно.
Закреплены MCP SDK 1.29.0, Pydantic 2.13.4 и jsonschema 4.26.0.
Версия MCP-протокола согласуется клиентом и сервером при подключении.

Открой новый обычный cmd и посмотри установленные интерпретаторы:

```bat
py -0p
```

Далее пример для установленного Python 3.12. Если установлен только 3.11,
замени `-3.12` на `-3.11`. Команды выполняются из корня репозитория:

```bat
py -3.12 -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install -r requirements-mcp.txt
```

Активация venv не обязательна: во всех командах указан его python.exe.

## Регистрация существующего расчёта

Требуются исходная `agzu6_network_4.pips`, соседний отчёт её сборки
`agzu6_network_4.json`, исходный `scenario_rates.json` и успешный
`runs\agzu6_isothermal_auto_2\report.json`.

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp register ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
 --report "C:\Users\User\Desktop\well-agent-data\runs\agzu6_isothermal_auto_2\report.json" ^
 --model "C:\Users\User\Desktop\well-agent-data\models\agzu6_network_4.pips" ^
 --rates "C:\Users\User\Desktop\well-agent-data\fluid\scenario_rates.json"
```

Ожидается `Registration: REGISTERED` и `run_id: UUID`.
Скопируй выведенный UUID: он понадобится в следующей команде.
Повторная регистрация той же версии файла возвращает тот же run_id.

Если контрольные суммы не совпали, регистрация останавливается.
Пересчитывать или редактировать контрольные суммы вручную не требуется.

## Проверка через настоящий MCP-клиент

Подставь UUID после `--run-id`:

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp check ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
 --run-id UUID_ИЗ_РЕГИСТРАЦИИ ^
 --output "C:\Users\User\Desktop\well-agent-data\mcp_check.json"
```

Команда сама запускает MCP-сервер дочерним процессом, выполняет
подключение, получает описание инструмента и проверяет шесть вызовов:

- чтение `base`, `plus10` и `compare`;
- неизвестный run_id;
- неподдерживаемый сценарий `plus20`;
- лишний аргумент `pressure: 12`.

Также проверяются схемы, единицы, арифметика сравнения, совпадение
текстового и структурированного ответов, trace_id и файлы журнала.
Ожидаемый итог: `MCP: MCP_CHECK_PASSED`.
При повторе укажи новое имя выходного JSON, например `mcp_check_2.json`.

Предупреждения Run остаются в результатах. Они не превращаются
в ошибку чтения только из-за своего наличия.
`physical_validation` остаётся `NOT_ASSESSED`.

## Ручной запуск сервера

Для будущего MCP-клиента или агента:

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp serve ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry"
```

Это stdio-сервер: он ожидает MCP-сообщения, а не команды, введённые вручную.
Его stdout занят протоколом. Для обычной проверки используй `check` выше.
Вывод диагностик идёт в stderr, журналы — в `mcp_registry\traces`.

## Автотесты и файлы проекта

```bat
.venv-mcp\Scripts\python.exe -m unittest discover -s tests -p test_mcp_read_results.py -v
```

Тесты используют синтетические данные; один тест поднимает настоящий
MCP stdio-клиент и сервер. Лицензия PIPESIM не требуется.

Исходники сервера: `src/well_agent_mcp/`.
Схемы запроса и ответа: `src/well_agent_mcp/schemas.py`.
Контракт: [mcp_contract.md](mcp_contract.md).

В GitHub добавляются код, тесты, `requirements-mcp.txt` и документация.
Реестр, журналы, отчёты, модели и `.venv-mcp` остаются локально.

Следующая интеграция — отдельный инструмент `run_steady_state`.
Текущий `read_results` только читает сохранённые результаты.
