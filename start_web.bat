@echo off
chcp 65001 >nul
title Конструктор ИИ-ботов (Порт 3000)

echo ================================================================
echo           Конструктор ИИ-ботов (Мастер-класс)
echo           Сервер запускается на http://0.0.0.0:3000
echo ================================================================
echo.

REM Проверка наличия Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не обнаружен в PATH!
    echo Пожалуйста, установите Python 3.10+ с галочкой "Add Python to PATH".
    echo Скачать: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Проверка и создание виртуального окружения при первом запуске
if not exist "venv" (
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
echo [ИНФО] Локальная LLM должна быть запущена по адресу: http://127.0.0.1:8080/v1
echo [ИНФО] Для доступа участников откройте в браузере: http://IP_СЕРВЕРА:3000
echo.
echo Запуск веб-сервера Uvicorn...
echo.

uvicorn main:app --host 0.0.0.0 --port 3000

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Сервер аварийно завершил работу.
    pause
)
