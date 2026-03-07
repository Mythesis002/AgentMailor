import smtplib
import json
import time
import re
import logging
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import types

# 1. LOGGING SETUP
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("himalaya_outreach.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# 2. CONFIGURATION
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDL6mu7ePmgpPgz-Lgp-uS9VMj4eitHbM4")
EMAIL_USER     = "adarshsinghgonti02@gmail.com"
EMAIL_PASS     = "mamppvuvxekabqkx"  # ✅ FIXED: removed spaces from App Password

# 3. GEMINI CLIENT INITIALIZATION
client = genai.Client(api_key=GEMINI_API_KEY)

# 4. HELPERS
def is_valid_email(email: str) -> bool:
    """Checks if email is valid and not a common placeholder."""
    pattern = r"^[\w\.\+\-]+@[\w\-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, str(email)):
        return False
    blocklist = {"example.com", "startup.com", "company.com", "yourdomain.com", "placeholder.com"}
    domain = email.split("@")[-1].lower()
    return domain not in blocklist

from typing import Optional

def safe_parse_json(text: str) -> Optional[dict]:
    """Robustly extracts JSON from AI response text."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group())
            except Exception as e:
                log.warning(f"JSON fallback parse failed: {e}")
    return None

# 5. AI RESEARCH & WRITING ENGINE
def find_and_draft_lead(niche_keyword: str):
    log.info(f"🔎 Researching niche: {niche_keyword}")

    prompt = f"""
    Search for ONE active AI startup in the "{niche_keyword}" space (Seed or Series A).
    Identify their product and their specific data labeling needs (Computer Vision, NLP, etc.).

    Draft a cold email from 'Adarsh, Founder of Himalaya IT' (25-member expert team in Greater Noida).
    
    RULES:
    1. Subject Line: Must be professional and relevant (e.g., 'A quick question about [Product Name]s data pipeline').
    2. Body: Max 4 sentences. 
    3. Customization: Mention a specific feature of their product you found during the search.
    4. Offer: A free pilot for bulk annotation.

    RETURN ONLY THIS JSON OBJECT, NO OTHER TEXT:
    {{
        "company_name": "Name",
        "likely_contact_email": "real_public_email_address",
        "clickbait_subject": "Subject",
        "email_body": "Message Body"
    }}
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",  # ✅ FIXED: valid model name
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],  # ✅ FIXED: correct tool format
                temperature=0.7
            )
        )

        data = safe_parse_json(response.text)
        if data and all(k in data for k in ["company_name", "likely_contact_email", "clickbait_subject", "email_body"]):
            return data
        log.warning(f"AI returned invalid format for '{niche_keyword}'. Raw response: {response.text[:300]}")
        return None
    except Exception as e:
        log.error(f"Gemini API Error: {e}")
        return None

# 6. EMAIL SENDING ENGINE
def send_email(target_email: str, subject: str, body: str):
    if not is_valid_email(target_email):
        log.warning(f"⚠️  Invalid or placeholder email skipped: {target_email}")
        return False

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = target_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
            log.info(f"✅ Email successfully sent to {target_email}")
            return True
    except smtplib.SMTPAuthenticationError:
        log.error("❌ Gmail authentication failed. Check your App Password.")
        return False
    except smtplib.SMTPRecipientsRefused:
        log.error(f"❌ Recipient refused: {target_email}")
        return False
    except Exception as e:
        log.error(f"❌ SMTP Error for {target_email}: {e}")
        return False

# 7. MAIN EXECUTION
if __name__ == "__main__":
    # --- CONTROL PANEL ---
    ACTUALLY_SEND = True  # ✅ FIXED: set to True to actually send emails

    TARGET_NICHES = [
        "Medical imaging AI startups India",  # ✅ Only 1 niche = 1 email
    ]
    # ---------------------

    log.info("🚀 Starting Himalaya IT Outreach Engine...")

    for niche in TARGET_NICHES:
        lead = find_and_draft_lead(niche)

        if lead:
            print("-" * 50)
            print(f"🏢 Company : {lead['company_name']}")
            print(f"📧 Email   : {lead['likely_contact_email']}")
            print(f"📌 Subject : {lead['clickbait_subject']}")
            print(f"💬 Body    :\n{lead['email_body']}")
            print("-" * 50)

            if ACTUALLY_SEND:
                sent = send_email(
                    lead['likely_contact_email'],
                    lead['clickbait_subject'],
                    lead['email_body']
                )
                if sent:
                    print("✅ Email sent successfully!")
                else:
                    print("❌ Email failed to send. Check himalaya_outreach.log for details.")
            else:
                log.info("ℹ️  Dry run active. Set ACTUALLY_SEND=True to send.")

        else:
            log.warning(f"No valid lead found for niche: '{niche}'")

        time.sleep(10)  # API rate limit protection

    log.info("✅ Done.")
