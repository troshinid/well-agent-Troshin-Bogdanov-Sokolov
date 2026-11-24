# Переход well-agent на RouterAI

## 1. Обновить код

Распакуйте `well_agent_routerai.zip` в корень существующего `well-agent`,
рядом с `agent.py`, с заменой файлов. Архив содержит обновление агента,
а не полный проект. Виртуальное окружение, реестр и расчётные данные сохраняются.
Локальный `.env` в архив не включён и не перезаписывается.
Старый `src/agent/openrouter.py` можно удалить: код использует `src/agent/routerai.py`.

Зависимости не изменились. Если прежняя версия агента ещё не установлена:

```bat
.venv-mcp\Scripts\python.exe -m pip install -r requirements-agent.txt
```

## 2. Указать новый ключ

Если `.env` уже существует, откройте его локально:

```bat
notepad .env
```

Замените старые строки `OPENROUTER_API_KEY` и `OPENROUTER_MODEL` на:

```dotenv
ROUTERAI_API_KEY=сюда_новый_ключ_RouterAI
ROUTERAI_MODEL=deepseek/deepseek-v4.1-flash-1017
```

Сохраните остальные настройки файла. Настоящий ключ вводится только локально;
его не нужно отправлять в чат или добавлять в GitHub.
Если `.env` ещё нет, выполните скрытый ввод ключа:

```bat
.venv-mcp\Scripts\python.exe agent.py configure-llm
```

При вопросе о модели нажмите Enter. Команда создаёт только новый `.env`;
существующий файл она не перезаписывает. Сообщение CONFIGURED подтверждает
сохранение настроек, а не доступность API.

Запрос отправляется на `https://routerai.ru/api/v1/chat/completions` с Bearer-ключом.
Адрес задан в коде. Старые переменные `OPENROUTER_*` больше не используются.
Переменные `ROUTERAI_*` из окружения имеют приоритет над `.env`; если они уже
заданы в терминале или Windows, обновите и их.

## 3. Проверить чтение

Из корня проекта в cmd выполните:

```bat
.venv-mcp\Scripts\python.exe agent.py ask ^
  --registry "C:\Users\User\Desktop\well-agent-data\mcp_registry" ^
  --run-id 040c309a-ab7b-55b3-ab0c-6bf9731e230a ^
  --request "Сравни сохранённые результаты base и plus10 для участка АГЗУ-6" ^
  --output "C:\Users\User\Desktop\well-agent-data\agent_routerai_read_check.json"
```

Путь и run-id взяты из прежней инструкции проекта. Если локальная копия другая,
используйте её путь и UUID успешного зарегистрированного расчёта.
Выберите новое имя output, если файл уже существует. Для первого обращения к
RouterAI не передавайте старый request-id: новый UUID создастся автоматически.

Ожидаются `Agent: SUCCESS`, `task.action=read_results` и `read_results` в evidence.
Новый расчёт PIPESIM не запускается. API-запрос может расходовать средства.
Результат этой проверки — `agent_routerai_read_check.json`; `.env` присылать не нужно.
Для нового расчёта после успешного чтения используйте раздел 4 в `docs/agent_setup.md`.
Повторно регистрировать ту же модель и настраивать configure-runner не требуется.

## Что изменено и проверено

- Клиент, импорты, сообщения и конфигурация переведены на RouterAI.
- Модель по умолчанию: `deepseek/deepseek-v4.1-flash-1017`.
- Передаётся строгая JSON-схема; ответ дополнительно проверяется локально.
  Параметр `provider.require_parameters` удалён из запроса.
- В журнал LLM добавлено `provider=routerai`; версия инструкции обновлена,
  поэтому прежний сохранённый план не переиспользуется с тем же request-id.
  Исход незавершённого старого расчёта сначала следует выяснить через MCP
  с его прежним request-id, прежде чем создавать новый расчёт.
- Пройдены 56 автоматических тестов с имитацией API и расчётного процесса,
  включая обмен через настоящий MCP stdio.
- Реальный запрос с новым ключом и запуск Windows/PIPESIM не выполнялись.

Повторить автоматические проверки:

```bat
.venv-mcp\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Документация RouterAI:

- https://routerai.ru/models/deepseek/deepseek-v4.1-flash-1017
- https://routerai.ru/docs/reference
