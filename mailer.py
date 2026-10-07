"""Shared SMTP sender (used by the CV screening and interview result flows)."""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def send_email(to_email: str, subject: str, body_text: str, body_html: str) -> bool:
    """Send a multipart text+HTML email. Returns False (and logs) on any failure.

    Never raises: callers run inside background tasks where a mail outage must
    not lose the candidate's score.
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender_email = os.getenv("SMTP_FROM", smtp_user or "no-reply@talentflow.tdi.my.id")

    if not (smtp_host and smtp_user and smtp_password):
        print("⚠️ SMTP credentials not configured in .env. Skipping email notification.")
        return False

    if not to_email or "@" not in to_email:
        print(f"⚠️ Invalid recipient address {to_email!r}. Skipping email notification.")
        return False

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

        print(f"✅ Email sent successfully to {to_email}!")
        return True
    except Exception as e:
        print(f"❌ Failed to send email to {to_email}: {e}")
        return False
