@echo off
rem  stock_view - launch the dashboard.
rem
rem  Double-click this file. It activates a venv if one is sitting next to it,
rem  then starts Streamlit.
rem
rem  It does NOT run the scan pipeline. stock_view only reads the JSON the
rem  pipeline already wrote. To refresh that data, run  py scan_report.py  in
rem  the folder above, then press the Reload button in the sidebar.

setlocal
cd /d "%~dp0"

rem  A venv beside the app wins, then one a level up (the pipeline's own).
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
)

rem  Prefer the py launcher, fall back to whatever python is on PATH.
set "PY=python"
where py >nul 2>&1
if not errorlevel 1 set "PY=py"

%PY% -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   streamlit is not installed for this interpreter.
    echo   Run:  %PY% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting stock_view - close this window to stop it.
echo.
%PY% -m streamlit run stock_view.py

if errorlevel 1 pause
endlocal
