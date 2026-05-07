@echo off
set PYTHONPATH=%~dp0
cd /d "%~dp0"
echo.
echo  ================================================
echo   FreightIQ -- Dry Bulk Intelligence Platform
echo   Dash UI  --  http://localhost:8503
echo  ================================================
echo.
call conda activate base 2>nul
python app_dash.py
pause
