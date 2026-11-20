# MCP: новый расчёт `run_steady_state`

Инструмент повторяет согласованные `base` и `plus10` в новой копии
уже подготовленной модели. Температура, флюид, геометрия и граничные
условия считываются и проверяются; инструмент их не назначает заново.

Сервер работает в `.venv-mcp`, а расчёт — в отдельном Python 3.8 Toolkit.
`scripts/pipesim_worker.cmd` выполняет проверенную активацию Toolkit
и запускает `run_network_model.py` в режиме `--prepared-model`.
Вывод расчётного процесса сохраняется в `worker.log`, отдельно от MCP stdout.

MCP stdio по умолчанию наследует сокращённое окружение. Поэтому клиент
явно передаёт стандартные пути Windows (включая `ProgramData`) и уже
настроенные переменные лицензирования SLB. Значения не выводятся
в ответы и журналы; `forwarded_environment_presence` содержит только
логические признаки наличия переменных. Параметры лицензии инструмент не меняет.

## Обновление файлов

Скопируй содержимое архива в корень проекта с объединением папок
и заменой совпадающих файлов. Используется уже созданная `.venv-mcp`.
Новых зависимостей нет.

## Локальный профиль запуска

Команды выполняются из корня репозитория. Профиль связывает исходный
run_id, подготовленную модель, установленный Toolkit, папку результатов
и контрольные суммы кода. Секреты и исполняемые команды в профиль не вводятся.

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp configure-runner ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
 --seed-run-id a6c13b93-011d-5a07-8107-3cfb628eb843 ^
 --prepared-model "C:\Users\User\Desktop\well-agent-data\runs\agzu6_isothermal_auto_2\isothermal_auto.pips" ^
 --toolkit-root "C:\Program Files\Schlumberger\PIPESIM_PTK_2021.1" ^
 --output-root "C:\Users\User\Desktop\well-agent-data\mcp_runs"
```

Ожидается `Runner: CONFIGURED`. Эта команда проверяет файлы
и сохраняет профиль; расчёт запускается следующей командой.
Путь, UUID и набор сценариев относятся к текущему проверенному участку.
На другом компьютере их задаёт оператор в локальной настройке.

## Расчёт через настоящий MCP-клиент

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp run-check ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
 --seed-run-id a6c13b93-011d-5a07-8107-3cfb628eb843 ^
 --output "C:\Users\User\Desktop\well-agent-data\mcp_run_check.json"
```

Команда создаёт `request_id` и сохраняет его до начала расчёта,
запускает MCP-сервер, проверяет отказы на неверные аргументы,
затем вызывает `run_steady_state`.
Сервер создаёт отдельную папку задачи и запускает Toolkit.
Выполняются Run и Restart для обоих сценариев.
После этого клиент повторяет запрос с тем же request_id и проверяет,
что новый расчёт не запущен, а новый run_id сохранился.
Далее читает результат через `read_results` и сравнивает давления
с исходным расчётом в пределах допуска численного воспроизведения.

Ожидаемый итог: `MCP run: MCP_RUN_CHECK_PASSED` и `new run_id: UUID`.
PIPESIM на этом шаге действительно выполняет новый расчёт;
успех нужно подтвердить на компьютере с установленным Toolkit.

## Папки и повтор вызова

Для каждой задачи создаётся `mcp_runs\REQUEST_ID\`:

- `worker.log` — stdout и stderr процесса Toolkit;
- `exit_code.txt` — код завершения процесса;
- `calculation\report.json` — отчёт расчёта;
- `calculation\isothermal_auto.pips` — новая расчётная модель;
- `calculation\base` и `calculation\plus10` — результаты обоих этапов.

В реестре остаются `runner.json`, `jobs\REQUEST_ID.json` и журналы `traces`.
Входные модели и прежние отчёты сохраняются без изменений.

По умолчанию сервер ожидает до 600 секунд. При таймауте возвращается
`TIMEOUT`, состояние — `OUTCOME_UNKNOWN`. Это не доказывает отказ расчёта.
Процесс принудительно не завершается, второй процесс не создаётся.
Для проверки исхода используй тот же UUID:

```bat
.venv-mcp\Scripts\python.exe -m src.well_agent_mcp run-check ^
 --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
 --seed-run-id a6c13b93-011d-5a07-8107-3cfb628eb843 ^
 --request-id UUID_ИЗ_ПРЕДЫДУЩЕГО_ЗАПРОСА ^
 --output "C:\Users\User\Desktop\well-agent-data\mcp_run_check_2.json"
```

Если задача ещё выполняется, новый request_id получит `BUSY`.
Не удаляй `runner.lock` вручную до выяснения состояния процесса.
При обновлении кода профиль нужно настроить снова после завершения
предыдущего расчёта; старый request_id с новой конфигурацией не переиспользуется.

## Проверки и границы

```bat
.venv-mcp\Scripts\python.exe -m unittest discover -s tests -p "test_mcp_*.py" -v
```

Тесты запуска и отказов используют имитацию расчётного процесса.
Есть обмен настоящих MCP-клиента и сервера с таким процессом.
Это не заменяет проверку гидравлики в установленном PIPESIM.

Через MCP принимаются только `seed_run_id`, `request_id` и
`scenario_set: "base_plus10"`. Произвольные пути, команды и новые
свойства смеси не принимаются. Подготовка и изменение входных параметров
остаются отдельными операциями целевой архитектуры.
`physical_validation` сохраняет значение `NOT_ASSESSED`.

В GitHub добавляются исходники, тесты и документация.
Модели, реестр, журналы, результаты и `.venv-mcp` остаются локально.
