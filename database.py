import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    if not DATABASE_URL:
        raise Exception("❌ DATABASE_URL not found in .env")
    
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        #print("✅ Connected to the database successfully.")
        return conn
    except Exception as e:
        print("❌ Failed to connect to the database.")
        raise e
