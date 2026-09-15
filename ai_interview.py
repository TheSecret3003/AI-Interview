from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
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
    phone: str
    education: str
    qa_pairs: List[QAPair]
    mode: Optional[str] = "fixed"

class AgentTurnPayload(BaseModel):
    role: str
    name: Optional[str] = ""
    qa_pairs: List[QAPair] = []

class TTSPayload(BaseModel):
    text: str


def get_llm(temperature: float = 0.3, max_tokens: int = None, model: str = "z-ai/glm-4.6"):
    """Helper to build a configured ChatOpenAI client via OpenRouter.

    model defaults to z-ai/glm-4.6 (used for scoring). The live interview agent loop
    passes a fast, non-reasoning model instead (e.g. openai/gpt-4o-mini).
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    http_client = httpx.Client(verify=False)
    kwargs = dict(
        model=model,
        temperature=temperature,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        http_client=http_client,
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def ensure_interview_config_table():
    """Create the interview_config table (stores per-role Option-2 agent settings)."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS interview_config (
                id SERIAL PRIMARY KEY,
                role TEXT UNIQUE NOT NULL,
                mode TEXT DEFAULT 'fixed',
                requirements TEXT DEFAULT '',
                rules TEXT DEFAULT '',
                max_questions INT DEFAULT 5,
                answer_time_limit INT DEFAULT 60
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Migration Error (interview_config):", e)


def load_interview_config(role: str):
    """Load the Option-2 agent configuration for a role, with sensible defaults."""
    ensure_interview_config_table()
    config = {
        "mode": "fixed",
        "requirements": "",
        "rules": "",
        "max_questions": 5,
        "answer_time_limit": 60,
    }
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT mode, requirements, rules, max_questions, answer_time_limit FROM interview_config WHERE role = %s", (role,))
        row = cur.fetchone()
        if row:
            config.update({
                "mode": row["mode"] or "fixed",
                "requirements": row["requirements"] or "",
                "rules": row["rules"] or "",
                "max_questions": row["max_questions"] or 5,
                "answer_time_limit": row["answer_time_limit"] or 60,
            })
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Load Interview Config Error:", e)
    return config


def save_interview_config(role: str, mode: str, requirements: str, rules: str, max_questions: int, answer_time_limit: int):
    ensure_interview_config_table()
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO interview_config (role, mode, requirements, rules, max_questions, answer_time_limit)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (role) DO UPDATE SET
                mode = EXCLUDED.mode,
                requirements = EXCLUDED.requirements,
                rules = EXCLUDED.rules,
                max_questions = EXCLUDED.max_questions,
                answer_time_limit = EXCLUDED.answer_time_limit
        """, (role, mode, requirements, rules, max_questions, answer_time_limit))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Save Interview Config Error:", e)

def load_questions(role: str = None):
    # Auto-migrate database to add time_limit column if it doesn't exist
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS time_limit INT DEFAULT 60;")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Migration Error (time_limit):", e)

    questions = []
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if role:
            cur.execute("SELECT question_text, time_limit FROM questions WHERE role = %s ORDER BY id ASC", (role,))
        else:
            cur.execute("SELECT question_text, time_limit FROM questions ORDER BY id ASC")
        questions = [{'text': row['question_text'], 'time_limit': row.get('time_limit', 60)} for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Load Questions Error:", e)
    return questions

def save_questions(role: str, questions: list, time_limits: list):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM questions WHERE role = %s", (role,))
        for q, t in zip(questions, time_limits):
            cur.execute("INSERT INTO questions (role, question_text, time_limit) VALUES (%s, %s, %s)", (role, q, t))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Save Questions Error:", e)

def run_interview_analysis_task(name: str, email: str, role: str, phone: str, education: str, qa_pairs: List[QAPair], mode: str = "fixed"):
    job_description = "Job description not found."
    # Load agent config (requirements/rules) so the scorer can factor them in for Option 2
    interview_config = load_interview_config(role)
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

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return

    # Phase 1: Refine and Correct the Transcript
    refine_prompt = f"""You are an expert Indonesian transcriber.
The following is an interview transcript generated by a Speech-to-Text engine. It may contain typos, misspellings, or phonetic mistakes.
Please read the transcript and correct any obvious spelling/grammar mistakes to make the candidate's answers clear and readable.
Do not change the actual meaning of their answers, just fix the typos.

Original Transcript:
{transcript_text}

Return ONLY the fully corrected transcript in the exact same Q&A format. Do not include any introductory or concluding text.
"""
    try:
        http_client = httpx.Client(verify=False)
        llm = ChatOpenAI(
            model="z-ai/glm-4.6",
            temperature=0.1,
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            http_client=http_client,
        )
        refined_response = llm.invoke(refine_prompt)
        refined_transcript = refined_response.content.strip()
    except Exception as e:
        print("Transcript Refinement Error:", e)
        refined_transcript = transcript_text # Fallback to original if refinement fails

    # Extra context for Option 2 (agentic) interviews: HR requirements & rules
    agent_criteria = ""
    if mode == "agent":
        agent_criteria = f"""

HR Requirements for this position:
{interview_config.get('requirements') or '(none provided)'}

HR Rules / focus areas for the interview:
{interview_config.get('rules') or '(none provided)'}
"""

    # Phase 2: Analyze and Score the Refined Transcript
    prompt = f"""You are an expert HR Interviewer. Please analyze the following candidate's interview transcript based on the requested Job Role.

Candidate Details:
- Name: {name}
- Applied Job Role: {role}

Job Description for {role}:
{job_description}
{agent_criteria}
Interview Transcript:
{refined_transcript}

Task:
1. Evaluate the candidate's answers based strictly on how well their responses match the Job Description{' and the HR Requirements/Rules above' if mode == 'agent' else ''}.
2. Provide an overall score for the interview out of 100.
3. Provide a detailed analysis of their strengths, weaknesses, and overall communication skills based on the transcript.
4. Please make it in points

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
        try:
            response = llm.invoke(prompt)
            ai_output = response.content
        except Exception as api_err:
            print("OpenRouter API Error:", api_err)
            ai_output = "Score: N/A\nAnalysis: Terjadi kesalahan saat memproses wawancara dengan AI. Harap pastikan API Key sudah benar."


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

        # Convert markdown **bold** to HTML strong tags
        analysis_text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', analysis_text)
        # Convert markdown bullet points (*) to standard bullet characters (•)
        analysis_text = re.sub(r'(?m)^\s*\*\s+', r'• ', analysis_text)

        # Send WhatsApp Notification for Interview Score >= 80
        try:
            numeric_score = int(score) if score.isdigit() else 0
            if numeric_score >= 80:
                wa_message = f"Halo {name},\n\nSelamat! Anda dinyatakan LULUS dalam tahap wawancara AI untuk posisi {role} di Indico dengan nilai {numeric_score}/100.\n\nTim HR kami akan segera menghubungi Anda untuk proses selanjutnya.\n\nSemoga sukses!"

                # Twilio Credentials
                account_sid = os.getenv("TWILIO_ACCOUNT_SID")
                auth_token = os.getenv("TWILIO_AUTH_TOKEN")

                # Format recipient number
                formatted_to = phone.strip()
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
                    res = httpx.post(
                        twilio_url,
                        auth=(account_sid, auth_token),
                        data=payload_data,
                        verify=False
                    )
                    if res.status_code == 201:
                        print(f"✅ Twilio Interview WA sent successfully to {formatted_to}!")
                    else:
                        print(f"❌ Twilio API Error ({res.status_code}): {res.text}")
                else:
                    print("⚠️ Twilio credentials missing in environment variables. Printing message to logs:")
                    print(f"Sender: whatsapp:+14155238886")
                    print(f"Recipient: {formatted_to}")
                    print(f"Message:\n{wa_message}")
        except Exception as wa_err:
            print("Failed to send Interview WhatsApp notification:", wa_err)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO candidate_score (name, email, phone, education, role, score, score_type, resume_text, analysis)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name,
            email,
            phone,
            education,
            role,
            int(score) if score.isdigit() else None,
            "AI Agent Interview Score" if mode == "agent" else "Interview Score",
            refined_transcript[:30000],
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
    config = load_interview_config(selected_role)

    return templates.TemplateResponse(request=request, name="hr_setup.html", context={
        "request": request,
        "questions": questions,
        "roles": roles,
        "selected_role": selected_role,
        "config": config
    })

@router.post("/save-questions", response_class=HTMLResponse)
async def save_questions_endpoint(request: Request, role: str = Form(...), mode: str = Form("fixed")):
    form_data = await request.form()

    mode = mode if mode in ("fixed", "agent") else "fixed"

    # --- Option 1: fixed questions ---
    questions_raw = form_data.getlist("questions")
    time_limits_raw = form_data.getlist("time_limits")
    questions = []
    time_limits = []
    for q, t in zip(questions_raw, time_limits_raw):
        if q.strip():
            questions.append(q.strip())
            time_limits.append(int(t) if t.isdigit() else 60)

    # --- Option 2: AI agent configuration ---
    requirements = (form_data.get("requirements") or "").strip()
    rules = (form_data.get("rules") or "").strip()
    max_questions_raw = form_data.get("max_questions") or "5"
    agent_time_limit_raw = form_data.get("answer_time_limit") or "60"
    try:
        max_questions = max(1, int(max_questions_raw))
    except ValueError:
        max_questions = 5
    try:
        answer_time_limit = max(10, int(agent_time_limit_raw))
    except ValueError:
        answer_time_limit = 60

    if role:
        # Persist fixed questions (kept even in agent mode so switching back is easy)
        if questions:
            save_questions(role, questions, time_limits)
        # Persist the selected mode + agent config
        save_interview_config(role, mode, requirements, rules, max_questions, answer_time_limit)

    config = load_interview_config(role)

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

    label = "AI Agent interview settings" if mode == "agent" else "Questions"
    return templates.TemplateResponse(request=request, name="hr_setup.html", context={
        "request": request,
        "questions": questions,
        "roles": roles,
        "selected_role": role,
        "config": config,
        "message": f"{label} for {role} saved successfully!"
    })

@router.get("/interview", response_class=HTMLResponse)
async def interview_page(request: Request):
    user_email = request.cookies.get("user_email")
    if not user_email:
        return RedirectResponse(url="/", status_code=303)

    user_email = user_email.lower().strip()
    candidate_name = "Candidate"
    candidate_role = "AI Engineer"

    candidate_phone = ""
    candidate_education = ""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Fetch the latest CV submission record to get the candidate's details
        cur.execute("SELECT name, role, phone, education FROM candidate_score WHERE email = %s AND score_type = 'CV Score' ORDER BY created_at DESC LIMIT 1", (user_email,))
        record = cur.fetchone()
        if record:
            candidate_name = record['name']
            candidate_role = record['role']
            candidate_phone = record['phone']
            candidate_education = record['education']
        cur.close()
        conn.close()
    except Exception as e:
        print("DB Fetch Candidate Error:", e)

    questions = load_questions(candidate_role)
    config = load_interview_config(candidate_role)

    return templates.TemplateResponse(request=request, name="interview.html", context={
        "request": request,
        "questions": questions,
        "candidate_name": candidate_name,
        "candidate_email": user_email,
        "candidate_role": candidate_role,
        "candidate_phone": candidate_phone,
        "candidate_education": candidate_education,
        "interview_mode": config.get("mode", "fixed"),
        "agent_time_limit": config.get("answer_time_limit", 60),
        "agent_max_questions": config.get("max_questions", 5)
    })

@router.post("/tts")
async def generate_tts(payload: TTSPayload):
    """
    Generate TTS audio using Google Gemini's audio modality.
    Returns base64 encoded raw PCM16 audio (24kHz).
    """
    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("TTS_MODEL", "gemini-3.1-flash-tts-preview")

    if not api_key:
        return {"error": "GEMINI_API_KEY is missing"}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    req_payload = {
        "contents": [{"role": "user", "parts": [{"text": payload.text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": "Aoede" # Aoede, Charon, Fenrir, Kore, Puck
                    }
                }
            }
        }
    }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(url, json=req_payload, timeout=20)
            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if not candidates:
                return {"error": "No candidates returned from TTS"}

            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "inlineData" in part:
                    audio_b64 = part["inlineData"].get("data")
                    return {"audio_base64": audio_b64}

            return {"error": "No inlineData found in response"}
    except Exception as e:
        print("TTS Error:", str(e))
        return {"error": str(e)}

@router.post("/analyze-interview")
async def analyze_interview(payload: InterviewPayload, background_tasks: BackgroundTasks):
    """
    Receive interview transcript, start background LLM task, and immediately respond.
    """
    # Push the heavily-processing task to run in the background
    background_tasks.add_task(run_interview_analysis_task, payload.name, payload.email, payload.role, payload.phone, payload.education, payload.qa_pairs, payload.mode or "fixed")

    # Return success immediately to front end
    return {
        "success": True,
        "message": f"Terima kasih {payload.name} sudah mengikuti wawancara di Indico!"
    }


@router.post("/interview-agent/next")
async def interview_agent_next(payload: AgentTurnPayload):
    """
    Option 2 (AI Agent) endpoint. Given the conversation so far, the LLM decides the
    next question OR signals that the interview is complete.

    Returns JSON:
      { "done": false, "question": "..." }   -> ask the next question
      { "done": true }                        -> interview finished
    """
    config = load_interview_config(payload.role)
    max_questions = config.get("max_questions", 5)
    requirements = config.get("requirements") or "(none provided)"
    rules = config.get("rules") or "(none provided)"

    # Pull job description for extra context
    job_description = "Job description not found."
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT job_description FROM job_offers WHERE title_offer ILIKE %s LIMIT 1", (f"%{payload.role}%",))
        res = cur.fetchone()
        if res:
            job_description = res[0]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Agent: could not read job description. Error: {e}")

    questions_asked = len(payload.qa_pairs)

    # Hard stop: never exceed the HR-defined limit
    if questions_asked >= max_questions:
        return {"done": True, "question": None, "questions_asked": questions_asked}

    # Build the running conversation transcript
    transcript_text = ""
    for idx, qa in enumerate(payload.qa_pairs):
        transcript_text += f"Q{idx+1}: {qa.question}\nA{idx+1}: {qa.answer}\n\n"
    if not transcript_text:
        transcript_text = "(The interview has not started yet. You must ask the opening question.)"

    prompt = f"""You are "Gabby", an expert AI HR interviewer conducting a live voice interview in Indonesian.

Applied Job Role: {payload.role}
Candidate Name: {payload.name or 'Kandidat'}

Job Description:
{job_description}

HR Requirements for this position:
{requirements}

HR Rules / focus areas you MUST follow:
{rules}

Interview limit: You must ask {max_questions} questions in total.
Questions already asked so far: {questions_asked}
Remaining questions you still need to ask: {max_questions - questions_asked}

Conversation so far:
{transcript_text}

Your task:
- Decide the single best NEXT question to ask, adapting to the candidate's previous answers (dig deeper, clarify, or move to a new relevant topic).
- Follow the HR requirements and rules strictly. Stay on-topic for the role.
- Ask ONE question at a time. Keep it concise and conversational.
- DIG DEEPER when needed: Jika jawaban kandidat sebelumnya kurang memuaskan, terlalu singkat, tidak spesifik, tidak jelas, atau menghindari inti pertanyaan, ajukan pertanyaan lanjutan (follow-up) untuk menggali lebih dalam pada topik yang sama sebelum berpindah ke topik baru. Minta contoh konkret, detail teknis, atau penjelasan lebih lanjut bila perlu.
- Jika jawaban kandidat sudah memuaskan dan lengkap, lanjutkan ke topik atau area penilaian berikutnya yang relevan.
- IMPORTANT: Do NOT end the interview early. Keep asking questions until the {max_questions}-question limit is reached. Only set "done": true when there are no remaining questions ({questions_asked} of {max_questions} already asked).

Respond with STRICT JSON only, no extra text, in one of these two forms:
{{"done": false, "question": "<the next question in Indonesian>"}}
or
{{"done": true}}

Since there are still {max_questions - questions_asked} question(s) remaining, you MUST return "done": false with a question now.
Use Indonesian language for the question. Do not use any '*' symbol.
"""

    # Use a fast, non-reasoning model for the live agent loop. Unlike a reasoning model,
    # it emits the JSON answer directly (no "thinking" tokens), so responses are quick
    # and won't come back empty. Scoring still uses z-ai/glm-4.6 (see run_interview_analysis_task).
    llm = get_llm(temperature=0.5, max_tokens=400, model="openai/gpt-4o-mini")
    if llm is None:
        return {"done": True, "question": None, "error": "LLM not configured"}

    try:
        response = llm.invoke(prompt)
        raw = (response.content or "").strip()
    except Exception as e:
        print("Agent LLM Error:", e)
        # Fail safe: end the interview so the candidate isn't stuck
        return {"done": True, "question": None, "error": str(e)}

    print(f"[Agent] role={payload.role} asked={questions_asked}/{max_questions} raw_response={raw!r}")

    # If the model returned nothing (e.g. reasoning consumed the whole budget), do NOT
    # end the interview while questions still remain -- fall back to a generic question.
    if not raw:
        print("[Agent] Empty response from model; using fallback question.")
        return {
            "done": False,
            "question": "Bisa Anda ceritakan lebih detail mengenai pengalaman Anda yang paling relevan dengan posisi ini?",
            "questions_asked": questions_asked,
            "max_questions": max_questions,
        }

    # Parse the model's JSON, tolerating markdown code fences or surrounding text
    import json
    question = None
    done = False
    try:
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(json_match.group(0) if json_match else raw)
        done = bool(parsed.get("done", False))
        question = parsed.get("question")
    except Exception as e:
        print("Agent JSON parse error:", e, "| raw:", raw)
        # If parsing fails but we got text, treat it as the next question
        cleaned = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()
        if cleaned:
            question = cleaned
            done = False
        else:
            done = False  # keep going with a fallback rather than ending early

    # Safety net: if the model said "done" but we haven't reached the limit yet,
    # OR it gave no question, keep the interview going with a fallback question.
    if (done or not question) and questions_asked < max_questions:
        if done:
            print(f"[Agent] Model tried to end early at {questions_asked}/{max_questions}; overriding with a follow-up.")
        done = False
        if not question:
            question = "Bisa Anda ceritakan lebih detail mengenai pengalaman Anda yang paling relevan dengan posisi ini?"

    return {
        "done": done,
        "question": None if done else question,
        "questions_asked": questions_asked,
        "max_questions": max_questions
    }
