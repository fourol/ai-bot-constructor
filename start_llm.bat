@echo off
chcp 65001 >nul
cd /d "%~dp0"
title llama-server (Qwen 2.5 3B • T1000 8GB • 24 параллельных слота)

echo ================================================================
echo     Запуск локальной LLM Qwen 2.5 3B (NVIDIA T1000 8GB)
echo     Режим: 24 параллельных слота (-np 24 -c 49152) • 20+ юзеров
echo     OpenAI API: http://127.0.0.1:8000/v1
echo ================================================================
echo.

if not exist "llm\qwen2.5-3b-instruct-q4_k_m.gguf" (
    echo [ОШИБКА] Файл модели не найден в папке llm\!
    echo Сначала запустите install_windows.bat или download_model.bat
    pause
    exit /b 1
)

set "SERVER_EXE="
if exist "llm\llama-server.exe" (
    set "SERVER_EXE=llama-server.exe"
) else if exist "llm\llama-b12651\llama-server.exe" (
    set "SERVER_EXE=llama-b12651\llama-server.exe"
) else (
    echo [ОШИБКА] llama-server.exe не найден в папке llm\!
    echo Сначала запустите install_windows.bat или download_model.bat
    pause
    exit /b 1
)

cd /d "%~dp0llm"
echo Запуск %SERVER_EXE% (GPU offload -ngl 99, 24 слота, контекст 49152)...
"%SERVER_EXE%" -m qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8000 -ngl 99 -np 24 -c 49152 -cb -fa auto --threads-http 8

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] llama-server завершил работу с ошибкой.
    echo Проверьте драйвер NVIDIA или наличие cudart64_*.dll в папке llm\.
    pause
)
