"""Self-check: interview result emails say the right thing to the right people.

Run: python test_interview_email.py
"""
import re

import ai_interview as ai

sent = []
ai.send_email = lambda to, subj, text, html: sent.append(
    {"to": to, "subject": subj, "text": text, "html": html}
)

AI_OUTPUT = """Score: 85
Analysis: Kandidat menunjukkan pemahaman teknis yang baik.
Kelemahan: kurang memberi contoh konkret, jawaban agak bertele-tele.
Feedback: Anda menyampaikan jawaban dengan percaya diri dan terstruktur.
Ke depannya, lengkapi jawaban dengan contoh konkret agar lebih meyakinkan.
"""

# --- Parsing: the HR analysis and the candidate feedback must separate cleanly ---
analysis = re.split(r"(?:Analysis|Analisis):\s*", AI_OUTPUT, maxsplit=1, flags=re.IGNORECASE)[1].strip()
parts = re.split(r"(?:Feedback|Masukan):\s*", analysis, maxsplit=1, flags=re.IGNORECASE)
internal, feedback = parts[0].strip(), parts[1].strip()

assert "Kelemahan" in internal, internal
assert "Kelemahan" not in feedback, "internal analysis leaked into candidate feedback"
assert feedback.startswith("Anda menyampaikan"), feedback

# --- Pass email ---
ai.send_interview_result_email("a@b.com", "Budi", "AI Engineer", 85, feedback)
passed = sent[-1]
assert "Selamat" in passed["subject"]
assert "LULUS" in passed["text"]
assert feedback.split("\n")[0] in passed["text"], "feedback missing from pass email"

# --- Reject email: polite, has feedback, no harshness ---
ai.send_interview_result_email("a@b.com", "Budi", "AI Engineer", 42, feedback)
rejected = sent[-1]
assert "Selamat" not in rejected["subject"], rejected["subject"]
assert "mohon maaf" in rejected["text"].lower()
assert "Terima kasih" in rejected["text"]
assert feedback.split("\n")[0] in rejected["text"], "feedback missing from reject email"

# --- The score is internal: it must never reach the candidate ---
for mail in (passed, rejected):
    for body in (mail["text"], mail["html"]):
        assert "85" not in body and "42" not in body, f"numeric score leaked: {mail['subject']}"
        assert "/100" not in body, "score leaked"

# --- Missing Feedback section falls back to generic text, never to the raw analysis ---
ai.send_interview_result_email("a@b.com", "Budi", "AI Engineer", 42, "")
assert ai.GENERIC_FEEDBACK in sent[-1]["text"]
assert "Kelemahan" not in sent[-1]["text"], "HR-facing analysis must never be emailed"

# --- HTML injection in LLM output / candidate name must be escaped ---
ai.send_interview_result_email("a@b.com", "<script>x</script>", "Role", 85, "<b>hi</b>")
assert "<script>" not in sent[-1]["html"], "unescaped name in HTML email"
assert "<b>hi</b>" not in sent[-1]["html"], "unescaped feedback in HTML email"

# --- Every call produced an email: pass, reject, no-feedback, injection ---
assert len(sent) == 4, len(sent)

print("OK")
