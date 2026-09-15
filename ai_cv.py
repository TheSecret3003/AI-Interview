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

def run_cv_analysis_task(name: str, email: str, wa_number: str, education: str, job_role: str, cv_text: str, password: str):
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
        try:
            response = llm.invoke(prompt)
            ai_output = response.content
        except Exception as api_err:
            print("OpenRouter API Error:", api_err)
            ai_output = "Score: N/A\nAnalysis: Terjadi kesalahan saat memproses CV dengan AI. Harap pastikan API Key sudah benar."


        score = "N/A"
        analysis_text = ai_output

        score_match = re.search(r"(?:Score|Skor):\s*(\d+)", ai_output, re.IGNORECASE)
        if score_match:
            score = score_match.group(1)

        analysis_match = re.split(r"(?:Analysis|Analisis):\s*", ai_output, maxsplit=1, flags=re.IGNORECASE)
        if len(analysis_match) > 1:
            analysis_text = analysis_match[1].strip()
        else:
            analysis_text = re.sub(r"(?:Score|Skor):\s*\d+\n?", "", ai_output, flags=re.IGNORECASE).strip()

        # Convert markdown **bold** and *italic* to HTML tags
        analysis_text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', analysis_text)
        # Convert markdown bullet points (*) to standard bullet characters (•)
        analysis_text = re.sub(r'(?m)^\s*\*\s+', r'• ', analysis_text)

        # Send WhatsApp Notification based on Score
        try:
            numeric_score = int(score) if score.isdigit() else 0
            if numeric_score >= 80:
                wa_message = f"Halo {name},\n\nSelamat! Anda dinyatakan LULUS dalam seleksi berkas CV untuk posisi {job_role} di Indico dengan nilai {numeric_score}/100.\n\nSilakan login ke portal wawancara menggunakan email Anda dan password berikut untuk mengikuti tahap wawancara AI:\nPassword: {password}\n\nSemoga sukses!"
            else:
                wa_message = f"Halo {name},\n\nTerima kasih telah melamar posisi {job_role} di Indico. Setelah melakukan proses seleksi berkas CV, mohon maaf Anda belum dapat kami lanjutkan ke tahap berikutnya.\n\nTetap semangat dan semoga sukses dalam karir Anda ke depan!"

            # Twilio Credentials
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")

            # Format recipient number to ensure it has '+' and 'whatsapp:' prefix
            formatted_to = wa_number.strip()
            # Remove any spaces, dashes, or parentheses
            formatted_to = re.sub(r'[^\d+]', '', formatted_to)
            if formatted_to.startswith("0"):
                formatted_to = "+62" + formatted_to[1:]
            elif formatted_to.startswith("62") and not formatted_to.startswith("+"):
                formatted_to = "+" + formatted_to
            elif not formatted_to.startswith("+"):
                formatted_to = "+62" + formatted_to

            formatted_to = f"whatsapp:{formatted_to}"

            if account_sid and auth_token:
                twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
                payload_data = {
                    "From": "whatsapp:+14155238886",
                    "To": formatted_to,
                    "Body": wa_message
                }
                # Twilio uses Basic Auth: SID as username, Auth Token as password
                res = httpx.post(
                    twilio_url,
                    auth=(account_sid, auth_token),
                    data=payload_data,
                    verify=False
                )
                if res.status_code == 201:
                    print(f"✅ Twilio WhatsApp message sent successfully to {formatted_to}!")
                else:
                    print(f"❌ Twilio API Error ({res.status_code}): {res.text}")
            else:
                print("⚠️ Twilio credentials missing in environment variables. Printing message to logs:")
                print(f"Sender: whatsapp:+14155238886")
                print(f"Recipient: {formatted_to}")
                print(f"Message:\n{wa_message}")
        except Exception as wa_err:
            print("Failed to send WhatsApp notification:", wa_err)

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

    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, "roles": roles})

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
            return templates.TemplateResponse(request=request, name="index.html", context={
                "request": request,
                "error": "Invalid file format. Please upload a .docx file."
            })

        file_content = await cv_file.read()
        email = email.lower().strip()

        # Generate password early so we can pass it to the background task
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

        try:
            doc_stream = io.BytesIO(file_content)
            document = docx.Document(doc_stream)
            cv_text = "\n".join([paragraph.text for paragraph in document.paragraphs])
        except Exception as e:
            return templates.TemplateResponse(request=request, name="index.html", context={
                "request": request,
                "error": f"Failed to parse the .docx file. Ensure it is not corrupted. Error: {str(e)}"
            })

        # Dispatch AI scoring logic asynchronously in the background
        background_tasks.add_task(run_cv_analysis_task, name, email, wa_number, education, job_role, cv_text, password)

        roles = [job_role]

        # Instantly return success message to user
        return templates.TemplateResponse(request=request, name="index.html", context={
            "request": request,
            "roles": roles,
            "success_message": f"Terima kasih {name} sudah mengirim CV anda. Kami sedang memproses data anda. Hasil seleksi berkas akan dikirimkan langsung ke nomor WhatsApp Anda."
        })

    except Exception as e:
        traceback.print_exc()
        return templates.TemplateResponse(request=request, name="index.html", context={
            "request": request,
            "error": f"An unexpected error occurred during processing: {str(e)}"
        })
