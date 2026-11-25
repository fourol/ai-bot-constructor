@echo off
chcp 65001 >nul
title КС54 • AI-Интегратор — Автоустановка для Windows 10 (T1000 8GB)

REM Проверка прав Администратора и автозапрос UAC при необходимости
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ИНФО] Запрашиваем права Администратора для установки и настройки Брандмауэра...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ========================================================================
echo    Запуск полного авторазвёртывания КС54 • AI-Интегратор
echo ========================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_windows.ps1"

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] Во время установки возникла ошибка (код %errorlevel%).
    pause
    exit /b %errorlevel%
)
