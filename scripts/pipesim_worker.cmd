@echo off
setlocal DisableDelayedExpansion
rem Paths are provided only by the locally configured runner, never by MCP arguments.
call "%WELL_AGENT_PTK_ROOT%\Scripts\activate.bat" "%WELL_AGENT_PTK_ROOT%"
if errorlevel 1 goto activation_failed
"%WELL_AGENT_PTK_ROOT%\python.exe" "%WELL_AGENT_RUN_SCRIPT%" "%WELL_AGENT_SOURCE_MODEL%" "%WELL_AGENT_RATES%" "%WELL_AGENT_OUTPUT_DIR%" --prepared-model "%WELL_AGENT_PREPARED_MODEL%" --seed-report "%WELL_AGENT_SEED_REPORT%" --apply
set "WELL_AGENT_EXIT_CODE=%errorlevel%"
goto finished
:activation_failed
set "WELL_AGENT_EXIT_CODE=90"
:finished
> "%WELL_AGENT_RECEIPT%" echo %WELL_AGENT_EXIT_CODE%
exit /b %WELL_AGENT_EXIT_CODE%
