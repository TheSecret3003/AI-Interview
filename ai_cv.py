from fastapi import APIRouter, Request, File, UploadFile, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os
import io
import docx
import re
import httpx
import traceback
import random
import string
from psycopg2.extras import RealDictCursor
from langchain_openai import ChatOpenAI

from database import get_db_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def run_cv_analysis_task(name: str, email: str, wa_number: str, education: str, job_role: str, cv_text: str):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return

    job_description = "Job description not found."
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT job_description FROM job_offers WHERE title_offer ILIKE %s LIMIT 1", (f"%{job_role}%",))
        res = cur.fetchone()
        if res:
            job_description = res[0]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not read job description from DB. Error: {e}")

    http_client = httpx.Client(verify=False)
    llm = ChatOpenAI(
        model="z-ai/glm-4.6",
        temperature=0.3,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        http_client=http_client,
    )

    prompt = f"""You are an expert HR recruiter and AI Interviewer. Please analyze the following candidate based on their submitted details and their parsed CV text, and evaluate their fit for the requested Job Role.

Candidate Details:
- Name: {name}
- Email: {email}
- WhatsApp: {wa_number}
- Education: {education}
- Applied Job Role: {job_role}

Job Description for {job_role}:
{job_description}

CV Text:
{cv_text}

Task:
1. Provide an overall score for the candidate out of 100 based strictly on how well their qualifications, experience, and skills match the Job Description provided.
2. Provide a detailed analysis of their strengths, weaknesses, and overall fit for this specific role.

Format your response EXACTLY as follows:
Score: [Your Score]
Analysis: [Your detailed analysis]

Don't use any '*' symbol on output. Please strictly use Indonesian language.
"""
    try:
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

        # Convert markdown **bold** and *italic* to HTML tags
        analysis_text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', analysis_text)
        # Convert markdown bullet points (*) to standard bullet characters (•)
        analysis_text = re.sub(r'(?m)^\s*\*\s+', r'• ', analysis_text)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO candidate_score (name, email, phone, education, role, score, score_type, resume_text, analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name,
            email,
            wa_number,
            education,
            job_role,
            int(score) if score.isdigit() else None,
            "CV Score",
            cv_text[:30000],
            analysis_text
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not save cv results to DB. Error: {e}")


@router.get("/cv-submission", response_class=HTMLResponse)
async def read_root(request: Request):
    roles = ["AI Engineer"]
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT title_offer FROM job_offers")
        db_roles = [row['title_offer'] for row in cur.fetchall()]
        if db_roles:
            roles = db_roles
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Fetch Roles Error:", e)

    return templates.TemplateResponse("index.html", {"request": request, "roles": roles})

def generate_password(length=6):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

@router.post("/analyze", response_class=HTMLResponse)
async def analyze_cv(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    email: str = Form(...),
    wa_number: str = Form(...),
    education: str = Form(...),
    job_role: str = Form(...),
    cv_file: UploadFile = File(...)
):
    try:
        if not cv_file.filename.lower().endswith(".docx"):
            return templates.TemplateResponse("index.html", {
                "request": request,
                "error": "Invalid file format. Please upload a .docx file."
            })

        file_content = await cv_file.read()
        email = email.lower().strip()
        try:
            doc_stream = io.BytesIO(file_content)
            document = docx.Document(doc_stream)
            cv_text = "\n".join([paragraph.text for paragraph in document.paragraphs])
        except Exception as e:
            return templates.TemplateResponse("index.html", {
                "request": request,
                "error": f"Failed to parse the .docx file. Ensure it is not corrupted. Error: {str(e)}"
            })

        # Dispatch AI scoring logic asynchronously in the background
        background_tasks.add_task(run_cv_analysis_task, name, email, wa_number, education, job_role, cv_text)

        # Generate password and insert user into DB
        password = generate_password()
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO users (email, password, role)
                VALUES (%s, %s, 'candidate')
                ON CONFLICT (email) DO UPDATE SET password = EXCLUDED.password
            """, (email, password))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Warning: Could not create user in DB. Error: {e}")

        roles = [job_role]

        # Instantly return success message to user with their auto-generated password
        return templates.TemplateResponse("index.html", {
            "request": request,
            "roles": roles,
            "success_message": f"Terima kasih {name} sudah mengirim CV anda. Kami sedang memproses data anda.",
            "password_message": password
        })

    except Exception as e:
        traceback.print_exc()
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": f"An unexpected error occurred during processing: {str(e)}"
        })
