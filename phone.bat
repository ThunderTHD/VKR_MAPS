@echo off
chcp 65001 >nul
rem Вход с телефона монтажника: HTTPS-адрес ноутбука в Wi-Fi и QR-код для телефона.
rem Телефон и ноутбук должны быть в одной Wi-Fi-сети. Нужен запущенный Docker Desktop.

cd /d "%~dp0"
if not exist .env copy .env.example .env >nul

powershell -NoProfile -ExecutionPolicy Bypass -File tools\phone.ps1
if errorlevel 1 (
  pause
  exit /b 1
)

echo Перезапускаю систему с HTTPS для телефона...
docker compose up -d --build
if errorlevel 1 (
  echo Ошибка запуска. Подробности: docker compose logs
  pause
  exit /b 1
)

:wait
timeout /t 3 /nobreak >nul
curl -fs http://localhost:8000/api/health >nul 2>&1
if errorlevel 1 goto wait

start "" http://localhost:8000/phone.html
echo Готово. На открывшейся странице — QR-код: наведите на него камеру телефона.
pause
