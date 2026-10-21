@echo off
chcp 65001 >nul
rem Остановка ГИС ВОЛС. Данные в базе сохраняются.
cd /d "%~dp0"
docker compose down
echo Система остановлена. Данные сохранены.
pause
