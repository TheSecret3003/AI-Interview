import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    # Attempt to load from env or fallback to default local configuration
    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/interview_db")
    return psycopg2.connect(db_url)
