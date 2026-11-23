#!/usr/bin/env bash
set -e

echo "================================================================"
echo "          Запуск локальной LLM Qwen 2.5 3B"
echo "          Сервер OpenAI API: http://127.0.0.1:8000/v1"
echo "================================================================"
echo ""

cd llm

if [ ! -f "qwen2.5-3b-instruct-q4_k_m.gguf" ]; then
    echo "[ОШИБКА] Файл модели не найден в папке llm!"
    echo "Сначала запустите ./download_model.sh"
    exit 1
fi

SERVER_BIN=""
if [ -f "llama-b12651/llama-server" ]; then
    SERVER_BIN="./llama-b12651/llama-server"
elif [ -f "./llama-server" ]; then
    SERVER_BIN="./llama-server"
else
    echo "[ОШИБКА] Бинарник llama-server не найден в папке llm!"
    echo "Сначала запустите ./download_model.sh"
    exit 1
fi

echo "Запуск $SERVER_BIN..."
exec "$SERVER_BIN" -m qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 8000 -c 4096 -ngl 99
