@echo off
chcp 65001 >nul
rem Сгенерировать тестовую сеть и демонстрационных пользователей.
rem ВНИМАНИЕ: текущие данные сети удаляются (справочники и пользователи остаются).
rem Система должна быть запущена (start.bat).

cd /d "%~dp0"
echo Генерирую тестовую сеть...
docker compose exec -T app python -m app.cli generate --nodes 300 --seed 42
if errorlevel 1 (
  echo Ошибка. Система запущена? Сначала start.bat
  pause
  exit /b 1
)
docker compose exec -T app python -m app.cli demo-users
echo Моделирую устранённые повреждения (для журнала и калибровки)...
docker compose exec -T app python -m app.cli simulate-incidents --n 40
echo.
echo Готово. Откройте http://localhost:8000
pause
