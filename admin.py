from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from database import get_db_connection
from psycopg2.extras import RealDictCursor

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    scores = []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM candidate_score ORDER BY created_at DESC")
        scores = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print("Dashboard Load Scores Error:", e)

    return templates.TemplateResponse("dashboard.html", {"request": request, "scores": scores})
