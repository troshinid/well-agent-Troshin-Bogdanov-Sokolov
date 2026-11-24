@echo off
setlocal DisableDelayedExpansion
call "%WELL_AGENT_PTK_ROOT%\Scripts\activate.bat" "%WELL_AGENT_PTK_ROOT%"
if errorlevel 1 goto failed
"%WELL_AGENT_PTK_ROOT%\python.exe" "scripts\build_network_model.py" "%WELL_AGENT_BUILD%\inputs\fluid.pips" "%WELL_AGENT_BUILD%\inputs\pipe.json" "%WELL_AGENT_BUILD%\inputs\rates.json" "%WELL_AGENT_BUILD%\network.pips" --settings "%WELL_AGENT_BUILD%\inputs\settings.json" --apply
set "build_rc=%errorlevel%"
goto finished
:failed
set "build_rc=90"
:finished
> "%WELL_AGENT_BUILD%\exit.tmp" echo %build_rc%
move /Y "%WELL_AGENT_BUILD%\exit.tmp" "%WELL_AGENT_BUILD%\exit.txt" >nul
exit /b %build_rc%
