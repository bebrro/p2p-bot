@echo off
echo ========================================
echo    P2P Bot - Mini App Setup
echo ========================================
echo.

REM Проверяем ngrok
where ngrok >nul 2>&1
if errorlevel 1 (
    echo [!] ngrok не установлен.
    echo.
    echo Установи одним из способов:
    echo   1. winget install ngrok.ngrok
    echo   2. choco install ngrok
    echo   3. Скачай с https://ngrok.com/download и положи ngrok.exe рядом с этим файлом
    echo.
    pause
    exit /b 1
)

echo [OK] ngrok найден.
echo.
echo Запускаю ngrok на порту 8080...
echo.
echo ВАЖНО: После запуска скопируй https://xxxx.ngrok-free.app
echo        и вставь в .env в строку WEBAPP_URL=
echo        Потом перезапусти бота.
echo.
ngrok http 8080
