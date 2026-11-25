@echo off
chcp 65001 >nul
cd /d "%~dp0"
title КС54 • НейроКонструктор (Веб-сервер на порту 3000)

echo ================================================================
echo          КС54 • НейроКонструктор (Мастер-класс 20+ чел.)
echo          Сервер запускается на http://0.0.0.0:3000
echo ================================================================
echo.

REM Проверка наличия Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не обнаружен в PATH!
    echo Запустите install_windows.bat для автоматической установки.
    pause
    exit /b 1
)

REM Проверка и создание виртуального окружения при первом запуске
if not exist "venv\Scripts\uvicorn.exe" (
    echo [1/2] Создание виртуального окружения Python...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
    call venv\Scripts\activate.bat
    echo [2/2] Установка необходимых библиотек из requirements.txt...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Ошибка при установке библиотек.
        pause
        exit /b 1
    )
) else (
    call venv\Scripts\activate.bat
)

echo.
echo [ИНФО] Локальная LLM ожидается по адресу: http://127.0.0.1:8000/v1
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | ForEach-Object { Write-Host ('[ИНФО] Адрес для участников в сети: http://' + $_.IPAddress + ':3000') -ForegroundColor Cyan }"
echo.
echo Запуск веб-сервера Uvicorn (20+ одновременных подключений)...
echo.

uvicorn main:app --host 0.0.0.0 --port 3000 --timeout-keep-alive 65 --backlog 2048

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Сервер аварийно завершил работу.
    pause
)
