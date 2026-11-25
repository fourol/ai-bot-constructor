@echo off
chcp 65001 >nul
cd /d "%~dp0"
title КС54 • НейроКонструктор — Запуск (20+ пользователей)

echo ========================================================================
echo    КС54 • НейроКонструктор — Запуск сервера (NVIDIA T1000 8GB)
echo ========================================================================
echo.

if not exist "llm\qwen2.5-3b-instruct-q4_k_m.gguf" (
    echo [ОШИБКА] Модель не найдена в папке llm\!
    echo Сначала запустите install_windows.bat для полной установки.
    pause
    exit /b 1
)

if not exist "venv\Scripts\uvicorn.exe" (
    echo [ОШИБКА] Виртуальное окружение Python не настроено!
    echo Сначала запустите install_windows.bat для полной установки.
    pause
    exit /b 1
)

echo [1/3] Запуск локального сервера нейросети (llama-server GPU)...
start "KS54 LLM Server (Port 8000)" cmd /k "cd /d "%~dp0" && start_llm.bat"

echo [2/3] Ожидание загрузки Qwen 2.5 3B в видеопамять T1000...
powershell -NoProfile -Command ^
  "$ok = $false; for ($i = 1; $i -le 60; $i++) { try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2; if ($r.status -eq 'ok') { $ok = $true; break } } catch {}; Start-Sleep -Seconds 1 }; if (-not $ok) { Write-Host '[!] LLM загружается дольше обычного, но веб-сервер всё равно запустится.' -ForegroundColor Yellow } else { Write-Host '[OK] Модель загружена в GPU и готова к работе!' -ForegroundColor Green }"

echo [3/3] Запуск веб-сервера конструктора на порту 3000...
start "KS54 Web Server (Port 3000)" cmd /k "cd /d "%~dp0" && start_web.bat"

timeout /t 2 /nobreak >nul
echo.
echo ========================================================================
echo    СЕРВЕР ЗАПУЩЕН! Откройте в браузере:
echo    Локально на сервере : http://127.0.0.1:3000
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | ForEach-Object { Write-Host ('    Для 20+ студентов   : http://' + $_.IPAddress + ':3000') -ForegroundColor Cyan }"
echo ========================================================================
echo.
echo Не закрывайте два открывшихся чёрных окна серверов во время работы!
start "" "http://127.0.0.1:3000"
pause
