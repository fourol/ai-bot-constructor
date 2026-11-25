# 🧠 КС54 • НейроКонструктор (ИИ-Лаборатория)

Готовое веб-приложение для обучения промпт-инжинирингу, RAG и сборке собственных ИИ-ассистентов на **20–25+ одновременных пользователей** в локальной сети (на базе **Windows 10 + NVIDIA T1000 8GB** или Linux).

Участники конструируют ботов из визуальных модулей на интерактивном холсте (Персонаж, Инструкция, Мозг, Температура, Few-Shot, Guardrails, Формат вывода, База знаний RAG `.txt/.pdf/.docx`), проходят встроенный интерактивный тур и общаются с ботами в общем чате с рендерингом Markdown, кода и формул.

---

## ⚡ Авторазвёртывание на Windows 10 Server (NVIDIA T1000 8GB • 20+ юзеров)

### Способ 1: Установка одной командой с GitHub (даже на чистую Windows 10)
Откройте **PowerShell от имени Администратора** (`Win + X` → Windows PowerShell (администратор)) и выполните:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://raw.githubusercontent.com/fourol/ai-bot-constructor/main/bootstrap_windows.ps1 | iex
```

Скрипт сам:
- проверит/установит **Git** и **Python 3.11**;
- создаст `venv` и установит все зависимости из `requirements.txt`;
- определит **NVIDIA T1000** через `nvidia-smi` и скачает **CUDA 12.4** `llama-server.exe` + `cudart` DLL (или Vulkan GPU при старом драйвере);
- скачает модель **Qwen 2.5 3B Instruct Q4_K_M** (~2.0 ГБ);
- откроет TCP-порт **3000** в Брандмауэре Windows;
- создаст ярлык **«КС54 НейроКонструктор»** на Рабочем столе и запустит `start_all.bat` с настройкой на **24 параллельных слота** (`-ngl 99 -np 24 -c 49152`).

### Способ 2: Ручной запуск автоустановки после `git clone`
```cmd
git clone https://github.com/fourol/ai-bot-constructor.git C:\ai-bot-constructor
cd C:\ai-bot-constructor
install_windows.bat
```
Для последующих запусков достаточно кликнуть по **`start_all.bat`** (или ярлыку на Рабочем столе).

📖 Подробная инструкция, расчёт видеопамяти T1000 и решение проблем: **[DEPLOY_WINDOWS.md](DEPLOY_WINDOWS.md)**

---

## 🚀 Архитектура и стек

- **Бэкенд**: Python 3.10+, FastAPI, Uvicorn, SQLite (`bots.db` в режиме `WAL`), `openai` (`AsyncOpenAI` + расширенный пул `httpx`), `pypdf`, `python-docx`, `python-multipart`, `rank-bm25`.
- **Фронтенд**: один самодостаточный файл `index.html` (визуальный нодовый редактор с проводами, справкой `ⓘ` на каждом модуле, автосохранением позиций, онбордингом и Markdown-рендерингом в чате).
- **RAG (Поиск по базе знаний)**: быстрый поиск релевантных фрагментов через `BM25Okapi` без тяжёлых эмбеддинг-моделей.
- **Локальная LLM**: `llama-server` с **Qwen 2.5 3B Instruct** (GGUF) с полным GPU-ускорением (`-ngl 99 -np 24 -c 49152 -cb`).

---

## 🐧 Быстрый запуск на Linux

```bash
# 1. Скачать llama-server и модель (1 раз)
./download_model.sh

# 2. Запустить LLM в первом терминале
./start_llm.sh

# 3. Запустить веб-сервер во втором терминале
./start_linux.sh
```

---

## 📂 Структура проекта

```text
├── main.py                # FastAPI бэкенд, RAG (BM25), SQLite WAL и стриминг LLM
├── index.html             # Фронтенд (Схема нод, Галерея, Чат с Markdown, Обучение)
├── requirements.txt       # Зависимости Python
├── bootstrap_windows.ps1  # Удалённый загрузчик с GitHub (1-line PowerShell)
├── deploy_windows.ps1     # Полное авторазвёртывание на Windows 10 (T1000 8GB, 20+ юзеров)
├── install_windows.bat    # Запуск deploy_windows.ps1 двойным кликом с правами Админа
├── start_all.bat          # Единый запуск LLM + Веб-сервера в 1 клик (Windows)
├── download_model.bat     # Скачивание CUDA/Vulkan llama-server и Qwen 2.5 3B (Windows)
├── start_llm.bat          # Запуск llama-server (Windows, -np 24 -c 49152 -ngl 99)
├── start_web.bat          # Запуск Uvicorn (Windows, порт 3000)
├── download_model.sh      # Скачивание llama-server и Qwen 2.5 3B (Linux)
├── start_llm.sh           # Запуск llama-server (Linux)
├── start_linux.sh         # Запуск Uvicorn (Linux)
├── DEPLOY_WINDOWS.md      # Подробная инструкция для Windows 10 Server (T1000 8GB)
├── .gitignore             # Игнорируемые файлы (venv, bots.db*, llm/, *.gguf)
└── README.md              # Документация проекта
```

---

## ⚙️ Переменные окружения (Опционально)

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `LLM_BASE_URL` | `http://127.0.0.1:8000/v1` | Адрес OpenAI-совместимого API локальной модели |
| `DB_PATH` | `bots.db` | Путь к базе данных SQLite |

---

## 📡 API Эндпоинты

- `GET /` — Веб-интерфейс `index.html`.
- `POST /api/bots` — Создание бота (`name`, `avatar`, `system_prompt`, `temperature`, `max_tokens`, `kb_text`, `kb_file`).
- `GET /api/bots` — Список всех ботов и количества чанков знаний.
- `GET /api/bots/{bot_id}` — Данные конкретного бота по ID.
- `DELETE /api/bots/{bot_id}` — Удаление бота и его базы знаний.
- `POST /api/chat` — Стриминг диалога с ботом (`StreamingResponse`, `text/plain`).
- `POST /api/test-draft` — Экспресс-тестирование черновика схемы прямо из ноды «Терминал Бота».
