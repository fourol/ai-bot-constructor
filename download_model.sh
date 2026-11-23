#!/usr/bin/env bash
set -e

echo "================================================================"
echo "     Скачивание llama-server и модели Qwen 2.5 3B (GGUF)"
echo "================================================================"
echo ""

mkdir -p llm
cd llm

# 1. Скачивание llama-server для Linux x64
if [ ! -f "llama-b12651/llama-server" ] && [ ! -f "llama-server" ]; then
    echo "[1/2] Скачивание llama-server для Linux..."
    curl -L "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-ubuntu-x64.tar.gz" -o llama-linux.tar.gz
    tar -xzf llama-linux.tar.gz
    rm -f llama-linux.tar.gz
    echo "llama-server готов."
else
    echo "[1/2] llama-server уже существует."
fi

# 2. Скачивание модели Qwen 2.5 3B Instruct Q4_K_M (1.9 ГБ)
if [ ! -f "qwen2.5-3b-instruct-q4_k_m.gguf" ]; then
    echo ""
    echo "[2/2] Скачивание модели Qwen 2.5 3B Instruct (GGUF, ~1.9 ГБ)..."
    curl -L -C - "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" -o qwen2.5-3b-instruct-q4_k_m.gguf
    echo "Модель успешно скачана."
else
    echo "[2/2] Файл модели qwen2.5-3b-instruct-q4_k_m.gguf уже скачан."
fi

cd ..
echo ""
echo "================================================================"
echo " Все файлы скачаны! Теперь запустите ./start_llm.sh для старта LLM"
echo "================================================================"
