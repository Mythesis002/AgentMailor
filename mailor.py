import smtplib
import json
import time
import re
import logging
import socket
import dns.resolver
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# 1. LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("himalaya_outreach.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 2. CONFIGURATION
# ─────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyDL6mu7ePmgpPgz-Lgp-uS9VMj4eitHbM4"
EMAIL_USER     = "adarshsinghgonti02@gmail.com"
EMAIL_PASS     = "mamppvuvxekabqkx"
MAX_RETRIES    = 3   # How many times to ask Gemini for a new email if verification fails

# ─────────────────────────────────────────────
# 3. GEMINI CLIENT
# ─────────────────────────────────────────────
client = genai.Client(api_key=GEMINI_API_KEY)

# ─────────────────────────────────────────────
# 4. HELPERS
# ─────────────────────────────────────────────
BLOCKLIST_DOMAINS = {
    "example.com", "startup.com", "company.com",
    "yourdomain.com", "placeholder.com", "test.com"
}

def is_syntactically_valid(email: str) -> bool:
    pattern = r"^[\w\.\+\-]+@[\w\-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, str(email)):
        return False
    domain = email.split("@")[-1].lower()
    return domain not in BLOCKLIST_DOMAINS


def verify_email_exists(email: str) -> tuple[bool, str]:
    """
    Two-layer email verification:
      Layer 1 – DNS MX record check (does the mail server exist?)
      Layer 2 – SMTP RCPT TO probe (does the mailbox exist?)
    
    Returns (is_valid: bool, reason: str)
    """
    if not is_syntactically_valid(email):
        return False, "Fails basic syntax / blocklist check"

    domain = email.split("@")[-1]

    # --- Layer 1: MX record ---
    try:
        mx_records = dns.resolver.resolve(domain, "MX")
        mx_host = sorted(mx_records, key=lambda r: r.preference)[0].exchange.to_text().rstrip(".")
        log.info(f"  ✅ MX found for {domain}: {mx_host}")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as e:
        reason = f"No MX record for domain '{domain}': {e}"
        log.warning(f"  ❌ {reason}")
        return False, reason

    # --- Layer 2: SMTP probe (catch-all aware) ---
    try:
        with smtplib.SMTP(timeout=10) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo("gmail.com")
            smtp.mail("verify@gmail.com")
            code, msg = smtp.rcpt(email)
            smtp.quit()
            if code == 250:
                log.info(f"  ✅ SMTP confirmed mailbox exists: {email}")
                return True, "Mailbox confirmed via SMTP"
            else:
                reason = f"SMTP rejected (code {code}): {msg.decode(errors='ignore')}"
                log.warning(f"  ❌ {reason}")
                return False, reason
    except smtplib.SMTPConnectError as e:
        # Some servers block port 25 but are still real — treat as uncertain but valid
        log.warning(f"  ⚠️  SMTP port 25 blocked for {mx_host}, assuming valid after MX pass: {e}")
        return True, "MX exists; SMTP port 25 blocked (likely valid)"
    except Exception as e:
        log.warning(f"  ⚠️  SMTP probe failed ({e}), assuming valid after MX pass")
        return True, f"MX exists; SMTP probe inconclusive ({e})"


def safe_parse_json(text: str) -> Optional[dict]:
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


# ─────────────────────────────────────────────
# 5. AGENTIC GEMINI SESSION (stateful conversation)
# ─────────────────────────────────────────────

class OutreachAgent:
    """
    Maintains a multi-turn conversation with Gemini in a single session.
    Gemini remembers what happened in this session (company found, emails tried, etc.)
    """

    def __init__(self):
        self.conversation_history = []   # Full message history sent to Gemini each turn
        self.model = "gemini-2.5-flash"

    def _chat(self, user_message: str) -> str:
        """Send a message and get a reply, keeping full history."""
        self.conversation_history.append({
            "role": "user",
            "parts": [{"text": user_message}]
        })

        response = client.models.generate_content(
            model=self.model,
            contents=self.conversation_history,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.7
            )
        )

        reply = response.text or ""
        # Add assistant reply to history so Gemini remembers it next turn
        self.conversation_history.append({
            "role": "model",
            "parts": [{"text": reply}]
        })
        return reply

    def find_lead(self, niche_keyword: str) -> Optional[dict]:
        """Turn 1: Ask Gemini to research and draft the first email."""
        log.info(f"🔎 [Agent] Researching niche: '{niche_keyword}'")

        prompt = f"""
You are an outreach assistant for 'Himalaya IT' (Founder: Adarsh, 25-member expert team, Greater Noida).

Search for ONE active AI startup in the "{niche_keyword}" space (Seed or Series A).
Find their real, publicly listed contact or founder email address from their website, LinkedIn, Crunchbase, or press releases.

Draft a cold email:
- Subject: Professional and specific (e.g., "Quick question about [Product]'s data pipeline")
- Body: Max 4 sentences. Mention a specific product feature. Offer a free pilot for bulk annotation.

RETURN ONLY THIS JSON, NO OTHER TEXT:
{{
    "company_name": "Name",
    "likely_contact_email": "real_verified_public_email",
    "clickbait_subject": "Subject Line",
    "email_body": "Email body text"
}}
"""
        reply = self._chat(prompt)
        return safe_parse_json(reply)

    def retry_email(self, company_name: str, bad_email: str, reason: str) -> Optional[dict]:
        """
        Subsequent turns: Tell Gemini the email failed verification and ask for a better one.
        Gemini still has full memory of what it found before.
        """
        log.info(f"🔄 [Agent] Asking Gemini to find a new email for {company_name} (bad: {bad_email})")

        prompt = f"""
The email you provided for {company_name} was "{bad_email}" but it FAILED email verification.
Reason: {reason}

Please search again and find a DIFFERENT, REAL, publicly listed email for {company_name}.
Try: their official website contact page, LinkedIn founder profile, Crunchbase, AngelList, or press releases.
Do NOT return "{bad_email}" again.

RETURN ONLY THIS JSON, NO OTHER TEXT:
{{
    "company_name": "{company_name}",
    "likely_contact_email": "new_real_email",
    "clickbait_subject": "Subject Line",
    "email_body": "Email body text"
}}
"""
        reply = self._chat(prompt)
        return safe_parse_json(reply)


# ─────────────────────────────────────────────
# 6. EMAIL SENDER
# ─────────────────────────────────────────────

def send_email(target_email: str, subject: str, body: str) -> bool:
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
            log.info(f"✅ Email sent to {target_email}")
            return True
    except smtplib.SMTPAuthenticationError:
        log.error("❌ Gmail auth failed. Check App Password.")
        return False
    except smtplib.SMTPRecipientsRefused:
        log.error(f"❌ Recipient refused: {target_email}")
        return False
    except Exception as e:
        log.error(f"❌ SMTP Error: {e}")
        return False


# ─────────────────────────────────────────────
# 7. MAIN AGENTIC LOOP
# ─────────────────────────────────────────────

def run_outreach(niche: str, actually_send: bool = True):
    print(f"\n{'='*60}")
    print(f"🚀 Starting agentic outreach for: '{niche}'")
    print(f"{'='*60}")

    agent = OutreachAgent()  # Fresh conversation session per niche

    # Turn 1: Find initial lead
    lead = agent.find_lead(niche)
    if not lead:
        log.error(f"❌ Gemini couldn't find a lead for '{niche}'. Skipping.")
        return

    attempt = 0
    while attempt < MAX_RETRIES:
        email = lead.get("likely_contact_email", "")
        company = lead.get("company_name", "Unknown")

        print(f"\n--- Attempt {attempt + 1}/{MAX_RETRIES} ---")
        print(f"🏢 Company : {company}")
        print(f"📧 Email   : {email}")
        print(f"📌 Subject : {lead.get('clickbait_subject', '')}")
        print(f"💬 Body    :\n{lead.get('email_body', '')}")

        # ── EMAIL VERIFICATION ──
        print(f"\n🔍 Verifying email: {email} ...")
        is_valid, reason = verify_email_exists(email)

        if is_valid:
            print(f"✅ Email verified: {reason}")
            if actually_send:
                sent = send_email(email, lead["clickbait_subject"], lead["email_body"])
                if sent:
                    print("✅ Email sent successfully!")
                else:
                    print("❌ Sending failed. Check logs.")
            else:
                log.info("ℹ️  Dry run — email not sent (set actually_send=True to send).")
            return  # Done for this niche

        else:
            print(f"❌ Email FAILED verification: {reason}")
            attempt += 1
            if attempt < MAX_RETRIES:
                print(f"🔄 Asking Gemini (same session) to find a better email...")
                time.sleep(3)
                lead = agent.retry_email(company, email, reason)
                if not lead:
                    log.error("❌ Gemini returned invalid format on retry. Stopping.")
                    return
            else:
                print(f"🚫 Max retries ({MAX_RETRIES}) reached. Could not verify a valid email for '{company}'.")
                log.warning(f"Gave up on '{company}' after {MAX_RETRIES} attempts.")


if __name__ == "__main__":
    # ── CONTROL PANEL ──────────────────────────
    ACTUALLY_SEND = True   # Set False for dry run (no emails sent)

    TARGET_NICHES = [
        "Medical imaging AI startups India",
        # "NLP AI startups India",  # Add more niches here
    ]
    # ───────────────────────────────────────────

    log.info("🚀 Himalaya IT Agentic Outreach Engine starting...")

    for niche in TARGET_NICHES:
        run_outreach(niche, actually_send=ACTUALLY_SEND)
        time.sleep(15)  # Rate limit buffer between niches

    log.info("✅ All niches processed.")
