from fastapi import APIRouter, Request, Form, Response
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from database import get_db_connection
from psycopg2.extras import RealDictCursor

router = APIRouter()
templates = Jinja2Templates(directory="templates")

@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...)
):
    email = email.lower().strip()
    user = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s AND password = %s", (email, password))
        user = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        print("Login DB Error:", e)

    if user:
        # Redirect based on role
        if user['role'] == 'admin':
            redirect_url = "/dashboard"
        else:
            redirect_url = "/interview"

        resp = RedirectResponse(url=redirect_url, status_code=303)
        # Set a simple cookie to track the session role (for prototype purposes)
        resp.set_cookie(key="user_email", value=user['email'])
        resp.set_cookie(key="user_role", value=user['role'])
        return resp
    else:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid email or password."
        })

@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("user_email")
    resp.delete_cookie("user_role")
    return resp
