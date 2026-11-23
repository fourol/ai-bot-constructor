@echo off
chcp 65001 >nul
title llama-server (Qwen 2.5 3B на порту 8000)

echo ================================================================
echo           Запуск локальной LLM Qwen 2.5 3B
echo           Сервер OpenAI API: http://127.0.0.1:8000/v1
echo ================================================================
echo.

cd llm

if not exist "qwen2.5-3b-instruct-q4_k_m.gguf" (
    echo [ОШИБКА] Файл модели не найден в папке llm!
    echo Сначала запустите download_model.bat
    pause
    exit /b 1
)

if not exist "llama-server.exe" (
    echo [ОШИБКА] llama-server.exe не найден в папке llm!
    echo Сначала запустите download_model.bat
    pause
    exit /b 1
)

echo Запуск llama-server...
llama-server.exe -m qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8000 -c 4096 -ngl 99

if %errorlevel% neq 0 (
    echo.
    echo [ОШИБКА] llama-server завершил работу с ошибкой.
    pause
)
