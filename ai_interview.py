from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from typing import Optional, List
from pydantic import BaseModel
import os
import re
import httpx
import traceback
from psycopg2.extras import RealDictCursor
from langchain_openai import ChatOpenAI

from database import get_db_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")

class QAPair(BaseModel):
    question: str
    answer: str

class InterviewPayload(BaseModel):
    name: str
    email: str
    role: str
    qa_pairs: List[QAPair]

def load_questions(role: str = None):
    questions = []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if role:
            cur.execute("SELECT question_text FROM questions WHERE role = %s ORDER BY id ASC", (role,))
        else:
            cur.execute("SELECT question_text FROM questions ORDER BY id ASC")
        questions = [row['question_text'] for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Load Questions Error:", e)
    return questions

def save_questions(role: str, questions: list):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM questions WHERE role = %s", (role,))
        for q in questions:
            cur.execute("INSERT INTO questions (role, question_text) VALUES (%s, %s)", (role, q))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Save Questions Error:", e)

def run_interview_analysis_task(name: str, email: str, role: str, qa_pairs: List[QAPair]):
    job_description = "Job description not found."
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT job_description FROM job_offers WHERE title_offer ILIKE %s LIMIT 1", (f"%{role}%",))
        res = cur.fetchone()
        if res:
            job_description = res[0]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not read job description from DB. Error: {e}")

    transcript_text = ""
    for idx, qa in enumerate(qa_pairs):
        transcript_text += f"Q{idx+1}: {qa.question}\nA{idx+1}: {qa.answer}\n\n"

    prompt = f"""You are an expert HR Interviewer. Please analyze the following candidate's interview transcript based on the requested Job Role.

Candidate Details:
- Name: {name}
- Applied Job Role: {role}

Job Description for {role}:
{job_description}

Interview Transcript:
{transcript_text}

Task:
1. Evaluate the candidate's answers based strictly on how well their responses match the Job Description provided.
2. Provide an overall score for the interview out of 100.
3. Provide a detailed analysis of their strengths, weaknesses, and overall communication skills based on the transcript.

Format your response EXACTLY as follows:
Score: [Your Score]
Analysis: [Your detailed analysis]

Don't use any '*' symbol on output. Please strictly use Indonesian language.
"""

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return

    try:
        http_client = httpx.Client(verify=False)
        llm = ChatOpenAI(
            model="z-ai/glm-4.6",
            temperature=0.3,
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            http_client=http_client,
        )
        response = llm.invoke(prompt)
        ai_output = response.content

        score = "N/A"
        analysis_text = ai_output

        score_match = re.search(r"Score:\s*(\d+)", ai_output, re.IGNORECASE)
        if score_match:
            score = score_match.group(1)

        analysis_match = re.split(r"Analysis:\s*", ai_output, maxsplit=1, flags=re.IGNORECASE)
        if len(analysis_match) > 1:
            analysis_text = analysis_match[1].strip()
        else:
            analysis_text = re.sub(r"Score:\s*\d+\n?", "", ai_output, flags=re.IGNORECASE).strip()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO candidate_score (name, email, phone, location, role, score, score_type, resume_text, analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name,
            email,
            "",
            "",
            role,
            int(score) if score.isdigit() else None,
            "Interview Score",
            transcript_text[:30000],
            analysis_text
        ))
        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        print(f"Background Task Error: {e}")


@router.get("/hr-setup", response_class=HTMLResponse)
async def hr_setup_page(request: Request, role: Optional[str] = None):
    roles = ["AI Engineer"]
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT title_offer FROM job_offers")
        db_roles = [row['title_offer'] for row in cur.fetchall()]
        if db_roles:
            roles = db_roles
        cur.close()
        conn.close()
    except Exception as e:
        pass

    selected_role = role if role else roles[0]
    questions = load_questions(selected_role)

    return templates.TemplateResponse("hr_setup.html", {
        "request": request,
        "questions": questions,
        "roles": roles,
        "selected_role": selected_role
    })

@router.post("/save-questions", response_class=HTMLResponse)
async def save_questions_endpoint(request: Request, role: str = Form(...)):
    form_data = await request.form()
    questions = form_data.getlist("questions")
    questions = [q.strip() for q in questions if q.strip()]

    if role:
        save_questions(role, questions)

    roles = [role]
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT title_offer FROM job_offers")
        db_roles = [row['title_offer'] for row in cur.fetchall()]
        if db_roles:
            roles = db_roles
        cur.close()
        conn.close()
    except Exception as e:
        pass

    return templates.TemplateResponse("hr_setup.html", {
        "request": request,
        "questions": questions,
        "roles": roles,
        "selected_role": role,
        "message": f"Questions for {role} saved successfully!"
    })

@router.get("/interview", response_class=HTMLResponse)
async def interview_page(request: Request, role: Optional[str] = None):
    roles = ["AI Engineer"]
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT DISTINCT title_offer FROM job_offers")
        db_roles = [row['title_offer'] for row in cur.fetchall()]
        if db_roles:
            roles = db_roles
        cur.close()
        conn.close()
    except Exception as e:
        pass

    selected_role = role if role else roles[0]
    questions = load_questions(selected_role)

    return templates.TemplateResponse("interview.html", {
        "request": request,
        "questions": questions,
        "roles": roles,
        "selected_role": selected_role
    })

@router.post("/analyze-interview")
async def analyze_interview(payload: InterviewPayload, background_tasks: BackgroundTasks):
    """
    Receive interview transcript, start background LLM task, and immediately respond.
    """
    # Push the heavily-processing task to run in the background
    background_tasks.add_task(run_interview_analysis_task, payload.name, payload.email, payload.role, payload.qa_pairs)
    
    # Return success immediately to front end
    return {
        "success": True,
        "message": f"Terima kasih {payload.name} sudah mengikuti wawancara di Snappy!"
    }
