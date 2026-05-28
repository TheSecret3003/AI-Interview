from fastapi import APIRouter, Request, Form
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

    return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request, "scores": scores})

@router.get("/add-job", response_class=HTMLResponse)
async def add_job_page(request: Request):
    return templates.TemplateResponse(request=request, name="add_job.html", context={"request": request})

@router.post("/add-job", response_class=HTMLResponse)
async def add_job_submit(request: Request, title_offer: str = Form(...), job_description: str = Form(...)):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO job_offers (title_offer, job_description) VALUES (%s, %s)",
            (title_offer, job_description)
        )
        conn.commit()
        cur.close()
        conn.close()
        success_message = f"Job Role '{title_offer}' added successfully!"
    except Exception as e:
        print("DB Add Job Error:", e)
        return templates.TemplateResponse(request=request, name="add_job.html", context={"request": request, "error": "Failed to add job role. It might already exist or there was a database error."})

    return templates.TemplateResponse(request=request, name="add_job.html", context={"request": request, "success_message": success_message})
