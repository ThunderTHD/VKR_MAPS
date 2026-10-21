@echo off
chcp 65001 >nul
rem Запуск ГИС ВОЛС на Windows двойным щелчком.
rem Нужен запущенный Docker Desktop.

cd /d "%~dp0"
if not exist .env copy .env.example .env >nul

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop не запущен. Запустите его и повторите.
  pause
  exit /b 1
)

echo Собираю и запускаю систему. Первый запуск займёт 5-10 минут...
docker compose up -d --build
if errorlevel 1 (
  echo Ошибка запуска. Подробности: docker compose logs
  pause
  exit /b 1
)

echo Жду, пока приложение будет готово...
:wait
timeout /t 3 /nobreak >nul
curl -fs http://localhost:8000/api/health >nul 2>&1
if errorlevel 1 goto wait

start "" http://localhost:8000
echo Готово: http://localhost:8000
echo Остановить систему: stop.bat
pause
