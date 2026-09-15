from fastapi import APIRouter, Request, File, UploadFile, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os
import io
import docx
from pypdf import PdfReader
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
import traceback
import random
import string
import json
from psycopg2.extras import RealDictCursor
from langchain_openai import ChatOpenAI

from database import get_db_connection

router = APIRouter()
templates = Jinja2Templates(directory="templates")

def send_email_notification(to_email: str, candidate_name: str, job_role: str, score: int, password: str):
    """Send email notification using SMTP (e.g. Gmail / Mailgun / Brevo / standard SMTP)."""
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender_email = os.getenv("SMTP_FROM", smtp_user or "no-reply@talentflow.tdi.my.id")

    if not (smtp_host and smtp_user and smtp_password):
        print("⚠️ SMTP credentials not configured in .env. Skipping email notification.")
        return

    is_passed = score >= 80
    if is_passed:
        subject = f"Selamat! Hasil Seleksi Berkas CV - Posisi {job_role} (Indico)"
        body_text = f"""Halo {candidate_name},

Selamat! Anda dinyatakan LULUS dalam seleksi berkas CV untuk posisi {job_role} di Indico dengan nilai {score}/100.

Silakan login ke portal wawancara menggunakan email Anda dan password berikut untuk mengikuti tahap wawancara AI:
Portal: https://talentflow.tdi.my.id
Email: {to_email}
Password: {password}

Semoga sukses!

Salam hangat,
Tim Rekrutmen Indico
"""
        body_html = f"""<div style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #1b8354;">Selamat! Anda Lulus Seleksi Berkas</h2>
    <p>Halo <strong>{candidate_name}</strong>,</p>
    <p>Selamat! Anda dinyatakan <strong>LULUS</strong> dalam seleksi berkas CV untuk posisi <strong>{job_role}</strong> di Indico dengan nilai <strong>{score}/100</strong>.</p>
    <div style="background: #f4fbf7; border: 1px solid #c7ebd8; border-radius: 8px; padding: 16px; margin: 20px 0;">
        <h4 style="margin-top: 0; color: #1b8354;">Kredensial Login Wawancara AI:</h4>
        <p style="margin: 4px 0;"><strong>Portal:</strong> <a href="https://talentflow.tdi.my.id" target="_blank">https://talentflow.tdi.my.id</a></p>
        <p style="margin: 4px 0;"><strong>Email:</strong> {to_email}</p>
        <p style="margin: 4px 0;"><strong>Password:</strong> <code style="background: #e1f5ec; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 1.1em;">{password}</code></p>
    </div>
    <p>Silakan segera login dan ikuti tahap wawancara AI.</p>
    <p>Semoga sukses!<br><strong>Tim Rekrutmen Indico</strong></p>
</div>"""
    else:
        subject = f"Hasil Seleksi Berkas CV - Posisi {job_role} (Indico)"
        body_text = f"""Halo {candidate_name},

Terima kasih telah melamar posisi {job_role} di Indico. Setelah melakukan proses seleksi berkas CV, mohon maaf Anda belum dapat kami lanjutkan ke tahap berikutnya.

Tetap semangat dan semoga sukses dalam karir Anda ke depan!

Salam hangat,
Tim Rekrutmen Indico
"""
        body_html = f"""<div style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <p>Halo <strong>{candidate_name}</strong>,</p>
    <p>Terima kasih telah melamar posisi <strong>{job_role}</strong> di Indico.</p>
    <p>Setelah melakukan proses seleksi berkas CV, mohon maaf Anda belum dapat kami lanjutkan ke tahap berikutnya.</p>
    <p>Tetap semangat dan semoga sukses dalam karir Anda ke depan!</p>
    <p>Salam hangat,<br><strong>Tim Rekrutmen Indico</strong></p>
</div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = to_email

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                server.login(smtp_user, smtp_password)
                server.sendmail(sender_email, to_email, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(sender_email, to_email, msg.as_string())

        print(f"✅ Email notification sent successfully to {to_email}!")
    except Exception as e:
        print(f"❌ Failed to send email notification to {to_email}: {e}")


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
        numeric_score = int(score) if score.isdigit() else 0
        try:
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

        # Send Email Notification with Login Credentials
        try:
            send_email_notification(
                to_email=email,
                candidate_name=name,
                job_role=job_role,
                score=numeric_score,
                password=password
            )
        except Exception as mail_err:
            print("Failed to send Email notification:", mail_err)

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
        filename = cv_file.filename.lower()
        if not (filename.endswith((".docx", ".pdf"))):
            return templates.TemplateResponse(request=request, name="index.html", context={
                "request": request,
                "error": "Invalid file format. Please upload a .docx or .pdf file."
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

        cv_text = ""
        try:
            file_stream = io.BytesIO(file_content)
            if filename.endswith(".docx"):
                document = docx.Document(file_stream)
                cv_text = "\n".join([paragraph.text for paragraph in document.paragraphs])
            elif filename.endswith(".pdf"):
                reader = PdfReader(file_stream)
                cv_text = "\n".join([page.extract_text() or "" for page in reader.pages])
            
            if not cv_text.strip():
                return templates.TemplateResponse(request=request, name="index.html", context={
                    "request": request,
                    "error": "Could not extract any text from the uploaded CV. Please make sure the file is not empty or scanned image only."
                })
        except Exception as e:
            return templates.TemplateResponse(request=request, name="index.html", context={
                "request": request,
                "error": f"Failed to parse the file. Ensure it is not corrupted. Error: {str(e)}"
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
