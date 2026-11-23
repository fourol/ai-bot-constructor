@echo off
chcp 65001 >nul
title Скачивание llama-server и модели Qwen 2.5 3B

echo ================================================================
echo      Скачивание llama-server и модели Qwen 2.5 3B (GGUF)
echo ================================================================
echo.

if not exist "llm" (
    mkdir llm
)

cd llm

REM 1. Скачивание llama-server для Windows x64
if not exist "llama-server.exe" (
    echo [1/2] Скачивание llama-server для Windows...
    curl -L "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cpu-x64.zip" -o llama-win.zip
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Не удалось скачать llama-server.
        pause
        exit /b 1
    )
    echo Распаковка llama-server...
    tar -xf llama-win.zip
    del llama-win.zip
    echo llama-server готов.
) else (
    echo [1/2] llama-server уже существует.
)

REM 2. Скачивание модели Qwen 2.5 3B Instruct Q4_K_M (1.9 ГБ)
if not exist "qwen2.5-3b-instruct-q4_k_m.gguf" (
    echo.
    echo [2/2] Скачивание модели Qwen 2.5 3B Instruct (GGUF, ~1.9 ГБ)...
    curl -L -C - "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" -o qwen2.5-3b-instruct-q4_k_m.gguf
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Ошибка скачивания модели.
        pause
        exit /b 1
    )
    echo Модель успешно скачана.
) else (
    echo [2/2] Файл модели qwen2.5-3b-instruct-q4_k_m.gguf уже скачан.
)

cd ..
echo.
echo ================================================================
echo  Все файлы скачаны! Теперь запустите start_llm.bat для старта LLM
echo ================================================================
pause
