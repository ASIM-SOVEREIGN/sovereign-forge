from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
import os
import hashlib
import secrets
from datetime import datetime
from pydantic import BaseModel
from contextlib import asynccontextmanager
import threading
import asyncio
import subprocess
import tempfile
import asyncpg
import requests
from itertools import cycle

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    print("✅ Neon connection pool created")

async def close_db_pool():
    global db_pool
    if db_pool:
        await db_pool.close()
        print("✅ Neon connection pool closed")

@asynccontextmanager
async def get_db():
    async with db_pool.acquire() as conn:
        yield conn

async def init_tables():
    async with get_db() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                token TEXT UNIQUE,
                token_created_at TIMESTAMP,
                total_tokens_used INTEGER DEFAULT 0,
                monthly_tokens_used INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                last_login_at TIMESTAMP,
                last_login_ip TEXT
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT DEFAULT 'New Chat',
                model_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        admin = await conn.fetchrow("SELECT * FROM users WHERE is_admin = 1")
        if not admin:
            salt = secrets.token_hex(16)
            hashed = hashlib.pbkdf2_hmac('sha256', "admin123".encode(), salt.encode(), 100000)
            token = secrets.token_urlsafe(32)
            await conn.execute("""
                INSERT INTO users (email, username, password_salt, password_hash, token, is_admin)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, "admin@iaithion.com", "admin", salt, hashed.hex(), token, 1)
            print("✅ Default admin created")

class SignupRequest(BaseModel):
    email: str
    password: str
    username: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ChatRequest(BaseModel):
    model: str
    messages: list
    max_tokens: int = 5000
    constitution: str = None

class MessageRequest(BaseModel):
    role: str
    content: str

class ExecuteRequest(BaseModel):
    code: str
    language: str
    timeout: int = 30

def hash_password(password: str, salt: str = None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt, hashed.hex()

def verify_password(password: str, salt: str, hashed: str):
    _, new_hash = hash_password(password, salt)
    return new_hash == hashed

def generate_token():
    return secrets.token_urlsafe(32)

GROQ_KEYS = [
    os.environ.get("GROQ_KEY_1"),
    os.environ.get("GROQ_KEY_2"),
    os.environ.get("GROQ_KEY_3"),
    os.environ.get("GROQ_KEY_4"),
]
GROQ_KEYS = [k for k in GROQ_KEYS if k]
groq_rotator = cycle(GROQ_KEYS) if GROQ_KEYS else None

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

def search_web(query):
    if not SERPER_API_KEY:
        return ""
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query},
            timeout=10
        )
        if response.status_code != 200:
            return ""
        data = response.json()
        results = []
        for r in data.get("organic", [])[:3]:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            if title and snippet:
                results.append(f"{title}: {snippet}")
        return "\n".join(results)
    except:
        return ""

@app.on_event("startup")
async def startup():
    await init_db_pool()
    await init_tables()
    print(f"✅ Groq keys: {len(GROQ_KEYS)}")
    print(f"✅ SERPER: {'configured' if SERPER_API_KEY else 'missing'}")

@app.on_event("shutdown")
async def shutdown():
    await close_db_pool()

@app.get("/")
def root():
    return {"status": "Sovereign Forge Proxy Alive", "serper": "configured" if SERPER_API_KEY else "missing"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/signup")
async def signup(request: SignupRequest):
    async with get_db() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1 OR username = $2", 
                                       request.email, request.username)
        if existing:
            raise HTTPException(status_code=400, detail="Email or username already exists")
        
        salt, hashed = hash_password(request.password)
        token = generate_token()
        
        await conn.execute("""
            INSERT INTO users (email, username, password_salt, password_hash, token, token_created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, request.email, request.username, salt, hashed, token, datetime.now())
        
        return {"message": "User created", "token": token}

@app.post("/login")
async def login(request: LoginRequest):
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE email = $1", request.email)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        if not verify_password(request.password, user['password_salt'], user['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        return {"message": "Login successful", "token": user['token']}

@app.post("/conversations/new")
async def create_conversation(authorization: str = Header(None), model_name: str = "ASIM_Pilot"):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        row = await conn.fetchrow("""
            INSERT INTO conversations (user_id, model_name, title)
            VALUES ($1, $2, $3)
            RETURNING id
        """, user['id'], model_name, "New Chat")
        
        return {"conversation_id": row['id']}

@app.post("/conversations/{conv_id}/messages")
async def save_message(conv_id: int, request: MessageRequest, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        conv = await conn.fetchrow("SELECT id FROM conversations WHERE id = $1 AND user_id = $2", conv_id, user['id'])
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        await conn.execute("""
            INSERT INTO conversation_messages (conversation_id, role, content)
            VALUES ($1, $2, $3)
        """, conv_id, request.role, request.content)
        
        await conn.execute("UPDATE conversations SET updated_at = NOW() WHERE id = $1", conv_id)
        
        return {"status": "saved"}

@app.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: int, limit: int = 50, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        messages = await conn.fetch("""
            SELECT role, content, created_at FROM conversation_messages
            WHERE conversation_id = $1 ORDER BY created_at ASC LIMIT $2
        """, conv_id, limit)
        
        return {"messages": [{"role": m['role'], "content": m['content']} for m in messages]}

@app.get("/conversations")
async def list_conversations(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        convs = await conn.fetch("""
            SELECT id, title, model_name, created_at, updated_at
            FROM conversations WHERE user_id = $1 ORDER BY updated_at DESC
        """, user['id'])
        
        return {"conversations": [{"id": c['id'], "title": c['title'], "model_name": c['model_name'], "created_at": c['created_at']} for c in convs]}

@app.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: int, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "")
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        await conn.execute("DELETE FROM conversations WHERE id = $1 AND user_id = $2", conv_id, user['id'])
        return {"status": "deleted"}

@app.post("/v1/chat/completions")
async def chat_completion(request: ChatRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.replace("Bearer ", "")
    
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_message = None
        for msg in reversed(request.messages):
            if msg.get("role") == "user":
                user_message = msg.get("content")
                break
        
        if not user_message:
            raise HTTPException(status_code=400, detail="No user message found")
        
        search_results = search_web(user_message)
        
        messages = []
        if search_results:
            messages.append({"role": "system", "content": f"Current search results: {search_results}"})
        if request.constitution:
            messages.append({"role": "system", "content": request.constitution})
        messages.append({"role": "user", "content": user_message})
        
        if not groq_rotator:
            raise HTTPException(status_code=503, detail="No API keys configured")
        
        groq_key = next(groq_rotator)
        groq_response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": request.max_tokens},
            timeout=60
        )
        
        if groq_response.status_code != 200:
            raise HTTPException(status_code=503, detail="Groq API failed")
        
        response_content = groq_response.json()["choices"][0]["message"]["content"]
        
        return {"choices": [{"message": {"role": "assistant", "content": response_content}}]}

@app.post("/v1/execute")
async def execute_code(request: ExecuteRequest, authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = authorization.replace("Bearer ", "")
    
    async with get_db() as conn:
        user = await conn.fetchrow("SELECT id FROM users WHERE token = $1", token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        if request.language != "python":
            return {"output": "", "error": "Only Python supported", "supported": False}
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(request.code)
            temp_file = f.name
        
        try:
            result = subprocess.run(['python3', temp_file], capture_output=True, text=True, timeout=request.timeout)
            return {"output": result.stdout, "error": result.stderr, "supported": True}
        except subprocess.TimeoutExpired:
            return {"output": "", "error": f"Timeout after {request.timeout}s", "supported": True}
        finally:
            os.unlink(temp_file)
