import os
import httpx
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("TAVUS_API_KEY")
replica_id = os.getenv("TAVUS_REPLICA_ID")

url = "https://tavusapi.com/v2/conversations"
headers = {
    "x-api-key": api_key,
    "Content-Type": "application/json"
}
payload = {
    "replica_id": replica_id,
    "custom_greeting": "Welcome to your interview.",
    "conversational_context": "You are an HR interviewer. Please ask these questions one by one: 1. Tell me about yourself. 2. What are your strengths? Wait for the user to answer before asking the next question."
}
try:
    response = httpx.post(url, headers=headers, json=payload, timeout=10)
    print("Status:", response.status_code)
    print("Body:", response.text)
except Exception as e:
    print("Error:", e)
