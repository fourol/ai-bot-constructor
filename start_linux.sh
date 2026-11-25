#!/usr/bin/env bash
set -e

echo "================================================================"
echo "          AI-Интегратор ИИ-ботов (Мастер-класс)"
echo "          Сервер запускается на http://0.0.0.0:3000"
echo "================================================================"
echo ""

if ! command -v python3 &> /dev/null; then
    echo "[ОШИБКА] Python 3 не найден в системе!"
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "[1/2] Создание виртуального окружения Python..."
    python3 -m venv venv
    source venv/bin/activate
    echo "[2/2] Установка необходимых библиотек из requirements.txt..."
    pip install --upgrade pip
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo ""
echo "[ИНФО] Локальная LLM должна быть запущена по адресу: http://127.0.0.1:8080/v1"
echo "[ИНФО] Для доступа участников откройте в браузере: http://IP_СЕРВЕРА:3000"
echo ""
echo "Запуск веб-сервера Uvicorn..."
echo ""

exec uvicorn main:app --host 0.0.0.0 --port 3000
