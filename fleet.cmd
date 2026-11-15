@echo off
setlocal
set "PYTHONUTF8=1"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto python
py -3 "%~dp0fleet.py" %*
exit /b %errorlevel%

:python
python "%~dp0fleet.py" %*
exit /b %errorlevel%
