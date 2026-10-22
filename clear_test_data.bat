@echo off
chcp 65001 >nul
rem Удалить тестовую сеть генератора. Импортированные и введённые вручную данные остаются.
rem Система должна быть запущена (start.bat).

cd /d "%~dp0"
echo Будет удалена ТЕСТОВАЯ сеть (созданная generate.bat).
echo Импортированные из KML и введённые вручную данные останутся.
choice /C YN /M "Продолжить? Y - да, N - нет"
if errorlevel 2 exit /b 0
docker compose exec -T app python -m app.cli clear-generated
if errorlevel 1 (
  echo Ошибка. Система запущена? Сначала start.bat
)
pause
