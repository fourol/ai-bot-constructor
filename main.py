import os
import re
import io
import json
import secrets
import string
import sqlite3
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from rank_bm25 import BM25Okapi
from pypdf import PdfReader
from docx import Document

try:
    import httpx as _httpx
except ImportError:
    try:
        import httpx2 as _httpx
    except ImportError:
        _httpx = None

# Конфигурация
DB_PATH = os.getenv("DB_PATH", "bots.db")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))
STATIC_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.html")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# Клиент AsyncOpenAI с расширенным пулом соединений для 20+ одновременных пользователей
http_client = (
    DefaultAsyncHttpxClient(
        limits=_httpx.Limits(max_connections=120, max_keepalive_connections=50),
        timeout=_httpx.Timeout(180.0, connect=10.0),
    )
    if _httpx is not None
    else None
)
client = (
    AsyncOpenAI(base_url=LLM_BASE_URL, api_key="none", http_client=http_client, max_retries=1)
    if http_client is not None
    else AsyncOpenAI(base_url=LLM_BASE_URL, api_key="none", timeout=180.0, max_retries=1)
)

STARTER_BOTS = []
STARTER_BOT_IDS = set()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        is_fresh_db = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bots'"
        ).fetchone() is None

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                avatar TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(bots)")
        existing_cols = [c[1] for c in cursor.fetchall()]
        if "temperature" not in existing_cols:
            cursor.execute("ALTER TABLE bots ADD COLUMN temperature REAL DEFAULT 0.7")
        if "max_tokens" not in existing_cols:
            cursor.execute(f"ALTER TABLE bots ADD COLUMN max_tokens INTEGER DEFAULT {DEFAULT_MAX_TOKENS}")
        if "user_id" not in existing_cols:
            cursor.execute("ALTER TABLE bots ADD COLUMN user_id TEXT DEFAULT 'system'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                FOREIGN KEY (bot_id) REFERENCES bots (id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_bot_id ON knowledge_chunks(bot_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_task_progress (
                user_id TEXT PRIMARY KEY,
                progress_json TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS workstations (
                client_key TEXT PRIMARY KEY,
                pc_number INTEGER UNIQUE NOT NULL,
                pc_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Всегда гарантируем наличие официальных образцовых сценариев КС54
        for b_id, b_name, b_avatar, b_prompt, b_temp, b_tokens, b_kb in STARTER_BOTS:
            cursor.execute(
                "INSERT OR IGNORE INTO bots (id, name, avatar, system_prompt, temperature, max_tokens, user_id) VALUES (?, ?, ?, ?, ?, ?, 'system')",
                (b_id, b_name, b_avatar, b_prompt, b_temp, b_tokens),
            )
            cursor.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE bot_id = ?", (b_id,))
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "INSERT INTO knowledge_chunks (bot_id, chunk_text) VALUES (?, ?)",
                    (b_id, b_kb),
                )

        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    if http_client is not None:
        await http_client.aclose()


app = FastAPI(
    title="КС54 • AI-Интегратор",
    description="Платформа AI-интегратора Колледжа связи №54 для создания и тестирования ИИ-ботов (20+ одновременных пользователей)",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def generate_short_id(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> List[str]:
    cleaned = clean_text(text)
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    step = max(chunk_size - overlap, 50)
    chunks = []
    start = 0
    text_len = len(cleaned)

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start += step

    return chunks


def tokenize(text: str) -> List[str]:
    return re.findall(r"\w+", text.lower())


async def extract_text_from_file(file: UploadFile) -> str:
    max_size = 1024 * 1024  # 1 МБ
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail="Файл превышает допустимый размер (максимум 1 МБ)")

    filename = (file.filename or "").lower()
    text = ""

    if filename.endswith(".txt"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("cp1251")
            except UnicodeDecodeError:
                text = content.decode("latin-1", errors="replace")

    elif filename.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(content))
            pages_text = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages_text)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Ошибка чтения PDF файла: {str(e)}")

    elif filename.endswith(".docx"):
        try:
            doc = Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text]
            text = "\n".join(paragraphs)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Ошибка чтения DOCX файла: {str(e)}")

    else:
        raise HTTPException(
            status_code=400,
            detail="Неподдерживаемый формат файла. Поддерживаются только .txt, .pdf и .docx"
        )

    return text


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    bot_id: str
    message: str
    history: List[ChatMessage] = []


# Главная страница
@app.get("/")
async def serve_index():
    if not os.path.exists(STATIC_INDEX_PATH):
        raise HTTPException(status_code=404, detail="Файл index.html не найден")
    return FileResponse(STATIC_INDEX_PATH, media_type="text/html")


# Создание бота
@app.post("/api/bots")
async def create_bot(
    name: str = Form(...),
    avatar: str = Form(...),
    system_prompt: str = Form(...),
    kb_text: Optional[str] = Form(None),
    kb_file: Optional[UploadFile] = File(None),
    temperature: Optional[float] = Form(0.7),
    max_tokens: Optional[int] = Form(DEFAULT_MAX_TOKENS),
    user_id: Optional[str] = Form("anon"),
):
    name = name.strip()
    avatar = avatar.strip()
    system_prompt = system_prompt.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Имя бота обязательно")
    if not avatar:
        avatar = "🤖"
    if not system_prompt:
        raise HTTPException(status_code=400, detail="Системный промпт обязателен")

    file_text = ""
    if kb_file and kb_file.filename:
        file_text = await extract_text_from_file(kb_file)

    combined_kb = ""
    if kb_text and kb_text.strip():
        combined_kb += kb_text.strip() + " "
    if file_text and file_text.strip():
        combined_kb += file_text.strip()

    chunks = chunk_text(combined_kb)

    bot_id = generate_short_id(6)
    with get_db() as conn:
        # Убедимся в уникальности id
        cursor = conn.cursor()
        while True:
            cursor.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,))
            if not cursor.fetchone():
                break
            bot_id = generate_short_id(6)

        clean_user_id = (user_id or "anon").strip() or "anon"
        cursor.execute(
            "INSERT INTO bots (id, name, avatar, system_prompt, temperature, max_tokens, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bot_id, name, avatar, system_prompt, float(temperature if temperature is not None else 0.7), int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS), clean_user_id)
        )

        for chunk in chunks:
            cursor.execute(
                "INSERT INTO knowledge_chunks (bot_id, chunk_text) VALUES (?, ?)",
                (bot_id, chunk)
            )

        cursor.execute("SELECT created_at FROM bots WHERE id = ?", (bot_id,))
        row = cursor.fetchone()
        created_at = row["created_at"] if row else ""
        conn.commit()

    return {
        "id": bot_id,
        "name": name,
        "avatar": avatar,
        "system_prompt": system_prompt,
        "temperature": float(temperature if temperature is not None else 0.7),
        "max_tokens": int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS),
        "user_id": clean_user_id,
        "chunks_count": len(chunks),
        "created_at": created_at
    }


# Получение списка ботов только текущего пользователя
@app.get("/api/bots")
async def list_bots(user_id: Optional[str] = None):
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.id, b.name, b.avatar, b.system_prompt, b.temperature, b.max_tokens, b.created_at, b.user_id,
                   COUNT(k.id) as chunks_count
            FROM bots b
            LEFT JOIN knowledge_chunks k ON b.id = k.bot_id
            WHERE b.user_id = ?
            GROUP BY b.id
            ORDER BY b.created_at DESC
        """, (clean_uid,))
        rows = cursor.fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "avatar": row["avatar"],
            "system_prompt": row["system_prompt"],
            "temperature": row["temperature"] if row["temperature"] is not None else 0.7,
            "max_tokens": row["max_tokens"] if row["max_tokens"] is not None else DEFAULT_MAX_TOKENS,
            "created_at": row["created_at"],
            "user_id": row["user_id"] or "anon",
            "chunks_count": row["chunks_count"]
        }
        for row in rows
    ]


# Получение конкретного бота (только владелец)
@app.get("/api/bots/{bot_id}")
async def get_bot(bot_id: str, user_id: Optional[str] = None):
    clean_uid = (user_id or "").strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.id, b.name, b.avatar, b.system_prompt, b.temperature, b.max_tokens, b.created_at, b.user_id,
                   COUNT(k.id) as chunks_count
            FROM bots b
            LEFT JOIN knowledge_chunks k ON b.id = k.bot_id
            WHERE b.id = ?
            GROUP BY b.id
        """, (bot_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Бот не найден")
    if not clean_uid or row["user_id"] != clean_uid:
        raise HTTPException(status_code=403, detail="Нет доступа к чужому боту")

    return {
        "id": row["id"],
        "name": row["name"],
        "avatar": row["avatar"],
        "system_prompt": row["system_prompt"],
        "temperature": row["temperature"] if row["temperature"] is not None else 0.7,
        "max_tokens": row["max_tokens"] if row["max_tokens"] is not None else DEFAULT_MAX_TOKENS,
        "created_at": row["created_at"],
        "user_id": row["user_id"] or "anon",
        "chunks_count": row["chunks_count"]
    }


# Обновление существующего бота
@app.put("/api/bots/{bot_id}")
async def update_bot(
    bot_id: str,
    name: str = Form(...),
    avatar: str = Form(...),
    system_prompt: str = Form(...),
    kb_text: Optional[str] = Form(None),
    kb_file: Optional[UploadFile] = File(None),
    temperature: Optional[float] = Form(0.7),
    max_tokens: Optional[int] = Form(DEFAULT_MAX_TOKENS),
    user_id: Optional[str] = Form(None),
):
    bot_id = bot_id.strip()
    name = name.strip()
    avatar = avatar.strip()
    system_prompt = system_prompt.strip()

    if not name:
        raise HTTPException(status_code=400, detail="Имя бота обязательно")
    if not avatar:
        avatar = "🤖"
    if not system_prompt:
        raise HTTPException(status_code=400, detail="Системный промпт обязателен")

    file_text = ""
    if kb_file and kb_file.filename:
        file_text = await extract_text_from_file(kb_file)

    combined_kb = ""
    if kb_text and kb_text.strip():
        combined_kb += kb_text.strip() + " "
    if file_text and file_text.strip():
        combined_kb += file_text.strip()

    chunks = chunk_text(combined_kb)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id FROM bots WHERE id = ?", (bot_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Бот не найден")
        if bot_id in STARTER_BOT_IDS or existing["user_id"] == "system":
            raise HTTPException(status_code=403, detail="Системные образцы КС54 нельзя перезаписывать")
        clean_uid = (user_id or "").strip()
        if not clean_uid or existing["user_id"] != clean_uid:
            raise HTTPException(status_code=403, detail="Нельзя редактировать чужого бота")

        cursor.execute(
            """
            UPDATE bots 
            SET name = ?, avatar = ?, system_prompt = ?, temperature = ?, max_tokens = ?
            WHERE id = ?
            """,
            (
                name,
                avatar,
                system_prompt,
                float(temperature if temperature is not None else 0.7),
                int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS),
                bot_id
            )
        )

        # Если передана база знаний (или строка kb_text была задана), обновляем чанки
        if combined_kb.strip() or kb_text is not None:
            cursor.execute("DELETE FROM knowledge_chunks WHERE bot_id = ?", (bot_id,))
            for chunk in chunks:
                cursor.execute(
                    "INSERT INTO knowledge_chunks (bot_id, chunk_text) VALUES (?, ?)",
                    (bot_id, chunk)
                )

        cursor.execute(
            """
            SELECT b.id, b.name, b.avatar, b.system_prompt, b.temperature, b.max_tokens, b.created_at, b.user_id,
                   COUNT(k.id) as chunks_count
            FROM bots b
            LEFT JOIN knowledge_chunks k ON b.id = k.bot_id
            WHERE b.id = ?
            GROUP BY b.id
            """,
            (bot_id,)
        )
        row = cursor.fetchone()
        conn.commit()

    return {
        "id": row["id"],
        "name": row["name"],
        "avatar": row["avatar"],
        "system_prompt": row["system_prompt"],
        "temperature": row["temperature"] if row["temperature"] is not None else 0.7,
        "max_tokens": row["max_tokens"] if row["max_tokens"] is not None else DEFAULT_MAX_TOKENS,
        "created_at": row["created_at"],
        "user_id": row["user_id"] or "anon",
        "chunks_count": row["chunks_count"]
    }


# Удаление бота и его базы знаний (только владелец)
@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str, user_id: Optional[str] = None):
    bot_id = bot_id.strip()
    clean_uid = (user_id or "").strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, user_id FROM bots WHERE id = ?", (bot_id,))
        bot = cursor.fetchone()
        if not bot:
            raise HTTPException(status_code=404, detail="Бот не найден")
        if bot_id in STARTER_BOT_IDS or bot["user_id"] == "system":
            raise HTTPException(status_code=403, detail="Системные образцы КС54 нельзя удалить")
        if not clean_uid or bot["user_id"] != clean_uid:
            raise HTTPException(status_code=403, detail="Нельзя удалить чужого бота")

        cursor.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        conn.commit()

    return {"status": "ok", "deleted_id": bot_id, "name": bot["name"]}


# Чат с ботом (со стримингом текста)
@app.post("/api/chat")
async def chat_with_bot(payload: ChatRequest):
    bot_id = payload.bot_id.strip()
    user_message = payload.message.strip()

    if not user_message:
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, avatar, system_prompt, temperature, max_tokens FROM bots WHERE id = ?", (bot_id,))
        bot = cursor.fetchone()
        if not bot:
            raise HTTPException(status_code=404, detail="Бот не найден")

        system_prompt = bot["system_prompt"]
        bot_temp = float(bot["temperature"]) if bot["temperature"] is not None else 0.7
        bot_max_tokens = int(bot["max_tokens"]) if bot["max_tokens"] is not None else DEFAULT_MAX_TOKENS
        if bot_max_tokens <= 0:
            bot_max_tokens = DEFAULT_MAX_TOKENS

        cursor.execute("SELECT chunk_text FROM knowledge_chunks WHERE bot_id = ?", (bot_id,))
        chunk_rows = cursor.fetchall()
        chunks = [r["chunk_text"] for r in chunk_rows]

    # RAG с помощью BM25Okapi
    relevant_chunks = []
    if chunks:
        tokenized_corpus = [tokenize(doc) for doc in chunks]
        valid_items = [(doc, toks) for doc, toks in zip(chunks, tokenized_corpus) if toks]

        if valid_items:
            docs, tokenized_docs = zip(*valid_items)
            bm25 = BM25Okapi(list(tokenized_docs))
            query_tokens = tokenize(user_message)

            if query_tokens:
                scores = bm25.get_scores(query_tokens)
                sorted_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
                top_indices = [i for i in sorted_indices[:3] if scores[i] > 0]
                if top_indices:
                    relevant_chunks = [docs[i] for i in top_indices]
                else:
                    relevant_chunks = list(docs[:2])
            else:
                relevant_chunks = list(docs[:2])

    if relevant_chunks:
        facts_text = "\n---\n".join(relevant_chunks)
        final_system_message = (
            f"{system_prompt}\n\n"
            f"Отвечай опираясь на эти факты из базы знаний (если они относятся к вопросу):\n"
            f"{facts_text}"
        )
    else:
        final_system_message = system_prompt

    messages = [{"role": "system", "content": final_system_message}]

    # Добавляем последние 6 сообщений из истории
    recent_history = payload.history[-6:] if payload.history else []
    for item in recent_history:
        if item.role in ("user", "assistant") and item.content:
            messages.append({"role": item.role, "content": item.content})

    messages.append({"role": "user", "content": user_message})

    async def stream_generator():
        try:
            response_stream = await client.chat.completions.create(
                model="local",
                messages=messages,
                temperature=bot_temp,
                max_tokens=bot_max_tokens,
                stream=True,
            )
            async for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content
        except Exception as e:
            yield f"\n[Ошибка соединения с локальной моделью: {str(e)}]"

    return StreamingResponse(stream_generator(), media_type="text/plain")


class TestDraftRequest(BaseModel):
    message: str
    system_prompt: str
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    kb_text: Optional[str] = None
    history: Optional[List[dict]] = None


@app.post("/api/test-draft")
async def test_draft(payload: TestDraftRequest):
    user_message = payload.message.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")

    system_prompt = payload.system_prompt.strip() or "Ты дружелюбный ИИ-помощник."
    bot_temp = float(payload.temperature if payload.temperature is not None else 0.7)
    bot_max_tokens = int(payload.max_tokens if payload.max_tokens is not None else DEFAULT_MAX_TOKENS)
    bot_max_tokens = min(max(bot_max_tokens, 50), 4096)

    relevant_chunks = []
    if payload.kb_text and payload.kb_text.strip():
        chunks = chunk_text(payload.kb_text.strip())
        if chunks:
            tokenized_corpus = [tokenize(doc) for doc in chunks]
            valid_items = [(doc, toks) for doc, toks in zip(chunks, tokenized_corpus) if toks]
            if valid_items:
                docs, tokenized_docs = zip(*valid_items)
                bm25 = BM25Okapi(list(tokenized_docs))
                query_tokens = tokenize(user_message)
                if query_tokens:
                    scores = bm25.get_scores(query_tokens)
                    sorted_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
                    top_indices = [i for i in sorted_indices[:3] if scores[i] > 0]
                    if top_indices:
                        relevant_chunks = [docs[i] for i in top_indices]
                    else:
                        relevant_chunks = list(docs[:2])
                else:
                    relevant_chunks = list(docs[:2])

    if relevant_chunks:
        facts_text = "\n---\n".join(relevant_chunks)
        final_system_message = (
            f"{system_prompt}\n\n"
            f"Отвечай опираясь на эти факты из базы знаний (если они относятся к вопросу):\n"
            f"{facts_text}"
        )
    else:
        final_system_message = system_prompt

    messages = [
        {"role": "system", "content": final_system_message}
    ]
    if payload.history:
        for h in payload.history[-6:]:
            if isinstance(h, dict) and "role" in h and "content" in h:
                messages.append({"role": str(h["role"]), "content": str(h["content"])})
    messages.append({"role": "user", "content": user_message})

    async def stream_generator():
        try:
            response_stream = await client.chat.completions.create(
                model="local",
                messages=messages,
                temperature=bot_temp,
                max_tokens=bot_max_tokens,
                stream=True,
            )
            async for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content
        except Exception as e:
            yield f"\n[Ошибка соединения: {str(e)}]"

    return StreamingResponse(stream_generator(), media_type="text/plain")


class TaskProgressPayload(BaseModel):
    progress: dict


class CodeTaskRequest(BaseModel):
    task_id: str
    system_prompt: str
    temperature: Optional[float] = 0.2
    kb_text: Optional[str] = None
    current_code: Optional[str] = None


CODE_TASKS = {
    "sum_pos": {
        "title": "1. Сумма положительных чисел",
        "stage": "Разминка",
        "difficulty": "Лёгкая",
        "est_minutes": 2,
        "description": "Функция solve(numbers) принимает список чисел и возвращает сумму всех чисел > 0 (если положительных нет, верни 0).",
        "prompt": "Напиши на Python функцию solve(numbers: list) -> int, которая возвращает сумму всех положительных чисел (> 0) из переданного списка. Если чисел > 0 нет, верни 0. Напиши только рабочий код функции в блоке ```python ... ```.",
        "tests": [
            {"label": "solve([1, -2, 3, 4, -5]) == 8", "call": "solve([1, -2, 3, 4, -5])", "expected": 8},
            {"label": "solve([-1, -2, -3]) == 0", "call": "solve([-1, -2, -3])", "expected": 0},
            {"label": "solve([10, 20]) == 30", "call": "solve([10, 20])", "expected": 30},
            {"label": "solve([]) == 0", "call": "solve([])", "expected": 0},
        ],
    },
    "vowels": {
        "title": "2. Счётчик русских гласных",
        "stage": "Разминка",
        "difficulty": "Лёгкая",
        "est_minutes": 2,
        "description": "Функция count_vowels(text) считает количество гласных русских букв в строке независимо от регистра.",
        "prompt": "Напиши на Python функцию count_vowels(text: str) -> int, которая считает количество русских гласных букв в переданной строке. Напиши только рабочий код функции в блоке ```python ... ```.",
        "tests": [
            {"label": 'count_vowels("Привет") == 2', "call": 'count_vowels("Привет")', "expected": 2},
            {"label": 'count_vowels("Ёжик в тумане") == 5', "call": 'count_vowels("Ёжик в тумане")', "expected": 5},
            {"label": 'count_vowels("КОЛЛЕДЖ 54") == 2', "call": 'count_vowels("КОЛЛЕДЖ 54")', "expected": 2},
            {"label": 'count_vowels("ЭЮЯ ёжик") == 5', "call": 'count_vowels("ЭЮЯ ёжик")', "expected": 5},
            {"label": 'count_vowels("бвгдж") == 0', "call": 'count_vowels("бвгдж")', "expected": 0},
        ],
    },
    "bug_discount": {
        "title": "3. Отладка ИИ: скидка и доставка",
        "stage": "Отладка ИИ",
        "difficulty": "Средняя",
        "est_minutes": 3,
        "mode": "debug",
        "description": "Почини код в редакторе: calc_order(prices, is_vip) суммирует только цены > 0 (пустая корзина -> 0), при is_vip=True даёт скидку 20% (total * 80 // 100), и если итог ПОСЛЕ скидки < 1000 и > 0, добавляет доставку 300 руб (при >= 1000 доставка 0).",
        "prompt": "Исправь ошибки в Python-функции calc_order(prices: list, is_vip: bool) -> int по ТЗ:\n1) Суммируются только положительные цены (> 0); пустой список -> 0 без доставки.\n2) Если is_vip=True, к сумме применяется скидка 20% (total * 80 // 100).\n3) Если итог после скидки > 0 и строго меньше 1000, добавляется доставка 300 руб; при >= 1000 доставка бесплатна.\nВерни только исправленный рабочий код в блоке ```python ... ```.",
        "tests": [
            {"label": "calc_order([500, 700], False) == 1200", "call": "calc_order([500, 700], False)", "expected": 1200},
            {"label": "calc_order([400, 200], False) == 900", "call": "calc_order([400, 200], False)", "expected": 900},
            {"label": "calc_order([1000], False) == 1000", "call": "calc_order([1000], False)", "expected": 1000},
            {"label": "calc_order([], True) == 0", "call": "calc_order([], True)", "expected": 0},
            {"label": "calc_order([500, -100, 600], True) == 1180", "call": "calc_order([500, -100, 600], True)", "expected": 1180},
            {"label": "calc_order([1500], True) == 1200", "call": "calc_order([1500], True)", "expected": 1200},
        ],
    },
    "bug_grades": {
        "title": "4. Отладка ИИ: балл с пересдачами",
        "stage": "Отладка ИИ",
        "difficulty": "Средняя",
        "est_minutes": 4,
        "mode": "debug",
        "description": "Почини код в редакторе: calc_gpa(record) разбирает строку оценок через запятую ('5, 2, 4*, н'). 'н' (в любом регистре) и пустые элементы игнорируются; оценка со звёздочкой (4*) — пересдача, она заменяет предыдущую числовую оценку; при отсутствии оценок вернуть 0.0; иначе среднее, округлённое до 2 знаков.",
        "prompt": "Исправь ошибки в Python-функции calc_gpa(record: str) -> float по ТЗ:\n- На вход подаётся строка оценок через запятую (например '2, н, 4*, 5').\n- 'н'/'Н' и пустые элементы пропускаются.\n- Оценка со звёздочкой (например '4*') заменяет предыдущую оценку (или добавляется, если она первая).\n- Если оценок нет, вернуть 0.0, иначе round(sum/len, 2).\nВерни только исправленный рабочий код в блоке ```python ... ```.",
        "tests": [
            {"label": 'calc_gpa("5, 4, 3") == 4.0', "call": 'calc_gpa("5, 4, 3")', "expected": 4.0},
            {"label": 'calc_gpa("4, 5, 4, 5") == 4.5', "call": 'calc_gpa("4, 5, 4, 5")', "expected": 4.5},
            {"label": 'calc_gpa("2, 5*, 4") == 4.5', "call": 'calc_gpa("2, 5*, 4")', "expected": 4.5},
            {"label": 'calc_gpa("2, н, 4*, 5") == 4.5', "call": 'calc_gpa("2, н, 4*, 5")', "expected": 4.5},
            {"label": 'calc_gpa("Н, , 5*") == 5.0', "call": 'calc_gpa("Н, , 5*")', "expected": 5.0},
            {"label": 'calc_gpa("") == 0.0', "call": 'calc_gpa("")', "expected": 0.0},
            {"label": 'calc_gpa("н, Н") == 0.0', "call": 'calc_gpa("н, Н")', "expected": 0.0},
        ],
    },
    "safe_json": {
        "title": "5. Парсер грязного JSON от LLM",
        "stage": "Хардкор",
        "difficulty": "Продвинутая",
        "est_minutes": 4,
        "mode": "hard",
        "description": "Функция safe_parse_llm_json(raw) извлекает первый валидный JSON-объект (dict) из произвольного ответа нейросети: вырезает обёртку ```json ... ``` и окружающий текст, убирает висячие запятые перед } или ], и возвращает None (без исключений), если валидного JSON-объекта нет или пришёл массив/мусор.",
        "prompt": "Напиши на Python функцию safe_parse_llm_json(raw: str), которая извлекает валидный JSON-объект (dict) из ответа LLM и возвращает его (или None, если извлечь словарь нельзя).\nТребования:\n- Учитывай лишний текст вокруг и markdown-блоки.\n- Учитывай висячие запятые перед закрывающими скобками.\n- Если в тексте до валидного JSON есть случайная фигурная скобка {мусор, функция не должна ломаться.\n- Если результат не dict (например список) или текст битый — верни None без исключений.\nНапиши только рабочий код в блоке ```python ... ```.",
        "tests": [
            {"label": 'safe_parse_llm_json(\'{"status": "ok", "count": 3}\')', "call": 'safe_parse_llm_json(\'{"status": "ok", "count": 3}\')', "expected": {"status": "ok", "count": 3}},
            {"label": 'markdown ```json ... ```', "call": 'safe_parse_llm_json(\'Вот ответ:\\n```json\\n{"name": "Bot", "active": true}\\n```\\nУдачи!\')', "expected": {"name": "Bot", "active": True}},
            {"label": 'trailing commas {"items": [1, 2,], "ok": true,}', "call": 'safe_parse_llm_json(\'Результат: {"items": [1, 2,], "ok": true,}\')', "expected": {"items": [1, 2], "ok": True}},
            {"label": 'нет JSON -> None', "call": 'safe_parse_llm_json("Не смог сформировать JSON.")', "expected": None},
            {"label": 'битый {"broken": 123 -> None', "call": 'safe_parse_llm_json(\'{"broken": 123\')', "expected": None},
            {"label": 'массив [1, 2, 3] -> None', "call": 'safe_parse_llm_json("[1, 2, 3]")', "expected": None},
            {"label": 'лишняя { перед {"id": 42, "ok": true}', "call": 'safe_parse_llm_json(\'Текст с {ошибкой внутри, потом {"id": 42, "ok": true}\')', "expected": {"id": 42, "ok": True}},
        ],
    },
    "robot_undo": {
        "title": "6. Робот с UNDO и границами поля",
        "stage": "Хардкор",
        "difficulty": "Продвинутая",
        "est_minutes": 5,
        "mode": "hard",
        "description": "Робот стартует в [0, 0] на сетке 0..10 по X и 0..10 по Y. Функция run_robot(commands) выполняет команды через ';': UP n (+y), DOWN n (-y), RIGHT n (+x), LEFT n (-x), RESET (в [0, 0]), UNDO (отмена последнего шага с возвратом точных координат до него). Координаты всегда зажаты в [0, 10]. Верни список [x, y].",
        "prompt": "Напиши на Python функцию run_robot(commands: str) -> list.\nПравила:\n- Старт в [0, 0], допустимые координаты 0 <= x <= 10 и 0 <= y <= 10 (при выходе за границу значение зажимается).\n- Команды разделяются ';', регистр не важен, пустые игнорируются: UP n (+y), DOWN n (-y), RIGHT n (+x), LEFT n (-x), RESET (переход в [0, 0]), UNDO (возврат к точным координатам до предыдущей выполненной команды).\n- Верни итоговый список [x, y].\nНапиши только рабочий код в блоке ```python ... ```.",
        "tests": [
            {"label": 'run_robot("UP 3; RIGHT 2") == [2, 3]', "call": 'run_robot("UP 3; RIGHT 2")', "expected": [2, 3]},
            {"label": 'run_robot("RIGHT 15; DOWN 4") == [10, 0]', "call": 'run_robot("RIGHT 15; DOWN 4")', "expected": [10, 0]},
            {"label": 'run_robot("RIGHT 8; RIGHT 5; UNDO") == [8, 0]', "call": 'run_robot("RIGHT 8; RIGHT 5; UNDO")', "expected": [8, 0]},
            {"label": 'run_robot("UP 4; RIGHT 3; UNDO; UNDO; UNDO") == [0, 0]', "call": 'run_robot("UP 4; RIGHT 3; UNDO; UNDO; UNDO")', "expected": [0, 0]},
            {"label": 'run_robot("up 5 ; right 2 ; RESET ; UNDO") == [2, 5]', "call": 'run_robot("up 5 ; right 2 ; RESET ; UNDO")', "expected": [2, 5]},
            {"label": 'run_robot("") == [0, 0]', "call": 'run_robot("")', "expected": [0, 0]},
        ],
    },
}


def extract_python_code(answer: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n?(.*?)```", answer, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    unclosed = re.search(r"```(?:python|py)?\s*\n(.*)", answer, re.DOTALL | re.IGNORECASE)
    if unclosed:
        return unclosed.group(1).strip()
    return answer.strip()


class RunCodeTaskRequest(BaseModel):
    task_id: str
    code: str


def run_python_tests_sandbox(code: str, tests_spec: list) -> dict:
    import subprocess
    import sys

    runner_script = (
        "import json, io, contextlib\n"
        f"USER_CODE = json.loads({json.dumps(json.dumps(code, ensure_ascii=False))})\n"
        f"TESTS = json.loads({json.dumps(json.dumps(tests_spec, ensure_ascii=False))})\n"
        "results = []\n"
        "ns = {}\n"
        "compile_err = ''\n"
        "out_chunks = []\n"
        "try:\n"
        "    buf = io.StringIO()\n"
        "    with contextlib.redirect_stdout(buf):\n"
        "        exec(compile(USER_CODE, '<code_editor>', 'exec'), ns, ns)\n"
        "    if buf.getvalue(): out_chunks.append(buf.getvalue())\n"
        "except Exception as e:\n"
        "    compile_err = f'{type(e).__name__}: {e}'\n"
        "for t in TESTS:\n"
        "    if compile_err:\n"
        "        results.append({'label': t['label'], 'passed': False, 'actual': '', 'expected': repr(t['expected']), 'error': compile_err})\n"
        "        continue\n"
        "    try:\n"
        "        buf = io.StringIO()\n"
        "        with contextlib.redirect_stdout(buf):\n"
        "            actual = eval(t['call'], ns, ns)\n"
        "        if buf.getvalue(): out_chunks.append(buf.getvalue())\n"
        "        exp = t['expected']\n"
        "        ok = (actual == exp) and (type(actual) is type(exp))\n"
        "        results.append({'label': t['label'], 'passed': ok, 'actual': repr(actual), 'expected': repr(exp), 'error': '' if ok else f'Получено {repr(actual)}, ожидалось {repr(exp)}'})\n"
        "    except Exception as e:\n"
        "        results.append({'label': t['label'], 'passed': False, 'actual': '', 'expected': repr(t['expected']), 'error': f'{type(e).__name__}: {e}'})\n"
        "print('__CODE_TEST_JSON__:' + json.dumps({'compile_error': compile_err, 'results': results, 'stdout': ''.join(out_chunks)[:2000]}, ensure_ascii=False))\n"
    )

    passed = False
    passed_count = 0
    err_msg = ""
    stdout_text = ""
    tests_results = []

    try:
        proc = subprocess.run(
            [sys.executable, "-c", runner_script],
            capture_output=True,
            text=True,
            timeout=4,
        )
        marker_line = None
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith("__CODE_TEST_JSON__:"):
                marker_line = line[len("__CODE_TEST_JSON__:"):]
                break
        if marker_line:
            parsed = json.loads(marker_line)
            tests_results = parsed.get("results", [])
            err_msg = parsed.get("compile_error", "")
            stdout_text = parsed.get("stdout", "")
            passed_count = sum(1 for r in tests_results if r.get("passed"))
            passed = len(tests_results) == len(tests_spec) and passed_count == len(tests_spec)
            if not passed and not err_msg:
                first_err = next((r.get("error") for r in tests_results if not r.get("passed") and r.get("error")), "")
                err_msg = first_err or "Не все unit-тесты пройдены"
        else:
            raw_err = (proc.stderr or proc.stdout or "").strip()
            err_lines = [l for l in raw_err.splitlines() if l.strip()]
            err_msg = err_lines[-1] if err_lines else "Ошибка при выполнении unit-тестов"
            tests_results = [
                {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": err_msg}
                for t in tests_spec
            ]
    except subprocess.TimeoutExpired:
        err_msg = "Превышено время выполнения (таймаут 4 сек). Возможно, в коде бесконечный цикл."
        tests_results = [
            {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": err_msg}
            for t in tests_spec
        ]
    except Exception as ex:
        err_msg = f"Системная ошибка запуска: {str(ex)}"
        tests_results = [
            {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": err_msg}
            for t in tests_spec
        ]

    return {
        "passed": passed,
        "passed_count": passed_count,
        "total_count": len(tests_spec),
        "error": err_msg,
        "stdout": stdout_text,
        "tests_results": tests_results,
    }


@app.post("/api/run-code-task")
async def run_code_task(payload: RunCodeTaskRequest):
    task = CODE_TASKS.get(payload.task_id) or CODE_TASKS["sum_pos"]
    tests_spec = task["tests"]
    tests_labels = [t["label"] for t in tests_spec]
    code = (payload.code or "").strip()
    if not code:
        return {
            "ok": True,
            "task_id": payload.task_id,
            "title": task["title"],
            "difficulty": task["difficulty"],
            "est_minutes": task["est_minutes"],
            "description": task["description"],
            "extracted_code": "",
            "passed": False,
            "passed_count": 0,
            "total_count": len(tests_spec),
            "error": "Редактор кода пуст. Напишите или сгенерируйте функцию на Python.",
            "stdout": "",
            "tests_labels": tests_labels,
            "tests_results": [
                {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": "Код отсутствует"}
                for t in tests_spec
            ],
        }

    run_res = run_python_tests_sandbox(code, tests_spec)
    return {
        "ok": True,
        "task_id": payload.task_id,
        "title": task["title"],
        "difficulty": task["difficulty"],
        "est_minutes": task["est_minutes"],
        "description": task["description"],
        "extracted_code": code,
        "passed": run_res["passed"],
        "passed_count": run_res["passed_count"],
        "total_count": run_res["total_count"],
        "error": run_res["error"],
        "stdout": run_res["stdout"],
        "tests_labels": tests_labels,
        "tests_results": run_res["tests_results"],
    }


@app.post("/api/verify-code-task")
async def verify_code_task(payload: CodeTaskRequest):
    task = CODE_TASKS.get(payload.task_id) or CODE_TASKS["sum_pos"]
    tests_spec = task["tests"]
    tests_labels = [t["label"] for t in tests_spec]

    sys_prompt = payload.system_prompt.strip() or "Ты опытный Python-разработчик."
    if payload.kb_text and payload.kb_text.strip():
        sys_prompt += f"\n\nФакты из базы знаний:\n{payload.kb_text.strip()}"

    temp = float(payload.temperature if payload.temperature is not None else 0.2)

    user_prompt = task["prompt"]
    if task.get("mode") == "debug" and payload.current_code and payload.current_code.strip():
        user_prompt = f"{user_prompt}\n\nТекущий код в редакторе:\n```python\n{payload.current_code.strip()}\n```"

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response = await client.chat.completions.create(
            model="qwen2.5-3b-instruct",
            messages=messages,
            temperature=temp,
            max_tokens=DEFAULT_MAX_TOKENS,
            stream=False,
        )
        answer = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return {
            "ok": False,
            "task_id": payload.task_id,
            "title": task["title"],
            "difficulty": task["difficulty"],
            "est_minutes": task["est_minutes"],
            "description": task["description"],
            "raw_answer": f"[Ошибка соединения с моделью: {str(e)}]",
            "extracted_code": "",
            "passed": False,
            "passed_count": 0,
            "total_count": len(tests_spec),
            "error": f"Ошибка вызова нейросети: {str(e)}",
            "stdout": "",
            "tests_labels": tests_labels,
            "tests_results": [
                {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": "Нет ответа от модели"}
                for t in tests_spec
            ],
        }

    code = extract_python_code(answer)
    run_res = run_python_tests_sandbox(code, tests_spec)

    return {
        "ok": True,
        "task_id": payload.task_id,
        "title": task["title"],
        "difficulty": task["difficulty"],
        "est_minutes": task["est_minutes"],
        "description": task["description"],
        "raw_answer": answer,
        "extracted_code": code,
        "passed": run_res["passed"],
        "passed_count": run_res["passed_count"],
        "total_count": run_res["total_count"],
        "error": run_res["error"],
        "stdout": run_res["stdout"],
        "tests_labels": tests_labels,
        "tests_results": run_res["tests_results"],
    }


@app.get("/api/workstation")
async def get_or_assign_workstation(request: Request, user_id: Optional[str] = None):
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client_ip = forwarded or ((request.client.host if request.client else "") or "unknown")
    clean_uid = (user_id or "").strip()

    if client_ip in ("127.0.0.1", "::1", "localhost") and clean_uid:
        client_key = f"local:{clean_uid}"
    else:
        client_key = f"ip:{client_ip}"

    with get_db() as conn:
        row = conn.execute(
            "SELECT pc_number, pc_name, user_id FROM workstations WHERE client_key = ?",
            (client_key,),
        ).fetchone()
        if row:
            return {
                "pc_number": row["pc_number"],
                "pc_name": row["pc_name"],
                "user_id": row["user_id"],
                "client_ip": client_ip,
                "locked": True,
            }

        max_row = conn.execute("SELECT COALESCE(MAX(pc_number), 0) FROM workstations").fetchone()
        next_num = int(max_row[0]) + 1
        pc_name = f"ПК-{next_num:02d}"

        assigned_uid = clean_uid
        if not assigned_uid:
            assigned_uid = f"ws_{next_num:02d}_{secrets.token_hex(3)}"
        else:
            existing_uid = conn.execute(
                "SELECT 1 FROM workstations WHERE user_id = ?",
                (assigned_uid,),
            ).fetchone()
            if existing_uid:
                assigned_uid = f"ws_{next_num:02d}_{secrets.token_hex(3)}"

        conn.execute(
            "INSERT INTO workstations (client_key, pc_number, pc_name, user_id) VALUES (?, ?, ?, ?)",
            (client_key, next_num, pc_name, assigned_uid),
        )
        conn.commit()

    return {
        "pc_number": next_num,
        "pc_name": pc_name,
        "user_id": assigned_uid,
        "client_ip": client_ip,
        "locked": True,
    }


@app.get("/api/progress/{user_id}")
async def get_task_progress(user_id: str):
    clean_uid = user_id.strip() or "user_guest"
    with get_db() as conn:
        row = conn.execute(
            "SELECT progress_json, updated_at FROM user_task_progress WHERE user_id = ?",
            (clean_uid,),
        ).fetchone()
    if not row:
        return {"user_id": clean_uid, "progress": None}
    try:
        data = json.loads(row[0])
    except Exception:
        data = None
    return {"user_id": clean_uid, "progress": data, "updated_at": row[1]}


@app.put("/api/progress/{user_id}")
async def save_task_progress(user_id: str, payload: TaskProgressPayload):
    clean_uid = user_id.strip() or "user_guest"
    serialized = json.dumps(payload.progress, ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_task_progress (user_id, progress_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                progress_json = excluded.progress_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (clean_uid, serialized),
        )
        conn.commit()
    return {"status": "ok", "user_id": clean_uid}
