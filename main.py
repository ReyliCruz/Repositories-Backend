from fastapi import FastAPI
from contextlib import asynccontextmanager
from database import get_connection
from routes import github
from fastapi.middleware.cors import CORSMiddleware

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        conn = get_connection()
        conn.close()
        print("✅ Database connection check passed.")
    except Exception as e:
        print("❌ Error during database connection check:", e)

    yield
    print("👋 Shutting down the app.")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "🚀 API is running and DB connection works!"}

app.include_router(github.router)
