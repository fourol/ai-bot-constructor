import os
import re
import io
import json
import time
import secrets
import string
import sqlite3
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
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
STATIC_TEACHER_PATH = os.path.join(os.path.dirname(__file__), "teacher.html")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
LOGS_DIR = os.getenv("LOGS_DIR", os.path.join(os.path.dirname(__file__), "logs"))
TEACHER_KEY = os.getenv("TEACHER_KEY", "").strip()
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id TEXT,
                pc_name TEXT,
                bot_id TEXT,
                status TEXT NOT NULL DEFAULT 'ok',
                latency_ms INTEGER,
                detail TEXT,
                meta_json TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_user ON activity_logs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_logs_type ON activity_logs(event_type)")

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


def resolve_pc_name(conn, user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    row = conn.execute(
        "SELECT pc_name FROM workstations WHERE user_id = ? LIMIT 1",
        (user_id.strip(),),
    ).fetchone()
    return row["pc_name"] if row else None


def log_activity(
    event_type: str,
    user_id: Optional[str] = None,
    pc_name: Optional[str] = None,
    bot_id: Optional[str] = None,
    status: str = "ok",
    latency_ms: Optional[int] = None,
    detail: str = "",
    meta: Optional[dict] = None,
):
    """Записывает событие занятия в SQLite (activity_logs) и дневной JSONL-файл."""
    try:
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        clean_uid = (user_id or "").strip() or None
        clean_bot = (bot_id or "").strip() or None
        clean_detail = (detail or "").strip()[:300]
        meta_str = json.dumps(meta or {}, ensure_ascii=False)

        with get_db() as conn:
            resolved_pc = pc_name or resolve_pc_name(conn, clean_uid)
            conn.execute(
                """
                INSERT INTO activity_logs (ts, event_type, user_id, pc_name, bot_id, status, latency_ms, detail, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, event_type, clean_uid, resolved_pc, clean_bot, status, latency_ms, clean_detail, meta_str),
            )
            conn.commit()

        record = {
            "ts": ts,
            "event_type": event_type,
            "user_id": clean_uid,
            "pc_name": resolved_pc,
            "bot_id": clean_bot,
            "status": status,
            "latency_ms": latency_ms,
            "detail": clean_detail,
            "meta": meta or {},
        }
        log_path = os.path.join(LOGS_DIR, f"session_{now.strftime('%Y-%m-%d')}.jsonl")
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def require_teacher_access(request: Request, key: Optional[str] = None):
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return
    if TEACHER_KEY and key and secrets.compare_digest(key.strip(), TEACHER_KEY):
        return
    raise HTTPException(
        status_code=403,
        detail="Панель преподавателя доступна только с компьютера сервера (127.0.0.1) или с TEACHER_KEY",
    )


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
    user_id: Optional[str] = None


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

    log_activity(
        "bot_created",
        user_id=clean_user_id,
        bot_id=bot_id,
        detail=f"Создан бот «{name}»",
        meta={"name": name, "temp": float(temperature if temperature is not None else 0.7), "max_tokens": int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS), "chunks": len(chunks)},
    )

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

    log_activity(
        "bot_updated",
        user_id=row["user_id"] or clean_uid,
        bot_id=bot_id,
        detail=f"Обновлён бот «{name}»",
        meta={"name": name, "temp": float(temperature if temperature is not None else 0.7), "max_tokens": int(max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS), "chunks": row["chunks_count"]},
    )

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

    log_activity(
        "bot_deleted",
        user_id=clean_uid,
        bot_id=bot_id,
        detail=f"Удалён бот «{bot['name']}»",
    )

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
        cursor.execute("SELECT user_id, name, avatar, system_prompt, temperature, max_tokens FROM bots WHERE id = ?", (bot_id,))
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

    chat_uid = (payload.user_id or "").strip() or bot["user_id"]

    async def stream_generator():
        t0 = time.perf_counter()
        total_chars = 0
        err_str = ""
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
                        total_chars += len(delta.content)
                        yield delta.content
        except Exception as e:
            err_str = str(e)
            yield f"\n[Ошибка соединения с локальной моделью: {err_str}]"
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log_activity(
                "chat_message",
                user_id=chat_uid,
                bot_id=bot_id,
                status="error" if err_str else "ok",
                latency_ms=latency_ms,
                detail=user_message[:140],
                meta={
                    "bot_name": bot["name"],
                    "answer_len": total_chars,
                    "rag_chunks": len(relevant_chunks),
                    "error": err_str or None,
                },
            )

    return StreamingResponse(stream_generator(), media_type="text/plain")


class TestDraftRequest(BaseModel):
    message: str
    system_prompt: str
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    kb_text: Optional[str] = None
    history: Optional[List[dict]] = None
    user_id: Optional[str] = None


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

    draft_uid = (payload.user_id or "").strip() or None

    async def stream_generator():
        t0 = time.perf_counter()
        total_chars = 0
        err_str = ""
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
                        total_chars += len(delta.content)
                        yield delta.content
        except Exception as e:
            err_str = str(e)
            yield f"\n[Ошибка соединения: {err_str}]"
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log_activity(
                "draft_test",
                user_id=draft_uid,
                status="error" if err_str else "ok",
                latency_ms=latency_ms,
                detail=user_message[:140],
                meta={
                    "answer_len": total_chars,
                    "rag_chunks": len(relevant_chunks),
                    "error": err_str or None,
                },
            )

    return StreamingResponse(stream_generator(), media_type="text/plain")


class TaskProgressPayload(BaseModel):
    progress: dict


class RunCodeLiveAiRequest(BaseModel):
    task_id: str
    code: str
    system_prompt: Optional[str] = ""
    temperature: Optional[float] = 0.2
    kb_text: Optional[str] = None
    user_id: Optional[str] = None


CODE_TASKS = {
    "ai_router": {
        "title": "1. Маршрутизатор тикетов (классификатор ИИ)",
        "stage": "Интеграция ИИ • База",
        "difficulty": "Лёгкая",
        "est_minutes": 3,
        "description": 'Функция route_ticket(message) вызывает ask_ai(...), нормализует ответ модели (пробелы, регистр, знаки .!?) и возвращает "billing", "tech" или "other". Если модель ответила нестандартно — вернуть "other".',
        "live_call": 'route_ticket("Здравствуйте, у меня два раза списалась оплата за подписку")',
        "tests": [
            {
                "label": 'route_ticket("Списали дважды") ["  BILLING.\\n"] -> "billing"',
                "call": 'route_ticket("Списали деньги дважды за подписку")',
                "expected": "billing",
                "mock_ai": "  BILLING.\n",
                "min_ai_calls": 1,
            },
            {
                "label": 'route_ticket("Сервер упал") ["Tech!"] -> "tech"',
                "call": 'route_ticket("Не запускается сервер после обновления")',
                "expected": "tech",
                "mock_ai": "Tech!",
                "min_ai_calls": 1,
            },
            {
                "label": 'route_ticket("Общий вопрос") ["other"] -> "other"',
                "call": 'route_ticket("Здравствуйте, расскажите о компании")',
                "expected": "other",
                "mock_ai": "other",
                "min_ai_calls": 1,
            },
            {
                "label": 'Нестандартный ответ модели -> fallback "other"',
                "call": 'route_ticket("Сложный непонятный запрос")',
                "expected": "other",
                "mock_ai": "Извините, я не уверен в категории.",
                "min_ai_calls": 1,
            },
            {
                "label": 'Пустой ответ модели ["   "] -> "other"',
                "call": 'route_ticket("???")',
                "expected": "other",
                "mock_ai": "   ",
            },
        ],
    },
    "ai_rag": {
        "title": "2. Мини-RAG (поиск контекста до вызова ИИ)",
        "stage": "Интеграция ИИ • RAG",
        "difficulty": "Лёгкая",
        "est_minutes": 4,
        "description": 'Функция answer_with_rag(question, docs) выбирает из списка docs фрагменты, где есть ключевые слова вопроса (длиной >= 3 символов). Если совпадений нет — возвращает "Нет данных в базе" БЕЗ вызова ИИ. Иначе передаёт найденный контекст + вопрос в ask_ai(...) и возвращает ответ модели.',
        "live_call": 'answer_with_rag("Во сколько открывается колледж?", ["Столовая работает с 10:00.", "Колледж открывается в 08:30 утра."])',
        "tests": [
            {
                "label": 'Находит нужный документ про колледж и передаёт в ask_ai',
                "call": 'answer_with_rag("Во сколько открывается колледж?", ["Столовая работает с 10:00.", "Колледж открывается в 08:30 утра."])',
                "expected": "Колледж открывается в 08:30.",
                "mock_ai": "Колледж открывается в 08:30.",
                "min_ai_calls": 1,
                "prompt_must_contain": ["08:30"],
                "prompt_must_not_contain": ["Столовая"],
            },
            {
                "label": 'Находит документ с паролем Wi-Fi и вызывает ask_ai',
                "call": 'answer_with_rag("Какой пароль от Wi-Fi аудитории?", ["Обед в 13:00.", "Пароль от Wi-Fi аудитории: KS54_Guest."])',
                "expected": "Пароль: KS54_Guest.",
                "mock_ai": "Пароль: KS54_Guest.",
                "min_ai_calls": 1,
                "prompt_must_contain": ["KS54_Guest"],
                "prompt_must_not_contain": ["Обед"],
            },
            {
                "label": 'Если релевантных документов нет -> "Нет данных в базе" без вызова ask_ai',
                "call": 'answer_with_rag("Сколько стоит билет на Марс?", ["Колледж открывается в 08:30.", "Столовая работает с 10:00."])',
                "expected": "Нет данных в базе",
                "mock_ai": "Стоит миллион рублей!",
                "max_ai_calls": 0,
            },
            {
                "label": 'Пустая база знаний [] -> "Нет данных в базе" без вызова ask_ai',
                "call": 'answer_with_rag("Когда каникулы?", [])',
                "expected": "Нет данных в базе",
                "mock_ai": "Летом",
                "max_ai_calls": 0,
            },
        ],
    },
    "ai_privacy": {
        "title": "3. Маскировка персональных данных (Privacy Guard)",
        "stage": "Безопасность ИИ",
        "difficulty": "Средняя",
        "est_minutes": 4,
        "description": 'Функция safe_ai_support(user_text) скрывает перед отправкой в ask_ai(...) телефоны на [PHONE] и email на [EMAIL], чтобы личные данные не утекли в ИИ, получает ответ ИИ и подставляет сохранённые телефон/email обратно пользователю.',
        "live_call": 'safe_ai_support("Позвоните клиенту на +7-999-111-22-33 или напишите на student@ks54.ru")',
        "tests": [
            {
                "label": 'Телефон скрыт от ask_ai как [PHONE] и возвращён клиенту',
                "call": 'safe_ai_support("Позвоните на +7-999-111-22-33 по заказу")',
                "expected": "Заявка принята, оператор свяжется по +7-999-111-22-33.",
                "mock_ai": "Заявка принята, оператор свяжется по [PHONE].",
                "min_ai_calls": 1,
                "prompt_must_contain": ["[PHONE]"],
                "prompt_must_not_contain": ["+7-999-111-22-33", "999-111-22-33"],
            },
            {
                "label": 'Email скрыт от ask_ai как [EMAIL] и восстановлен в ответе',
                "call": 'safe_ai_support("Чек отправьте на student@ks54.ru пожалуйста")',
                "expected": "Чек выслан на student@ks54.ru.",
                "mock_ai": "Чек выслан на [EMAIL].",
                "min_ai_calls": 1,
                "prompt_must_contain": ["[EMAIL]"],
                "prompt_must_not_contain": ["student@ks54.ru"],
            },
            {
                "label": 'Одновременно скрыты и телефон, и email',
                "call": 'safe_ai_support("Контакты: ivan@test.org и +79165554433")',
                "expected": "Проверьте ivan@test.org и +79165554433.",
                "mock_ai": "Проверьте [EMAIL] и [PHONE].",
                "min_ai_calls": 1,
                "prompt_must_contain": ["[EMAIL]", "[PHONE]"],
                "prompt_must_not_contain": ["ivan@test.org", "9165554433"],
            },
            {
                "label": 'Текст без персональных данных проходит без искажений',
                "call": 'safe_ai_support("Где находится кабинет 204?")',
                "expected": "На втором этаже.",
                "mock_ai": "На втором этаже.",
                "min_ai_calls": 1,
            },
        ],
    },
    "ai_retry": {
        "title": "4. Отладка AI-пайплайна (Retry + Fallback)",
        "stage": "Отладка ИИ",
        "difficulty": "Средняя",
        "est_minutes": 4,
        "mode": "debug",
        "description": 'Почини код в редакторе: ask_json_with_retry(prompt, max_attempts=3) должен делать до max_attempts попыток вызова ask_ai(...), очищать markdown ```json ... ```, при ошибке парсинга повторять запрос с подсказкой про JSON и вернуть None (без падения), если все попытки провалились.',
        "live_call": 'ask_json_with_retry("Верни JSON с полями status и ok: {\\\"status\\\": \\\"ready\\\", \\\"ok\\\": true}", 3)',
        "tests": [
            {
                "label": 'Валидный JSON с 1-й попытки',
                "call": 'ask_json_with_retry("Дай статус")',
                "expected": {"status": "ok", "count": 2},
                "mock_ai": ['{"status": "ok", "count": 2}'],
                "min_ai_calls": 1,
            },
            {
                "label": '1-я попытка словами -> повтор и снятие ```json ... ``` со 2-й',
                "call": 'ask_json_with_retry("Создай профиль")',
                "expected": {"user": "Alex", "role": "dev"},
                "mock_ai": ['Извините, вот ответ текстом', '```json\n{"user": "Alex", "role": "dev"}\n```'],
                "min_ai_calls": 2,
                "max_ai_calls": 2,
            },
            {
                "label": 'Успех на 3-й попытке после 2 плохих ответов',
                "call": 'ask_json_with_retry("Парсинг", 3)',
                "expected": {"step": 3},
                "mock_ai": ['ошибка 1', 'ошибка 2', '{"step": 3}'],
                "min_ai_calls": 3,
                "max_ai_calls": 3,
            },
            {
                "label": 'Лимит max_attempts=2 исчерпан -> безопасный None (без 3-го вызова)',
                "call": 'ask_json_with_retry("Сломайся безопасно", 2)',
                "expected": None,
                "mock_ai": ['не json', 'совсем не json', '{"never": 1}'],
                "min_ai_calls": 2,
                "max_ai_calls": 2,
            },
            {
                "label": 'Пустые ответы от ИИ -> безопасный None',
                "call": 'ask_json_with_retry("Пусто", 3)',
                "expected": None,
                "mock_ai": ['', '   ', 'nope'],
                "max_ai_calls": 3,
            },
        ],
    },
    "ai_structured": {
        "title": "5. Парсер + валидатор Structured Output",
        "stage": "Хардкор ИИ",
        "difficulty": "Продвинутая",
        "est_minutes": 4,
        "mode": "hard",
        "description": 'Функция parse_order_from_ai(user_query) вызывает ask_ai(user_query), извлекает первый валидный JSON-объект (dict) даже с ```json, текстом вокруг, висячими запятыми и лишней { до объекта, и проверяет схему: "item" — непустая str, "qty" — int (не bool!) > 0. Иначе возвращает None.',
        "live_call": 'parse_order_from_ai("Оформи заказ: 2 ноутбука. Верни только JSON {\\\"item\\\": ..., \\\"qty\\\": число}")',
        "tests": [
            {
                "label": 'Чистый валидный JSON заказа',
                "call": 'parse_order_from_ai("Закажи 3 ноутбука")',
                "expected": {"item": "ноутбук", "qty": 3},
                "mock_ai": '{"item": "ноутбук", "qty": 3}',
                "min_ai_calls": 1,
            },
            {
                "label": 'Markdown ```json + trailing comma в конце',
                "call": 'parse_order_from_ai("Нужно 2 мышки")',
                "expected": {"item": "мышка", "qty": 2},
                "mock_ai": 'Вот ваш заказ:\n```json\n{"item": "мышка", "qty": 2,}\n```\nСпасибо!',
                "min_ai_calls": 1,
            },
            {
                "label": 'Лишняя фигурная скобка {мусор перед реальным JSON',
                "call": 'parse_order_from_ai("5 кабелей")',
                "expected": {"item": "кабель", "qty": 5},
                "mock_ai": 'Лог {ошибка внутри}, итого: {"item": "кабель", "qty": 5}',
                "min_ai_calls": 1,
            },
            {
                "label": 'Неверный тип qty="2" (строка вместо int) -> None',
                "call": 'parse_order_from_ai("Ошибка типа")',
                "expected": None,
                "mock_ai": '{"item": "монитор", "qty": "2"}',
            },
            {
                "label": 'Ловушка Python: qty=true (bool) -> None',
                "call": 'parse_order_from_ai("Bool вместо числа")',
                "expected": None,
                "mock_ai": '{"item": "монитор", "qty": true}',
            },
            {
                "label": 'Пустой item или qty <= 0 -> None',
                "call": 'parse_order_from_ai("Ноль штук")',
                "expected": None,
                "mock_ai": '{"item": "  ", "qty": 0}',
            },
            {
                "label": 'Список [...] вместо словаря -> None',
                "call": 'parse_order_from_ai("Массив")',
                "expected": None,
                "mock_ai": '["мышка", "ноутбук"]',
            },
        ],
    },
    "ai_agent": {
        "title": "6. Мини-агент с калькулятором (Tool-Calling Loop)",
        "stage": "Хардкор ИИ",
        "difficulty": "Продвинутая",
        "est_minutes": 5,
        "mode": "hard",
        "description": 'Функция run_math_agent(question, max_steps=4) в цикле обращается к ask_ai(...). Если ИИ вернул "CALC: <выражение>", код вычисляет выражение и отправляет результат обратно в ask_ai(...). Когда ИИ отвечает "FINAL: <ответ>", возвращает текст после FINAL:. При исчерпании шагов — "Лимит шагов".',
        "live_call": 'run_math_agent("Сколько будет 12 * 4 + 2? Если нужно посчитать, напиши CALC: 12 * 4 + 2, иначе FINAL: ответ")',
        "tests": [
            {
                "label": '1 вызов CALC -> передача 50 обратно в ask_ai -> FINAL',
                "call": 'run_math_agent("Сколько будет 12 * 4 + 2?")',
                "expected": "Ответ: 50",
                "mock_ai": ["CALC: 12 * 4 + 2", "FINAL: Ответ: 50"],
                "min_ai_calls": 2,
                "max_ai_calls": 2,
                "prompt_must_contain": ["50"],
            },
            {
                "label": 'Цепочка из 2 вызовов CALC (75 -> 240) -> FINAL',
                "call": 'run_math_agent("Посчитай (100 - 25) * 3 + 15")',
                "expected": "240",
                "mock_ai": ["CALC: 100 - 25", "CALC: 75 * 3 + 15", "FINAL: 240"],
                "min_ai_calls": 3,
                "max_ai_calls": 3,
                "prompt_must_contain": ["75", "240"],
            },
            {
                "label": 'Прямой ответ FINAL с первого шага',
                "call": 'run_math_agent("Столица России?")',
                "expected": "Москва",
                "mock_ai": ["FINAL: Москва"],
                "min_ai_calls": 1,
                "max_ai_calls": 1,
            },
            {
                "label": 'Защита от зацикливания (max_steps=2) -> "Лимит шагов"',
                "call": 'run_math_agent("Бесконечный агент", 2)',
                "expected": "Лимит шагов",
                "mock_ai": ["CALC: 2 + 2", "CALC: 3 + 3", "FINAL: поздно"],
                "min_ai_calls": 2,
                "max_ai_calls": 2,
            },
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
    user_id: Optional[str] = None


def run_python_tests_sandbox(code: str, tests_spec: list) -> dict:
    import subprocess
    import sys

    runner_script = (
        "import json, io, contextlib\n"
        f"USER_CODE = json.loads({json.dumps(json.dumps(code, ensure_ascii=False))})\n"
        f"TESTS = json.loads({json.dumps(json.dumps(tests_spec, ensure_ascii=False))})\n"
        "results = []\n"
        "_ai_state = {'mock_spec': None, 'calls': [], 'idx': 0}\n"
        "def ask_ai(prompt, system='', temperature=0.2):\n"
        "    _ai_state['calls'].append({'prompt': str(prompt), 'system': str(system or ''), 'temperature': temperature})\n"
        "    ms = _ai_state['mock_spec']\n"
        "    if isinstance(ms, list):\n"
        "        if not ms: return ''\n"
        "        i = _ai_state['idx']\n"
        "        _ai_state['idx'] = i + 1\n"
        "        return str(ms[i] if i < len(ms) else ms[-1])\n"
        "    if isinstance(ms, dict):\n"
        "        full_in = f'{system}\\n{prompt}'.lower()\n"
        "        for k, v in ms.items():\n"
        "            if k != '_default' and str(k).lower() in full_in:\n"
        "                return str(v)\n"
        "        return str(ms.get('_default', ''))\n"
        "    if ms is not None:\n"
        "        return str(ms)\n"
        "    return ''\n"
        "ns = {'ask_ai': ask_ai}\n"
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
        "    _ai_state['mock_spec'] = t.get('mock_ai')\n"
        "    _ai_state['calls'] = []\n"
        "    _ai_state['idx'] = 0\n"
        "    try:\n"
        "        buf = io.StringIO()\n"
        "        with contextlib.redirect_stdout(buf):\n"
        "            actual = eval(t['call'], ns, ns)\n"
        "        if buf.getvalue(): out_chunks.append(buf.getvalue())\n"
        "        exp = t['expected']\n"
        "        ok = (actual == exp) and (type(actual) is type(exp))\n"
        "        err_txt = '' if ok else f'Получено {repr(actual)}, ожидалось {repr(exp)}'\n"
        "        calls = _ai_state['calls']\n"
        "        min_c = t.get('min_ai_calls')\n"
        "        max_c = t.get('max_ai_calls')\n"
        "        if ok and min_c is not None and len(calls) < min_c:\n"
        "            ok = False\n"
        "            err_txt = f'Функция должна вызвать ask_ai(...) минимум {min_c} раз(а), вызвано: {len(calls)}'\n"
        "        if ok and max_c is not None and len(calls) > max_c:\n"
        "            ok = False\n"
        "            err_txt = f'Лишний вызов ask_ai(...): вызвано {len(calls)} раз(а), максимум допустимо {max_c}'\n"
        "        all_prompts = '\\n'.join((c.get('prompt','') + '\\n' + c.get('system','')) for c in calls).lower()\n"
        "        if ok and t.get('prompt_must_contain'):\n"
        "            for needle in t['prompt_must_contain']:\n"
        "                if str(needle).lower() not in all_prompts:\n"
        "                    ok = False\n"
        "                    err_txt = f'В запрос к ask_ai(...) не передано: {needle}'\n"
        "                    break\n"
        "        if ok and t.get('prompt_must_not_contain'):\n"
        "            for banned in t['prompt_must_not_contain']:\n"
        "                if str(banned).lower() in all_prompts:\n"
        "                    ok = False\n"
        "                    err_txt = f'В запросе к ask_ai(...) обнаружены лишние/незамаскированные данные: {banned}'\n"
        "                    break\n"
        "        results.append({'label': t['label'], 'passed': ok, 'actual': repr(actual), 'expected': repr(exp), 'error': err_txt})\n"
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
    t0 = time.perf_counter()
    task = CODE_TASKS.get(payload.task_id) or CODE_TASKS["ai_router"]
    tests_spec = task["tests"]
    tests_labels = [t["label"] for t in tests_spec]
    code = (payload.code or "").strip()
    if not code:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        log_activity(
            "code_editor_run",
            user_id=payload.user_id,
            status="fail",
            latency_ms=latency_ms,
            detail=f"{payload.task_id}: пустой редактор",
            meta={"task_id": payload.task_id, "passed": False, "passed_count": 0, "total_count": len(tests_spec)},
        )
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
            "error": "Редактор кода пуст. Напишите код или соберите его в «Мини-блоках».",
            "stdout": "",
            "tests_labels": tests_labels,
            "tests_results": [
                {"label": t["label"], "passed": False, "actual": "", "expected": repr(t["expected"]), "error": "Код отсутствует"}
                for t in tests_spec
            ],
        }

    run_res = run_python_tests_sandbox(code, tests_spec)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    log_activity(
        "code_editor_run",
        user_id=payload.user_id,
        status="ok" if run_res["passed"] else "fail",
        latency_ms=latency_ms,
        detail=f"{payload.task_id}: {run_res['passed_count']}/{run_res['total_count']}",
        meta={
            "task_id": payload.task_id,
            "passed": run_res["passed"],
            "passed_count": run_res["passed_count"],
            "total_count": run_res["total_count"],
            "error": run_res["error"] or None,
        },
    )
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


@app.post("/api/run-code-live-ai")
async def run_code_live_ai(payload: RunCodeLiveAiRequest):
    """Выполняет код студента на одном тестовом примере с реальными вызовами локальной LLM через ask_ai(...)."""
    import subprocess
    import sys

    t0 = time.perf_counter()
    task = CODE_TASKS.get(payload.task_id) or CODE_TASKS["ai_router"]
    code = (payload.code or "").strip()
    if not code:
        return {"ok": False, "error": "Редактор кода пуст. Сначала напишите решение.", "calls": [], "result": "", "stdout": ""}

    base_sys = (payload.system_prompt or "").strip()
    if payload.kb_text and payload.kb_text.strip():
        base_sys = f"{base_sys}\n\nБаза знаний:\n{payload.kb_text.strip()}".strip()
    default_temp = float(payload.temperature if payload.temperature is not None else 0.2)
    live_call = task.get("live_call") or ""

    runner_script = (
        "import json, io, contextlib, urllib.request\n"
        f"USER_CODE = json.loads({json.dumps(json.dumps(code, ensure_ascii=False))})\n"
        f"LLM_URL = json.loads({json.dumps(json.dumps(LLM_BASE_URL.rstrip('/') + '/chat/completions', ensure_ascii=False))})\n"
        f"BASE_SYS = json.loads({json.dumps(json.dumps(base_sys, ensure_ascii=False))})\n"
        f"DEF_TEMP = {default_temp!r}\n"
        f"LIVE_CALL = json.loads({json.dumps(json.dumps(live_call, ensure_ascii=False))})\n"
        "live_calls = []\n"
        "def ask_ai(prompt, system='', temperature=None):\n"
        "    if len(live_calls) >= 5:\n"
        "        raise RuntimeError('Превышен лимит 5 обращений к живому ИИ за один запуск')\n"
        "    sys_msg = (str(system).strip() or BASE_SYS or 'Ты точный AI-помощник.')\n"
        "    t_val = DEF_TEMP if temperature is None else float(temperature)\n"
        "    body = json.dumps({'model': 'local', 'messages': [{'role': 'system', 'content': sys_msg}, {'role': 'user', 'content': str(prompt)}], 'temperature': t_val, 'max_tokens': 320, 'stream': False}).encode('utf-8')\n"
        "    req = urllib.request.Request(LLM_URL, data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer none'}, method='POST')\n"
        "    with urllib.request.urlopen(req, timeout=18) as resp:\n"
        "        data = json.loads(resp.read().decode('utf-8'))\n"
        "    reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '')\n"
        "    live_calls.append({'prompt': str(prompt), 'system': sys_msg, 'reply': str(reply)})\n"
        "    return str(reply)\n"
        "ns = {'ask_ai': ask_ai}\n"
        "out_buf = io.StringIO()\n"
        "res_repr = ''\n"
        "err = ''\n"
        "try:\n"
        "    with contextlib.redirect_stdout(out_buf):\n"
        "        exec(compile(USER_CODE, '<code_live>', 'exec'), ns, ns)\n"
        "        if LIVE_CALL:\n"
        "            val = eval(LIVE_CALL, ns, ns)\n"
        "            res_repr = repr(val)\n"
        "except Exception as e:\n"
        "    err = f'{type(e).__name__}: {e}'\n"
        "print('__LIVE_AI_JSON__:' + json.dumps({'result': res_repr, 'error': err, 'calls': live_calls, 'stdout': out_buf.getvalue()[:1500]}, ensure_ascii=False))\n"
    )

    try:
        proc = subprocess.run([sys.executable, "-c", runner_script], capture_output=True, text=True, timeout=28)
        marker = next((l[len("__LIVE_AI_JSON__:"):] for l in reversed((proc.stdout or "").splitlines()) if l.startswith("__LIVE_AI_JSON__:")), None)
        if not marker:
            raw_err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(raw_err.splitlines()[-1] if raw_err else "Ошибка запуска")
        parsed = json.loads(marker)
    except Exception as ex:
        parsed = {"result": "", "error": f"Не удалось выполнить живой вызов ИИ: {ex}", "calls": [], "stdout": ""}

    latency_ms = int((time.perf_counter() - t0) * 1000)
    ok = not parsed.get("error")
    log_activity(
        "code_ai_verify",
        user_id=payload.user_id,
        status="ok" if ok else "fail",
        latency_ms=latency_ms,
        detail=f"live:{payload.task_id} calls={len(parsed.get('calls') or [])}",
        meta={"task_id": payload.task_id, "live": True, "error": parsed.get("error") or None},
    )
    return {
        "ok": ok,
        "task_id": payload.task_id,
        "live_call": live_call,
        "result": parsed.get("result", ""),
        "error": parsed.get("error", ""),
        "calls": parsed.get("calls", []),
        "stdout": parsed.get("stdout", ""),
        "latency_ms": latency_ms,
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

    log_activity(
        "workstation_assigned",
        user_id=assigned_uid,
        pc_name=pc_name,
        detail=f"Подключено рабочее место {pc_name} ({client_ip})",
        meta={"ip": client_ip, "pc_number": next_num, "client_key": client_key},
    )

    return {
        "pc_number": next_num,
        "pc_name": pc_name,
        "user_id": assigned_uid,
        "client_ip": client_ip,
        "locked": True,
    }


def _extract_progress_counts(progress: Optional[dict]) -> dict:
    if not isinstance(progress, dict):
        return {"levels_done": 0, "exams_done": 0, "code_done": 0, "total_done": 0}
    levels = progress.get("levels") if isinstance(progress.get("levels"), dict) else {}
    exams = progress.get("exams") if isinstance(progress.get("exams"), dict) else {}
    code_tasks = progress.get("codeTasks") if isinstance(progress.get("codeTasks"), dict) else {}

    levels_done = sum(1 for v in levels.values() if isinstance(v, dict) and v.get("passed"))
    exams_done = sum(1 for v in exams.values() if isinstance(v, dict) and v.get("passed"))
    code_done = sum(1 for v in code_tasks.values() if isinstance(v, dict) and v.get("passed"))
    return {
        "levels_done": levels_done,
        "exams_done": exams_done,
        "code_done": code_done,
        "total_done": levels_done + exams_done + code_done,
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
    new_counts = _extract_progress_counts(payload.progress)
    prev_counts = None

    with get_db() as conn:
        prev_row = conn.execute(
            "SELECT progress_json FROM user_task_progress WHERE user_id = ?",
            (clean_uid,),
        ).fetchone()
        if prev_row:
            try:
                prev_counts = _extract_progress_counts(json.loads(prev_row[0]))
            except Exception:
                prev_counts = None

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

    if prev_counts is None:
        if new_counts["total_done"] > 0:
            log_activity(
                "progress_saved",
                user_id=clean_uid,
                detail=f"Уровни {new_counts['levels_done']}/3, Экзамены {new_counts['exams_done']}/3, Код {new_counts['code_done']}/6",
                meta=new_counts,
            )
    elif new_counts != prev_counts:
        log_activity(
            "progress_saved",
            user_id=clean_uid,
            detail=f"Уровни {new_counts['levels_done']}/3, Экзамены {new_counts['exams_done']}/3, Код {new_counts['code_done']}/6",
            meta={**new_counts, "prev": prev_counts},
        )

    return {"status": "ok", "user_id": clean_uid}


@app.get("/teacher")
async def serve_teacher_page(request: Request, key: Optional[str] = None):
    require_teacher_access(request, key or request.headers.get("x-teacher-key"))
    if os.path.exists(STATIC_TEACHER_PATH):
        return FileResponse(STATIC_TEACHER_PATH)
    raise HTTPException(status_code=404, detail="teacher.html не найден")


@app.get("/api/teacher/logs")
async def get_teacher_logs(
    request: Request,
    key: Optional[str] = None,
    limit: int = 200,
    event_type: Optional[str] = None,
    pc_name: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
):
    require_teacher_access(request, key or request.headers.get("x-teacher-key"))
    safe_limit = min(max(int(limit), 1), 1000)

    clauses = []
    params = []
    if event_type and event_type.strip():
        clauses.append("event_type = ?")
        params.append(event_type.strip())
    if pc_name and pc_name.strip():
        clauses.append("pc_name = ?")
        params.append(pc_name.strip())
    if status and status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    if search and search.strip():
        like = f"%{search.strip()}%"
        clauses.append("(detail LIKE ? OR user_id LIKE ? OR pc_name LIKE ? OR bot_id LIKE ? OR meta_json LIKE ?)")
        params.extend([like, like, like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_db() as conn:
        log_rows = conn.execute(
            f"""
            SELECT id, ts, event_type, user_id, pc_name, bot_id, status, latency_ms, detail, meta_json
            FROM activity_logs
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, safe_limit),
        ).fetchall()

        ws_rows = conn.execute(
            """
            SELECT w.client_key, w.pc_number, w.pc_name, w.user_id, w.created_at,
                   p.progress_json, p.updated_at AS progress_updated_at,
                   (SELECT COUNT(*) FROM bots b WHERE b.user_id = w.user_id) AS bots_count
            FROM workstations w
            LEFT JOIN user_task_progress p ON p.user_id = w.user_id
            ORDER BY w.pc_number ASC
            """
        ).fetchall()

        last_act_rows = conn.execute(
            """
            SELECT l.user_id, l.ts, l.event_type
            FROM activity_logs l
            INNER JOIN (
                SELECT user_id, MAX(id) AS max_id
                FROM activity_logs
                WHERE user_id IS NOT NULL
                GROUP BY user_id
            ) latest ON latest.max_id = l.id
            """
        ).fetchall()
        last_by_uid = {r["user_id"]: {"ts": r["ts"], "event_type": r["event_type"]} for r in last_act_rows}

        stats_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_events,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_events,
                SUM(CASE WHEN event_type IN ('code_editor_run', 'code_ai_verify') AND status = 'ok' THEN 1 ELSE 0 END) AS code_passes
            FROM activity_logs
            """
        ).fetchone()
        bots_total = conn.execute("SELECT COUNT(*) FROM bots WHERE user_id != 'system'").fetchone()[0]
        event_types_rows = conn.execute(
            "SELECT DISTINCT event_type FROM activity_logs ORDER BY event_type ASC"
        ).fetchall()

    logs = []
    for r in log_rows:
        try:
            meta_obj = json.loads(r["meta_json"]) if r["meta_json"] else {}
        except Exception:
            meta_obj = {}
        logs.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "event_type": r["event_type"],
                "user_id": r["user_id"],
                "pc_name": r["pc_name"],
                "bot_id": r["bot_id"],
                "status": r["status"],
                "latency_ms": r["latency_ms"],
                "detail": r["detail"],
                "meta": meta_obj,
            }
        )

    workstations = []
    for w in ws_rows:
        progress_obj = None
        if w["progress_json"]:
            try:
                progress_obj = json.loads(w["progress_json"])
            except Exception:
                progress_obj = None
        counts = _extract_progress_counts(progress_obj)
        client_key = w["client_key"] or ""
        ip_label = client_key.split(":", 1)[1] if client_key.startswith("ip:") else "localhost"
        last_act = last_by_uid.get(w["user_id"])
        workstations.append(
            {
                "pc_number": w["pc_number"],
                "pc_name": w["pc_name"],
                "user_id": w["user_id"],
                "ip": ip_label,
                "bots_count": int(w["bots_count"] or 0),
                "levels_done": counts["levels_done"],
                "exams_done": counts["exams_done"],
                "code_done": counts["code_done"],
                "last_ts": last_act["ts"] if last_act else w["created_at"],
                "last_event": last_act["event_type"] if last_act else "workstation_assigned",
            }
        )

    return {
        "kpis": {
            "total_workstations": len(workstations),
            "total_bots": int(bots_total or 0),
            "total_events": int(stats_row["total_events"] or 0) if stats_row else 0,
            "error_events": int(stats_row["error_events"] or 0) if stats_row else 0,
            "code_passes": int(stats_row["code_passes"] or 0) if stats_row else 0,
        },
        "event_types": [r["event_type"] for r in event_types_rows],
        "workstations": workstations,
        "logs": logs,
    }


@app.get("/api/teacher/logs/download")
async def download_teacher_logs(request: Request, key: Optional[str] = None, date: Optional[str] = None):
    require_teacher_access(request, key or request.headers.get("x-teacher-key"))
    safe_date = datetime.now().strftime("%Y-%m-%d")
    if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date.strip()):
        safe_date = date.strip()
    log_path = os.path.join(LOGS_DIR, f"session_{safe_date}.jsonl")
    content = ""
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as lf:
            content = lf.read()
    return Response(
        content=content,
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="session_{safe_date}.jsonl"'},
    )


@app.post("/api/teacher/logs/clear")
async def clear_teacher_logs(request: Request, key: Optional[str] = None):
    require_teacher_access(request, key or request.headers.get("x-teacher-key"))
    with get_db() as conn:
        cur = conn.execute("DELETE FROM activity_logs")
        deleted_rows = cur.rowcount
        conn.commit()
    deleted_files = 0
    if os.path.isdir(LOGS_DIR):
        for fname in os.listdir(LOGS_DIR):
            if fname.endswith(".jsonl"):
                try:
                    os.remove(os.path.join(LOGS_DIR, fname))
                    deleted_files += 1
                except Exception:
                    pass
    return {"status": "ok", "deleted_rows": deleted_rows, "deleted_files": deleted_files}
