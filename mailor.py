"""
AgentMailor: Freelance Web & AI Outreach
(businesses sourced from Google Maps data via RapidAPI, with OpenStreetMap fallback)

An independent freelance developer offers relevant website, web-app, mobile-app and AI call-agent
services based on evidence found for each business.

Per run:
  0. Sync inbox: mark replies / bounces / opt-outs, pause sending if the bounce rate is too high
  1. Follow up (once) with leads that stayed silent for 3+ days
  2. RapidAPI 'Local Business Data' (Google Maps) finds real businesses, only in cities where it is business hours NOW
     (free OpenStreetMap is the automatic fallback if the API key/quota is not available)
  3. FIT SCORE: decide which businesses really need a call agent, using measured facts only:
       - how phone-dependent the category is (dentists, clinics, salons, home services ...)
       - no live chat / no online booking on their website, or no website at all
       - Google listing shows closed days or early closing (missed calls after hours)
       - many reviews (busy phone line)
     Businesses below SCORE_MIN are skipped, never emailed.
  4. Public email from Google Maps data / the site + MX check
  5. Gemini (free tier) writes a short, hooking, honest email that says who we are and what the agent does;
     a template is used if Gemini is unavailable
  6. Send, log to sent_log.csv; every good-fit lead with a phone number goes to call_list.csv

Secrets come ONLY from environment variables (GitHub Actions secrets).
"""

import csv
import imaplib
import ipaddress
import json
import logging
import os
import random
import re
import smtplib
import socket
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import dns.resolver
import requests
import urllib3
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("himalaya_outreach.log"), logging.StreamHandler()],
)
log = logging.getLogger("himalaya")


# ─────────────────────────────────────────────
# CONFIG  (secrets from env, never hardcode)
# ─────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


RAPIDAPI_KEY = _env("RAPIDAPI_KEY")
RAPID_MAX_CALLS_PER_DAY = int(_env("RAPID_MAX_CALLS_PER_DAY", "6"))   # protect your free plan quota
RAPID_LIMIT = int(_env("RAPID_LIMIT", "30"))                          # businesses per API call
GEMINI_API_KEY = _env("GEMINI_API_KEY")                 # optional: template email is used without it
EMAIL_USER = _env("EMAIL_USER")                         # inbox that receives replies (IMAP)
EMAIL_PASS = _env("EMAIL_PASS")
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER", EMAIL_USER)
SMTP_PASS = _env("SMTP_PASS", EMAIL_PASS)
FROM_EMAIL = _env("FROM_EMAIL", SMTP_USER)

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
BUSINESS_HOURS_ONLY = _env("BUSINESS_HOURS_ONLY", "true").lower() == "true"
DAILY_LIMIT = int(_env("DAILY_LIMIT", "10"))            # new emails per UTC day (all runs together)
FOLLOWUP_LIMIT = int(_env("FOLLOWUP_LIMIT", "10"))      # follow-ups per UTC day
MAX_COMBOS_PER_RUN = int(_env("MAX_COMBOS", "4"))
LEADS_PER_SEARCH = int(_env("LEADS_PER_SEARCH", "25"))
FOLLOWUP_AFTER_DAYS = int(_env("FOLLOWUP_AFTER_DAYS", "3"))
FOLLOWUP_MAX_AGE_DAYS = int(_env("FOLLOWUP_MAX_AGE_DAYS", "14"))
BOUNCE_PAUSE_RATE = float(_env("BOUNCE_PAUSE_RATE", "0.15"))
BOUNCE_MIN_SAMPLE = int(_env("BOUNCE_MIN_SAMPLE", "30"))
BOUNCE_MIN_COUNT = int(_env("BOUNCE_MIN_COUNT", "4"))
FORCE_RESUME = _env("FORCE_RESUME", "false").lower() == "true"
MAX_RUNTIME_S = int(_env("MAX_RUNTIME_MIN", "40")) * 60  # stop early so the log always gets saved
SLEEP_MIN, SLEEP_MAX = 60, 150                           # slower, human-like pacing helps deliverability
WARMUP = _env("WARMUP", "true").lower() == "true"        # ramp volume up slowly: 3/day, 5, 8, then DAILY_LIMIT
SCORE_MIN = int(_env("SCORE_MIN", "6"))                  # minimum "needs a call agent" score to contact a business

SENDER_NAME = _env("SENDER_NAME", "Ashish")
COMPANY_NAME = _env("COMPANY_NAME", "")  # optional; leave blank for transparent freelancer outreach
COMPANY_PHONE = _env("COMPANY_PHONE")
COMPANY_SITE = _env("COMPANY_WEBSITE")
COMPANY_ADDRESS = _env("COMPANY_ADDRESS")   # required to email US businesses (CAN-SPAM)
DELIVERY_DAYS = _env("DELIVERY_DAYS", "5-7 working days")
COMPANY_WHO = _env(
    "COMPANY_WHO",
    "I build websites and web apps, and I also develop AI call agents for businesses."
)
# The three lines below are the biggest reply-rate boosters. They are written by YOU (never by the AI), so they must be true.
DEMO_PHONE = _env("DEMO_PHONE")          # optional: only include a demo number when it is configured and working
PROOF_LINE = _env("PROOF_LINE")            # optional: set only to a verifiable, accurate proof point
PRICE_LINE = _env("PRICE_LINE")            # optional: set only to a real, approved pricing statement
SERVICE_OFFERINGS = _env(
    "SERVICE_OFFERINGS",
    "business websites | custom web applications | mobile apps | AI-powered automation and voice agents",
)
# What the agent can do (pipe-separated in the PRODUCT_FEATURES variable). Two random ones plus the business-specific
# "does" line are used per email, so every email describes the product a bit differently. Only list things you really offer.
PRODUCT_FEATURES = [f.strip() for f in _env("PRODUCT_FEATURES", "").split("|") if f.strip()] or [
    "answers every call, day and night",
    "talks to callers naturally instead of reading out a menu",
    "answers common questions such as hours, prices and location",
    "sends you a short summary of every call",
]

MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")        # change here if Google renames/retires it
LOG_CSV = "sent_log.csv"
CALL_CSV = "call_list.csv"
SUPPRESS_FILE = "suppression.txt"
LOG_FIELDS = ["date", "business", "city", "website", "domain", "email", "subject", "main_issue", "status",
              "message_id", "country", "category", "score"]
CALL_FIELDS = ["date", "business", "category", "city", "country", "website", "phone", "email", "instagram",
               "facebook", "rating", "reviews", "angle", "score", "what_to_say"]
# 'main_issue' in the log stores the email ANGLE (after_hours, no_site, busy, no_chat, general)
BLOCKING_STATUSES = {"sent", "no_email", "low_fit", "no_phone", "bad_email", "dup_email", "optout"}
START_TS = time.time()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HimalayaITBot/4.0)",
    "Accept-Language": "en;q=0.9,*;q=0.5",
}

# (city, country, latitude, longitude). English-friendly business markets worldwide.
CITIES = [
    ("Lucknow", "India", 26.85, 80.95), ("Kanpur", "India", 26.45, 80.35), ("Prayagraj", "India", 25.43, 81.85),
    ("Varanasi", "India", 25.32, 82.97), ("Agra", "India", 27.18, 78.01), ("Meerut", "India", 28.98, 77.71),
    ("Ghaziabad", "India", 28.67, 77.45), ("Noida", "India", 28.57, 77.32), ("Jaipur", "India", 26.91, 75.79),
    ("Indore", "India", 22.72, 75.86), ("Bhopal", "India", 23.26, 77.41), ("Nagpur", "India", 21.15, 79.09),
    ("Surat", "India", 21.17, 72.83), ("Vadodara", "India", 22.31, 73.18), ("Rajkot", "India", 22.30, 70.80),
    ("Ahmedabad", "India", 23.02, 72.57), ("Coimbatore", "India", 11.02, 76.96), ("Kochi", "India", 9.93, 76.27),
    ("Visakhapatnam", "India", 17.69, 83.22), ("Hyderabad", "India", 17.39, 78.49), ("Pune", "India", 18.52, 73.86),
    ("Chandigarh", "India", 30.73, 76.78), ("Ludhiana", "India", 30.90, 75.86), ("Dehradun", "India", 30.32, 78.03),
    ("Patna", "India", 25.59, 85.14), ("Ranchi", "India", 23.34, 85.31), ("Bhubaneswar", "India", 20.30, 85.82),
    ("Guwahati", "India", 26.14, 91.74), ("Nashik", "India", 20.00, 73.79), ("Delhi", "India", 28.61, 77.21),
    ("Mumbai", "India", 19.08, 72.88), ("Bengaluru", "India", 12.97, 77.59), ("Chennai", "India", 13.08, 80.27),
    ("Kolkata", "India", 22.57, 88.36),
    ("New York", "United States", 40.71, -74.01), ("Los Angeles", "United States", 34.05, -118.24),
    ("Chicago", "United States", 41.88, -87.63), ("Houston", "United States", 29.76, -95.37),
    ("Phoenix", "United States", 33.45, -112.07), ("Dallas", "United States", 32.78, -96.80),
    ("Austin", "United States", 30.27, -97.74), ("San Diego", "United States", 32.72, -117.16),
    ("Miami", "United States", 25.76, -80.19), ("Atlanta", "United States", 33.75, -84.39),
    ("Seattle", "United States", 47.61, -122.33), ("Denver", "United States", 39.74, -104.99),
    ("Boston", "United States", 42.36, -71.06), ("Orlando", "United States", 28.54, -81.38),
    ("Charlotte", "United States", 35.23, -80.84),
    ("Toronto", "Canada", 43.65, -79.38), ("Vancouver", "Canada", 49.28, -123.12),
    ("Calgary", "Canada", 51.05, -114.07), ("Montreal", "Canada", 45.50, -73.57),
    ("London", "United Kingdom", 51.51, -0.13), ("Manchester", "United Kingdom", 53.48, -2.24),
    ("Birmingham", "United Kingdom", 52.49, -1.89), ("Leeds", "United Kingdom", 53.80, -1.55),
    ("Glasgow", "United Kingdom", 55.86, -4.25), ("Bristol", "United Kingdom", 51.45, -2.59),
    ("Dublin", "Ireland", 53.35, -6.26),
    ("Sydney", "Australia", -33.87, 151.21), ("Melbourne", "Australia", -37.81, 144.96),
    ("Brisbane", "Australia", -27.47, 153.03), ("Perth", "Australia", -31.95, 115.86),
    ("Adelaide", "Australia", -34.93, 138.60), ("Auckland", "New Zealand", -36.85, 174.76),
    ("Dubai", "United Arab Emirates", 25.20, 55.27), ("Abu Dhabi", "United Arab Emirates", 24.45, 54.38),
    ("Singapore", "Singapore", 1.35, 103.82), ("Kuala Lumpur", "Malaysia", 3.14, 101.69),
    ("Manila", "Philippines", 14.60, 120.98), ("Hong Kong", "Hong Kong", 22.32, 114.17),
    ("Karachi", "Pakistan", 24.86, 67.01), ("Lahore", "Pakistan", 31.55, 74.34),
    ("Dhaka", "Bangladesh", 23.81, 90.41), ("Colombo", "Sri Lanka", 6.93, 79.86), ("Kathmandu", "Nepal", 27.72, 85.32),
    ("Lagos", "Nigeria", 6.52, 3.38), ("Accra", "Ghana", 5.60, -0.19), ("Nairobi", "Kenya", -1.29, 36.82),
    ("Johannesburg", "South Africa", -26.20, 28.05), ("Cape Town", "South Africa", -33.92, 18.42),
    ("Amsterdam", "Netherlands", 52.37, 4.90), ("Stockholm", "Sweden", 59.33, 18.07),
    ("Copenhagen", "Denmark", 55.68, 12.57),
]
# Canada is blocked by default (strict consent rules). Set BLOCKED_COUNTRIES=none to allow it.
BLOCKED_COUNTRIES = {c.strip().lower() for c in os.environ.get("BLOCKED_COUNTRIES", "Canada").split(",")
                     if c.strip() and c.strip().lower() != "none"}
# In these places, emailing a person's private address (gmail etc.) is not allowed without consent:
# only company-domain addresses are used there.
STRICT_COUNTRIES = {"United Kingdom", "Ireland", "Netherlands", "Sweden", "Denmark"}
SEARCH_RADIUS_M = int(_env("SEARCH_RADIUS_M", "12000"))
INCLUDE_CHAINS = _env("INCLUDE_CHAINS", "false").lower() == "true"

# Businesses where a missed phone call = a lost customer.
#   weight   : how phone-dependent the category is (3 = very, 2 = quite)
#   customer : who calls   |  call_for : what they call to do   |  does : what our agent does for them
CATEGORIES = {
    "dental clinics": {
        "osm": [("amenity", "dentist")], "q": ["dental clinic", "dentist"], "weight": 3,
        "customer": "patient", "call_for": "book an appointment", "does": "books and reschedules appointments"},
    "clinics and doctors": {
        "osm": [("amenity", "clinic"), ("amenity", "doctors")],
        "q": ["clinic", "skin clinic", "physiotherapy clinic"], "weight": 3,
        "customer": "patient", "call_for": "book an appointment", "does": "books and reschedules appointments"},
    "veterinary clinics": {
        "osm": [("amenity", "veterinary")], "q": ["veterinary clinic", "pet clinic"], "weight": 3,
        "customer": "pet owner", "call_for": "book a visit", "does": "books visits and takes basic details about the pet"},
    "diagnostic labs": {
        "osm": [("healthcare", "laboratory")], "q": ["diagnostic lab", "pathology lab"], "weight": 2,
        "customer": "patient", "call_for": "book a test", "does": "books tests and home sample collection"},
    "salons and spas": {
        "osm": [("shop", "hairdresser"), ("shop", "beauty")], "q": ["beauty salon", "spa", "hair salon"], "weight": 3,
        "customer": "client", "call_for": "book a slot", "does": "books and reschedules appointments"},
    "restaurants": {
        "osm": [("amenity", "restaurant")], "q": ["restaurant", "family restaurant"], "weight": 2,
        "customer": "guest", "call_for": "reserve a table", "does": "takes table reservations and answers menu questions"},
    "hotels and guest houses": {
        "osm": [("tourism", "hotel"), ("tourism", "guest_house")], "q": ["hotel", "guest house"], "weight": 3,
        "customer": "guest", "call_for": "check room availability", "does": "answers room and rate enquiries and takes booking requests"},
    "gyms and yoga studios": {
        "osm": [("leisure", "fitness_centre")], "q": ["gym", "yoga studio"], "weight": 2,
        "customer": "prospective member", "call_for": "ask about membership", "does": "answers membership and timing questions and books trial sessions"},
    "real estate agencies": {
        "osm": [("office", "estate_agent")], "q": ["real estate agency"], "weight": 3,
        "customer": "buyer", "call_for": "enquire about a property", "does": "captures buyer and renter enquiries and books property viewings"},
    "lawyers": {
        "osm": [("office", "lawyer")], "q": ["lawyer", "law firm"], "weight": 2,
        "customer": "client", "call_for": "book a consultation", "does": "takes new-client details and books consultations"},
    "car repair and dealers": {
        "osm": [("shop", "car_repair"), ("shop", "car")], "q": ["car repair", "car service center", "car dealer"], "weight": 3,
        "customer": "customer", "call_for": "book a service", "does": "books service slots and captures enquiries"},
    "home services": {
        "osm": [("craft", "plumber"), ("craft", "electrician"), ("craft", "hvac")],
        "q": ["plumber", "electrician", "AC repair service"], "weight": 3,
        "customer": "customer", "call_for": "get a job quoted", "does": "captures job details and books visits"},
    "coaching and schools": {
        "osm": [("amenity", "school"), ("amenity", "college"), ("amenity", "language_school")],
        "q": ["coaching institute", "tutoring center", "private school"], "weight": 2,
        "customer": "parent", "call_for": "ask about admissions", "does": "answers admission and fee questions and books campus visits"},
    "event and wedding venues": {
        "osm": [("amenity", "events_venue")], "q": ["wedding venue", "banquet hall"], "weight": 2,
        "customer": "customer", "call_for": "check a date", "does": "answers date and pricing enquiries and books venue visits"},
    "travel agencies": {
        "osm": [("shop", "travel_agency")], "q": ["travel agency"], "weight": 2,
        "customer": "traveller", "call_for": "enquire about a trip", "does": "captures trip enquiries and books consultation calls"},
}

# Markers searched in the homepage source (script tags included, so JS-injected widgets are caught).
CHAT_MARKERS = ("tawk.to", "intercom", "drift.com", "crisp.chat", "tidio", "livechatinc", "livechat", "hs-scripts",
                "zendesk", "zopim", "freshchat", "olark", "smartsupp", "manychat", "botpress", "landbot", "chatbot",
                "chat-widget", "jivochat", "gupshup", "wati.io", "ai-assistant", "verloop", "yellow.ai")
BOOKING_MARKERS = ("calendly", "booksy", "fresha", "setmore", "acuity", "squareup.com/appointments", "simplybook",
                   "mindbody", "opentable", "resy.com", "zenoti", "practo", "zocdoc", "bookingbug", "cal.com",
                   "book online", "online booking", "online appointment", "book your appointment online",
                   "reserve online", "book a table online", "appointy", "vagaro", "timetap", "bookeo")


# ─────────────────────────────────────────────
# GEMINI (optional, free tier) with circuit breaker
# ─────────────────────────────────────────────
_client = None
_gemini_failures = 0


def gemini() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def gemini_call(prompt: str, json_mode: bool = False, retries: int = 2, temperature: float = 0.7) -> str:
    """Plain Gemini text call. After 2 failed calls it is switched off for the run (template email is used)."""
    global _gemini_failures
    if not GEMINI_API_KEY or _gemini_failures >= 2:
        return ""
    cfg = {"temperature": temperature}
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    for attempt in range(1, retries + 1):
        try:
            resp = gemini().models.generate_content(
                model=MODEL, contents=prompt, config=types.GenerateContentConfig(**cfg)
            )
            _gemini_failures = 0
            return resp.text or ""
        except Exception as e:  # noqa: BLE001
            log.warning(f"Gemini call failed (attempt {attempt}/{retries}): {e}")
            time.sleep(15 * attempt)
    _gemini_failures += 1
    return ""


def parse_json(text: str, want=dict):
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, want) else None
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]" if want is list else r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                data = json.loads(m.group())
                return data if isinstance(data, want) else None
            except json.JSONDecodeError:
                return None
    return None


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def migrate_csv(path: str, fields: list) -> None:
    """If an older file has different columns, rewrite it with the new columns (nothing is lost)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        old, rows = reader.fieldnames or [], list(reader)
    if old == fields:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def ensure_log() -> None:
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()
    else:
        migrate_csv(LOG_CSV, LOG_FIELDS)


def load_rows() -> list:
    with open(LOG_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def record(row: dict) -> None:
    row.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow({k: row.get(k, "") for k in LOG_FIELDS})


def parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_seen(rows: list) -> set:
    """Domains/keys we never process again. 'unreachable' only blocks after 2 failures (could be a blip)."""
    seen, unreachable = set(), {}
    for r in rows:
        d, st = r.get("domain", ""), r.get("status")
        if st in BLOCKING_STATUSES:
            seen.add(d)
        elif st == "unreachable":
            unreachable[d] = unreachable.get(d, 0) + 1
    return seen | {d for d, n in unreachable.items() if n >= 2}


def load_seen_emails(rows: list) -> set:
    return {(r.get("email") or "").lower() for r in rows
            if r.get("status") in ("sent", "followup_sent") and r.get("email")}


def load_suppressed() -> set:
    if not os.path.exists(SUPPRESS_FILE):
        return set()
    with open(SUPPRESS_FILE, encoding="utf-8") as f:
        return {ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")}


def add_suppression(value: str) -> None:
    value = (value or "").strip().lower()
    if not value:
        return
    if value in load_suppressed():
        return
    with open(SUPPRESS_FILE, "a", encoding="utf-8") as f:
        f.write(value + "\n")


def count_today(rows: list, status: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for r in rows if r.get("status") == status and (r.get("date") or "").startswith(today))


def effective_daily_limit(rows: list) -> int:
    """New senders must ramp up slowly or Gmail files everything in spam: 3/day, then 5, 8, then the full limit."""
    if not WARMUP:
        return DAILY_LIMIT
    firsts = [d for d in (parse_dt(r.get("date")) for r in rows if r.get("status") == "sent") if d]
    if not firsts:
        return min(DAILY_LIMIT, 3)
    age = (datetime.now(timezone.utc) - min(firsts)).days
    cap = 3 if age < 7 else 5 if age < 14 else 8 if age < 28 else DAILY_LIMIT
    return min(DAILY_LIMIT, cap)


_call_seen: set = set()


def ensure_call_log() -> None:
    if os.path.exists(CALL_CSV):
        with open(CALL_CSV, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != CALL_FIELDS:                     # older format: keep it, start a fresh file
            os.replace(CALL_CSV, CALL_CSV.replace(".csv", "_old.csv"))
    if not os.path.exists(CALL_CSV):
        with open(CALL_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CALL_FIELDS).writeheader()
    with open(CALL_CSV, newline="", encoding="utf-8") as f:
        _call_seen.update(r.get("website", "") for r in csv.DictReader(f))


def add_call_lead(biz: dict, website: str, phone: str, email: str, angle: str, score: int, fact: str) -> None:
    """Good-fit lead + phone number = a lead you can also call / WhatsApp yourself."""
    if not phone or website in _call_seen:
        return
    _call_seen.add(website)
    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "business": biz["name"], "category": biz["category"],
        "city": biz["city"], "country": biz.get("country", ""), "website": website, "phone": phone,
        "email": email or "", "instagram": biz.get("instagram") or "", "facebook": biz.get("facebook") or "",
        "rating": biz.get("rating") or "", "reviews": biz.get("reviews") or "",
        "angle": angle, "score": score, "what_to_say": fact,
    }
    with open(CALL_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CALL_FIELDS).writerow(row)


# ─────────────────────────────────────────────
# DISCOVERY (OpenStreetMap, free)
# ─────────────────────────────────────────────
def host_of(url: str) -> str:
    if "//" not in url:
        url = "https://" + url
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_public_host(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


DIRECTORY_DOMAINS = (
    "justdial", "indiamart", "sulekha", "tripadvisor", "zomato", "swiggy", "facebook", "instagram",
    "linkedin", "youtube", "wikipedia", "google.", "practo", "magicbricks", "99acres", "housing.com",
    "makemytrip", "booking.com", "lybrate", "urbanclap", "urbancompany", "clinicspots", "yelp",
    "yellowpages", "trustpilot", "airbnb", "expedia", "opentable", "tiktok", "twitter.", "x.com",
    "pinterest", "maps.", "goo.gl", "apple.com", "groupon", "healthgrades", "zocdoc", "doctolib", "treatwell",
    "business.site", "linktr.ee", "wa.me",
)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def clean_osm_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    for e in re.split(r"[;,\s]+", raw.lower()):
        if EMAIL_RE.fullmatch(e) and not any(j in e for j in JUNK_SUBSTR) and not e.endswith(JUNK_EXT):
            return e
    return None


def clean_phone(raw: Optional[str]) -> str:
    if not raw:
        return ""
    first = re.split(r"[;,/]", str(raw))[0]
    p = re.sub(r"[^\d+]", "", first)
    return p if 7 <= len(re.sub(r"\D", "", p)) <= 15 else ""


def overpass_query(lat: float, lon: float, tags: list) -> str:
    # a business qualifies if it lists a website OR a phone number
    parts = "".join(
        f'nwr["{k}"="{v}"][~"^(contact:)?(website|phone)$"~"."](around:{SEARCH_RADIUS_M},{lat},{lon});' for k, v in tags
    )
    return f"[out:json][timeout:90];({parts});out tags 200;"


def discover(city: tuple, category: str) -> list:
    name_, country, lat, lon = city
    log.info(f"🔎 Discovering (OpenStreetMap): {category} in {name_}, {country}")
    time.sleep(2)                                   # be polite to the free public servers
    query, data = overpass_query(lat, lon, CATEGORIES[category]["osm"]), None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=120)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                log.warning(f"   Overpass {url} returned {resp.status_code}")
            except (requests.RequestException, ValueError) as e:
                log.warning(f"   Overpass {url} failed: {e}")
        if data is not None:
            break
        time.sleep(15 * (attempt + 1))

    elements = (data or {}).get("elements", [])
    random.Random(f"{date.today()}-{name_}-{category}").shuffle(elements)
    out, seen_keys = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        site = tags.get("website") or tags.get("contact:website")
        biz_name = (tags.get("name") or "").strip()
        if not biz_name:
            continue
        if not INCLUDE_CHAINS and (tags.get("brand") or tags.get("brand:wikidata") or tags.get("operator:wikidata")):
            continue
        h = host_of(str(site)) if site else ""
        if h and ("." not in h or any(d in h for d in DIRECTORY_DOMAINS)):
            h = ""
        phone = clean_phone(tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile") or tags.get("mobile"))
        if not h and not phone:
            continue
        key = h or f"osm:{el.get('type')}{el.get('id')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append({
            "name": biz_name, "host": h, "city": name_, "country": country, "category": category,
            "key": key, "tz": None, "place_id": "", "rating": None, "reviews": None, "instagram": "", "facebook": "",
            "source": "osm", "hours": None,
            "api_emails": [e for e in [clean_osm_email(tags.get("email") or tags.get("contact:email"))] if e],
            "phone": phone,
        })
        if len(out) >= LEADS_PER_SEARCH:
            break
    log.info(f"   → {len(out)} candidate businesses")
    return out


# ── RapidAPI "Local Business Data" (Google Maps data) ─────────────────────────────
RAPID_URL = "https://local-business-data.p.rapidapi.com/search"
RAPID_HOST = "local-business-data.p.rapidapi.com"
USAGE_FILE = "api_usage.json"
_rapid_dead = False          # set when the key is invalid / quota is used up: fall back to OpenStreetMap

COUNTRY_CODE = {
    "India": "in", "United States": "us", "United Kingdom": "gb", "Ireland": "ie", "Australia": "au",
    "New Zealand": "nz", "United Arab Emirates": "ae", "Singapore": "sg", "Malaysia": "my", "Philippines": "ph",
    "Hong Kong": "hk", "Pakistan": "pk", "Bangladesh": "bd", "Sri Lanka": "lk", "Nepal": "np", "Nigeria": "ng",
    "Ghana": "gh", "Kenya": "ke", "South Africa": "za", "Netherlands": "nl", "Sweden": "se", "Denmark": "dk",
    "Canada": "ca",
}


def load_usage() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        d = {}
    if d.get("date") != today:
        d = {"date": today, "calls": 0, "offsets": d.get("offsets", {})}
    d.setdefault("offsets", {})
    return d


def save_usage(d: dict) -> None:
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def rapid_search(query: str, lat: float, lng: float, region: str, limit: int, offset: int) -> Optional[list]:
    """One API call. Returns a list of businesses, or None when the API cannot be used."""
    global _rapid_dead
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPID_HOST, "X-User-Agent": "desktop"}
    params = {"query": query, "limit": str(limit), "offset": str(offset), "language": "en", "region": region,
              "lat": str(lat), "lng": str(lng), "zoom": "12", "extract_emails_and_contacts": "true"}
    try:
        resp = requests.get(RAPID_URL, headers=headers, params=params, timeout=90)
    except requests.RequestException as e:
        log.warning(f"   RapidAPI request failed: {e}")
        return None
    remaining = resp.headers.get("x-ratelimit-requests-remaining")
    if remaining is not None:
        log.info(f"   RapidAPI requests remaining this period: {remaining}")
    if resp.status_code in (401, 403, 429):
        log.error(f"   RapidAPI returned {resp.status_code} (invalid key / not subscribed / quota used up): using OpenStreetMap instead")
        _rapid_dead = True
        return None
    if resp.status_code != 200:
        log.warning(f"   RapidAPI returned {resp.status_code}")
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if remaining is not None and str(remaining).strip() == "0":
        _rapid_dead = True                         # this call worked, but the plan is now empty
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


def discover_api(city: tuple, category: str, usage: dict) -> Optional[list]:
    name_, country, lat, lon = city
    phrase = random.Random(f"{date.today()}-{name_}-{category}").choice(CATEGORIES[category]["q"])
    okey = f"{name_}|{phrase}"
    offset = int(usage["offsets"].get(okey, 0))
    query = f"{phrase} in {name_}, {country}"
    log.info(f"🔎 Discovering (Google Maps via RapidAPI): '{query}' offset={offset}")
    data = rapid_search(query, lat, lon, COUNTRY_CODE.get(country, "us"), RAPID_LIMIT, offset)
    usage["calls"] += 1
    if data is None:
        return None
    usage["offsets"][okey] = offset + RAPID_LIMIT if len(data) >= RAPID_LIMIT else 0   # next page, or start over

    out, skipped = [], {"closed": 0, "big_or_chain": 0, "no_phone": 0, "duplicate": 0}
    seen_keys = set()
    for it in data:
        if not isinstance(it, dict) or not (it.get("name") or "").strip():
            continue
        if (it.get("business_status") or "OPEN") != "OPEN":
            skipped["closed"] += 1
            continue
        ec = it.get("emails_and_contacts") or {}
        emails = [e for e in (ec.get("emails") or []) if isinstance(e, str)]
        phones = ec.get("phone_numbers") or []
        try:
            reviews = int(it.get("review_count") or 0)
        except (TypeError, ValueError):
            reviews = 0
        # Many different emails/phones = a multi-location brand; 3000+ reviews = large business
        if not INCLUDE_CHAINS and (reviews > 3000 or len(set(emails)) >= 4 or len(set(phones)) >= 4):
            skipped["big_or_chain"] += 1
            continue
        site = it.get("website") or ""
        host = host_of(site) if site else ""
        if host and ("." not in host or any(d in host for d in DIRECTORY_DOMAINS)):
            host = ""                                 # a Facebook / listing page is not their own website
        phone = clean_phone(it.get("phone_number")) or (clean_phone(phones[0]) if phones else "")
        if not phone and not host:
            skipped["no_phone"] += 1
            continue
        key = host or f"gmaps:{it.get('place_id') or it.get('business_id') or it['name']}"
        if key in seen_keys:
            skipped["duplicate"] += 1
            continue
        seen_keys.add(key)
        out.append({
            "name": it["name"].strip(), "host": host, "key": key, "city": it.get("city") or name_, "country": country,
            "category": category, "phone": phone, "api_emails": emails, "tz": it.get("timezone"),
            "rating": it.get("rating"), "reviews": reviews, "place_id": it.get("place_id") or "",
            "instagram": ec.get("instagram") or "", "facebook": ec.get("facebook") or "", "source": "rapidapi",
            "hours": it.get("working_hours") or it.get("working_hours_old_format"),
        })
    log.info(f"   → {len(out)} usable of {len(data)} returned (skipped: {skipped})")
    return out


def discover_any(city: tuple, category: str, usage: dict) -> list:
    if RAPIDAPI_KEY and not _rapid_dead and usage["calls"] < RAPID_MAX_CALLS_PER_DAY:
        res = discover_api(city, category, usage)
        if res is not None:
            return res
        log.warning("   RapidAPI gave no usable answer: falling back to OpenStreetMap for this search")
    return discover(city, category)


def local_ok(tz_name: Optional[str]) -> bool:
    """Exact local business-hours check from the business's own timezone (Mon-Fri 08:30-17:00)."""
    if not BUSINESS_HOURS_ONLY or not tz_name:
        return True
    try:
        lt = datetime.now(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001
        return True
    return lt.weekday() < 5 and 8.5 <= lt.hour + lt.minute / 60 < 17


def pick_api_email(emails: list, city: str, host: str) -> Optional[str]:
    """Choose ONE sensible address from the API list. Never guess between branches."""
    good = []
    for e in emails or []:
        e = str(e).strip().lower()
        if not EMAIL_RE.fullmatch(e) or any(j in e for j in JUNK_SUBSTR) or e.endswith(JUNK_EXT):
            continue
        dom = e.split("@")[-1]
        if host and not (dom == host or dom.endswith("." + host) or host.endswith("." + dom) or dom in FREE_MAIL):
            continue                                  # third-party address (agency, hosting...)
        good.append(e)
    good = list(dict.fromkeys(good))
    if not good:
        return None
    if len(good) == 1:
        return good[0]
    token = re.sub(r"[^a-z]", "", city.lower())
    local = [e for e in good if token and token in re.sub(r"[^a-z]", "", e.split("@")[0])]
    if local:
        return local[0]
    generic = [e for e in good if e.split("@")[0] in PREFERRED_PREFIX]
    return generic[0] if generic else None


def in_business_hours(city: tuple, now: Optional[datetime] = None) -> bool:
    """Approximate local time from longitude: Mon-Fri, 08:30-17:00."""
    now = now or datetime.now(timezone.utc)
    lt = now + timedelta(hours=city[3] / 15)
    return lt.weekday() < 5 and 8.5 <= lt.hour + lt.minute / 60 < 17


def pick_combos(n: int, now: Optional[datetime] = None) -> list:
    now = now or datetime.now(timezone.utc)
    cities = [c for c in CITIES
              if c[1].lower() not in BLOCKED_COUNTRIES
              and not (c[1] == "United States" and not COMPANY_ADDRESS)
              and (not BUSINESS_HOURS_ONLY or in_business_hours(c, now))]
    combos = [(c, k) for c in cities for k in CATEGORIES]
    random.Random(f"{now.date()}-{now.hour}").shuffle(combos)
    return combos[:n]


# ─────────────────────────────────────────────
# FIT SCORING: does this business really need a call agent? (measured facts only)
# ─────────────────────────────────────────────
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m", re.I)


def fetch_site(host: str):
    """Returns the homepage response, or None if the site cannot be loaded."""
    for scheme, verify in (("https", True), ("https", False), ("http", True)):
        try:
            r = requests.get(f"{scheme}://{host}/", headers=HEADERS, timeout=15, allow_redirects=True, verify=verify)
            if r.status_code < 400 and "html" in r.headers.get("Content-Type", "").lower():
                return r
        except requests.RequestException:
            continue
    return None


def site_signals(r) -> dict:
    """What their website does (or does not) offer to someone who wants to reach them."""
    low = r.text.lower()
    soup = BeautifulSoup(r.text, "html.parser")
    hrefs = [(a.get("href") or "").lower() for a in soup.find_all("a")]
    return {
        "chat": any(k in low for k in CHAT_MARKERS),
        "booking": any(k in low for k in BOOKING_MARKERS),
        "tel": any(h.startswith("tel:") for h in hrefs),
        "form": soup.find("form") is not None,
    }


def parse_hours(wh) -> dict:
    """Read the Google Maps opening hours. Only reports what it can actually parse."""
    out = {"known": False, "closed_days": [], "early_close": False, "typical_close": None}
    try:
        if not isinstance(wh, dict):
            return out
        days = {}
        for k, v in wh.items():
            name = str(k).strip().lower()
            if name in DAY_NAMES:
                days[name] = " ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        if len(days) < 5:
            return out
        closes = []
        for d in DAY_NAMES:
            s = days.get(d)
            if s is None:
                continue
            low = s.lower()
            if "24 hours" in low or "open 24" in low:
                closes.append(24.0)
                continue
            times = TIME_RE.findall(s)
            if "closed" in low and not times:
                out["closed_days"].append(d.capitalize())
                continue
            if times:
                h, mnt, ap = times[-1]
                hh = int(h) % 12 + (12 if ap.lower() == "p" else 0)
                if hh == 0:
                    hh = 24
                closes.append(hh + int(mnt or 0) / 60)
        out["known"] = True
        if len(closes) >= 4:
            closes.sort()
            med = closes[len(closes) // 2]
            out["typical_close"] = med
            out["early_close"] = med <= 19.0
    except Exception:  # noqa: BLE001
        return {"known": False, "closed_days": [], "early_close": False, "typical_close": None}
    return out


def hours_fact(hrs: dict) -> str:
    if hrs["closed_days"]:
        return f"Your Google listing shows you're closed on {' and '.join(hrs['closed_days'][:2])}."
    if hrs["early_close"] and hrs["typical_close"]:
        h12 = int(hrs["typical_close"] + 0.5) % 12 or 12
        return f"Your Google listing shows you usually close by around {h12} PM."
    return ""


def score_lead(biz: dict, site: str, sig: dict, hrs: dict) -> tuple:
    """Returns (score, angle, fact). 'site' is 'none' (no website), 'ok' (loaded) or 'unknown' (could not load)."""
    info = CATEGORIES[biz["category"]]
    pts = info["weight"]
    reviews = int(biz.get("reviews") or 0)
    facts = {}

    if site == "none":
        pts += 3
        facts["no_site"] = f"I couldn't find a website for {display_name(biz['name']) or 'your business'} on Google Maps."
    elif site == "ok":
        if not sig.get("chat"):
            pts += 2
        if not sig.get("booking"):
            pts += 1
        if sig.get("tel") and not sig.get("form"):
            pts += 1
        if not sig.get("chat") and not sig.get("booking"):
            facts["no_chat"] = "I couldn't find online booking or live chat on your website."
        elif not sig.get("chat"):
            facts["no_chat"] = "I couldn't find live chat on your website."

    hf = hours_fact(hrs)
    if hf:
        pts += 2
        facts["after_hours"] = hf
    if reviews >= 100:
        pts += 2
    elif reviews >= 30:
        pts += 1
    if reviews >= 30:
        facts["busy"] = f"With {reviews} Google reviews, plenty of people clearly find you."

    # Match the email to the strongest observed business need, not always to phone automation.
    if "no_site" in facts:
        return pts, "no_site", facts["no_site"]
    if "after_hours" in facts:
        return pts, "after_hours", facts["after_hours"]
    if "no_chat" in facts and site == "ok" and not sig.get("form"):
        return pts, "no_chat", facts["no_chat"]
    if site == "ok" and not sig.get("booking"):
        return pts, "no_booking", "I couldn't find an obvious online booking or enquiry flow on your website."
    if "busy" in facts:
        return pts, "busy", facts["busy"]
    return pts, "general", ""


# ─────────────────────────────────────────────
# EMAIL DISCOVERY / VERIFICATION
# ─────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.uk", "yahoo.com.au", "outlook.com",
             "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
             "proton.me", "protonmail.com", "rediffmail.com", "zoho.com", "mail.com"}
JUNK_SUBSTR = ("sentry", "wixpress", "example", "yourdomain", "domain.com", "email.com", "noreply",
               "no-reply", "donotreply", "u003e", "@2x", "godaddy", "privacy@", "abuse@")
JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff")
PREFERRED_PREFIX = ("info", "contact", "hello", "enquiry", "inquiry", "sales", "care", "support", "admin", "office",
                    "reception", "appointments", "booking", "reservations")


def _decode_cf(enc: str) -> str:
    key = int(enc[:2], 16)
    return "".join(chr(int(enc[i:i + 2], 16) ^ key) for i in range(2, len(enc), 2))


def emails_from_html(html: str) -> set:
    found = set(EMAIL_RE.findall(html))
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            found.update(EMAIL_RE.findall(a["href"]))
    for el in soup.select("[data-cfemail]"):
        try:
            found.add(_decode_cf(el["data-cfemail"]))
        except Exception:  # noqa: BLE001
            pass
    return found


def site_phone(html: str) -> str:
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            p = clean_phone(a["href"][4:])
            if p:
                return p
    return ""


def find_public_email(host: str, home) -> Optional[str]:
    pages = [home.url]
    soup = BeautifulSoup(home.text, "html.parser")
    for a in soup.find_all("a", href=True):
        label = (a.get_text(" ", strip=True) + " " + a["href"]).lower()
        if any(k in label for k in ("contact", "about", "reach", "enquiry")):
            u = urljoin(home.url, a["href"])
            if host_of(u) == host and u not in pages:
                pages.append(u)
    for path in ("contact", "contact-us", "about", "about-us"):
        pages.append(urljoin(home.url, "/" + path))

    candidates, visited = set(emails_from_html(home.text)), {home.url}
    for u in pages[1:6]:
        if u in visited:
            continue
        visited.add(u)
        try:
            resp = requests.get(u, headers=HEADERS, timeout=12)
            if resp.status_code < 400:
                candidates |= emails_from_html(resp.text)
        except requests.RequestException:
            continue

    scored = []
    for e in candidates:
        e = e.strip(".").lower()
        if any(j in e for j in JUNK_SUBSTR) or e.endswith(JUNK_EXT):
            continue
        dom = e.split("@")[-1]
        if dom == host or dom.endswith("." + host) or host.endswith("." + dom):
            rank = 0
        elif dom in FREE_MAIL:
            rank = 1
        else:
            continue
        scored.append((rank, 0 if e.split("@")[0] in PREFERRED_PREFIX else 1, e))
    scored.sort()
    return scored[0][2] if scored else None


def verify_email(email: str) -> tuple:
    """MX check only (port 25 probing is blocked on GitHub Actions and many servers accept-all)."""
    if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", email):
        return False, "bad syntax"
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 8
        answers = resolver.resolve(email.split("@")[-1], "MX")
        if not len(answers):
            return False, "no MX"
        if all(str(a.exchange).rstrip(".") == "" for a in answers):   # RFC 7505 "null MX": domain accepts no mail
            return False, "null MX"
        return True, "MX ok"
    except Exception as e:  # noqa: BLE001
        return False, f"MX lookup failed: {e}"


# ─────────────────────────────────────────────
# EMAIL WRITING  (every email is unique + spam-checked before it is sent)
# ─────────────────────────────────────────────
# Words/phrases that spam filters (and people) associate with bulk marketing. Any email containing one is rejected.
SPAM_RE = re.compile(
    r"guarantee|100\s?%|best price|cheapest|act now|limited time|\bfree\b|\boffers?\b|discount|click here|buy now|"
    r"risk[- ]free|no obligation|winner|congratulations|urgent|\bcash\b|\bearn\b|income|make money|promotion|"
    r"dear (?:sir|madam|friend)|once in a lifetime|don'?t miss|exclusive|\bsale\b|special deal|[$₹€£]\s?\d|!|"
    r"\bcheap\b|\bbonus\b|\bprize\b|unsubscribe now|this is not spam|\bsir\b|\bmadam\b|outreach", re.I)

HOOKS = {
    "no_booking": [
        "Would a customer who wants to book or enquire be able to do that easily on your website?",
        "I couldn't spot an obvious online booking or enquiry flow. Is that something you're looking to improve?",
        "How do customers usually send an enquiry through your website today?"
    ],
    "after_hours": [
        "When a {customer} wants to {call_for} after you've closed, who picks up the phone?",
        "What happens to the call from a {customer} who wants to {call_for} once you've shut for the day?",
        "If a {customer} rings at 8 pm to {call_for}, does anyone answer?",
        "A {customer} who wants to {call_for} usually calls when they're free, often after hours. Who answers?",
    ],
    "no_site": [
        "When a new {customer} finds you on Google Maps and wants to {call_for}, the phone is their only option. What happens when you can't pick up?",
        "A {customer} who finds you on Google Maps has one way to {call_for}: call. What happens when the line is busy?",
        "Is every call from a {customer} who wants to {call_for} getting answered?",
        "For someone who wants to {call_for}, you're one phone call away. What if nobody answers?",
    ],
    "busy": [
        "You clearly get plenty of calls. What happens to the ones that arrive while your team is with someone else?",
        "How many {customer}s ring to {call_for} and get no answer at peak times?",
        "When two {customer}s call at the same moment to {call_for}, who gets the second call?",
        "A busy business means a busy phone. How many calls go unanswered at peak times?",
    ],
    "no_chat": [
        "What happens when a {customer} calls to {call_for} and everyone is busy?",
        "If a {customer} can't reach you to {call_for}, they usually call the next business on the list. How often does that happen?",
        "How many calls from {customer}s who want to {call_for} do you miss in a typical week?",
        "A {customer} who decides to {call_for} usually calls once. What if nobody picks up?",
    ],
    "general": [
        "What happens when a {customer} calls to {call_for} and nobody can answer?",
        "If a {customer} can't get through to {call_for}, do they call back or try someone else?",
        "How many calls from {customer}s who want to {call_for} do you miss in a typical week?",
        "Who answers the phone when the whole team is busy?",
    ],
}
WHO_VARIANTS = [
    "I build business websites, web apps and AI call agents.",
    "I help businesses with websites, web applications and AI-powered call agents.",
    "My work includes website development, web apps and AI call agents for businesses.",
]
INTROS = [
    "I'm {sender}, an independent developer. {who}",
    "My name is {sender}; I work as a freelance developer. {who}",
    "I'm {sender}, a freelance developer who builds digital tools for businesses. {who}",
]
FEATURE_SENTENCES = [
    "We build an AI receptionist for the phone. It {a}.",
    "Our AI receptionist {a}.",
    "What we make is an AI receptionist that {a}.",
    "We make an AI phone receptionist that {a}.",
]
# The #1 reason small-business owners ignore this kind of email: "it will replace my staff / annoy my callers".
POSITIONING = [
    "It's meant for the calls your team can't get to, not to replace anyone.",
    "It covers lunch breaks, busy moments and after hours; your team handles everything else.",
    "It only steps in when your team can't pick up.",
    "Your staff stay in charge; it just makes sure no call goes unanswered.",
]
TIMINGS = [
    "Setup takes {d}.",
    "",
    "",
    "",
]
CTA_DEMO = [      # used when DEMO_PHONE is set: the prospect can hear it in a minute, no meeting needed
    "Easiest way to judge it: call {demo} and talk to it like a {customer} who wants to {call_for}. It takes a minute.",
    "You can hear it yourself. Call {demo} and pretend to be a {customer} who wants to {call_for}.",
    "Call {demo} and try it as a {customer} would. If it sounds right, just reply and I'll set one up.",
]
CTA_SERVICE = {
    "no_site": [
        "Would it be useful if I sent two website ideas tailored to your business?",
        "Open to seeing a couple of practical website ideas for your business?",
        "Would you be the right person to ask about improving your website presence?"
    ],
    "no_chat": [
        "Would it help if I sent one or two ideas to make customer enquiries easier?",
        "Open to a couple of practical ideas for improving the enquiry flow?",
        "Would you like me to send a short example of how this could work?"
    ],
    "no_booking": [
        "Would it be useful to see a simple example of how online enquiries or booking could work?",
        "Is improving online enquiries something you're considering this year?",
        "Would you be open to one practical idea for making enquiries easier?"
    ],
    "general": [
        "Are you the right person to speak with about your website or digital tools?",
        "Would it be useful if I sent one relevant idea based on your business?",
        "Is improving your website or digital workflow on your radar this year?"
    ],
}
SUBJECTS.update({
    "no_site": ["Website for {name}", "A website idea for {name}", "{name} online"],
    "no_chat": ["A website idea for {name}", "Customer enquiries at {name}", "One idea for {name}"],
    "no_booking": ["Online enquiries at {name}", "Booking flow for {name}", "One idea for {name}"],
    "general": ["A digital idea for {name}", "Website and app development", "An idea for {name}"],
})
CTA_REPLY = [     # used when DEMO_PHONE is not set
    "Want to hear it? Reply \"yes\" and I'll set up a 2-minute demo where you play the {customer}.",
    "Reply \"demo\" and I'll arrange a short call so you can hear how it handles a {customer}.",
    "Curious how it sounds? Reply \"yes\" and I'll arrange a short demo.",
]
SUBJECTS = {
    "after_hours": ["Who answers {name} after hours?", "Calls after closing at {name}", "{name}: after-hours calls",
                    "When {name} is closed", "Phone at {name} after hours"],
    "no_site": ["Calls to {name}", "{name} on Google Maps", "When {name} can't pick up", "Reaching {name} by phone",
                "A question about {name}'s phone line"],
    "busy": ["Busy phone line at {name}?", "Missed calls at {name}", "{name}: calls while you're busy",
             "Second caller at {name}", "Peak-time calls at {name}"],
    "no_chat": ["Missed calls at {name}?", "When {name} can't pick up", "A question about {name}'s phone",
                "Who answers at {name}?", "Calls to {name} when busy"],
    "general": ["Missed calls at {name}?", "A question about {name}'s phone", "When {name} can't pick up",
                "Who answers at {name}?", "Phone calls at {name}"],
}
# Style directives given to Gemini (one random one per email) so no two emails share the same shape.
STYLES = [
    "Open directly with the question. Short sentences. Warm and plain.",
    "Open with one sentence about what you noticed, then ask the question. Slightly more conversational.",
    "Open with a tiny scenario (one sentence) of a caller who gets no answer, then ask how often that happens.",
    "Open with the question, then explain in a relaxed, matter-of-fact tone.",
    "Write like a quick note from one local professional to another. Use contractions. Keep it humble.",
]

_recent: list = []        # recently written emails [{"subject":..., "body":...}] used to guarantee uniqueness
RECENT_FILE = "recent_emails.json"
RECENT_KEEP = 40
SIM_MAX = float(_env("SIM_MAX", "0.40"))     # max allowed overlap (word 3-grams) with any recent email


def display_name(name: str) -> str:
    """Business name as a person would write it: no ALL CAPS, no mixed-script clutter. '' if it cannot be used."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    latin = " ".join(w for w in n.split() if w.isascii())     # 'DEEP GUEST HOUSE दीप गेस्ट हाउस' -> 'DEEP GUEST HOUSE'
    n = latin if len(latin) >= 3 else n
    n = re.sub(r"[\s|\-–—:,&]+$", "", n)
    if not n.isascii() or len(n) < 3:
        return ""
    if n.isupper():
        n = n.title()
    if len(n) > 42:
        n = n[:42].rsplit(" ", 1)[0]
    return n


def greeting(nm: str) -> str:
    return f"Hi {nm} team," if nm else "Hi there,"


def load_recent() -> list:
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def remember_email(subject: str, core: str) -> None:
    _recent.append({"subject": subject, "body": core})
    del _recent[:-RECENT_KEEP]
    if DRY_RUN:
        return
    try:
        with open(RECENT_FILE, "w", encoding="utf-8") as f:
            json.dump(_recent, f)
    except OSError:
        pass


def _shingles(text: str) -> set:
    w = re.findall(r"[a-z']+", text.lower())
    return {" ".join(w[i:i + 3]) for i in range(max(0, len(w) - 2))}


def max_similarity(core: str) -> float:
    a = _shingles(core)
    best = 0.0
    for r in _recent:
        b = _shingles(r.get("body", ""))
        if a and b:
            best = max(best, len(a & b) / len(a | b))
    return best


def spam_problems(subject: str, body: str) -> list:
    """Local pre-send spam check. Returns a list of problems (empty = clean)."""
    out = []
    text = subject + " " + body
    for line in (PRICE_LINE, PROOF_LINE):            # lines YOU wrote (price, proof) are allowed to contain numbers/currency
        if line:
            text = text.replace(line, " ")
    m = SPAM_RE.search(text)
    if m:
        out.append(f"spammy wording '{m.group()}'")
    if re.search(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|in|io|co|net|org|ly)\b", text, re.I):
        out.append("contains a link/domain")
    if re.search(r"\[[^\]]+\]|\{[^}]+\}", text):
        out.append("unfilled placeholder")
    letters = [c for c in body if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.12:
        out.append("too many capitals")
    if any(w.isupper() and len(w) > 3 for w in subject.split()):
        out.append("ALL-CAPS word in subject")
    if len(subject) > 60 or len(subject) < 8:
        out.append("subject length")
    if subject.lower().startswith(("re:", "fwd:", "fw:")):
        out.append("fake reply subject")
    if body.count("?") > 3:
        out.append("too many question marks")
    plain = body
    for line in (PRICE_LINE, PROOF_LINE):
        if line:
            plain = plain.replace(line, " ")
    words = len(plain.split())
    if not 45 <= words <= 115:
        out.append(f"length {words} words")
    if re.search(r"([.?,])\1", body):
        out.append("repeated punctuation")
    return out


def pick_features(info: dict) -> list:
    """What the agent does for THIS kind of business + one random general ability, in random order."""
    chosen = [info["does"]] + random.sample(PRODUCT_FEATURES, k=min(1, len(PRODUCT_FEATURES)))
    random.shuffle(chosen)
    return chosen


def closing_block(nm: str, info: dict, angle: str = "general") -> str:
    """Proof, price and the call to action. Written by CODE from YOUR settings, never invented by the AI."""
    fmt = {"customer": info["customer"], "call_for": info["call_for"], "name": nm or "your business",
           "demo": DEMO_PHONE}
    parts = [x for x in (PROOF_LINE, PRICE_LINE) if x]
    if angle in ("no_site", "no_chat", "no_booking", "general"):
        parts.append(random.choice(CTA_SERVICE.get(angle, CTA_SERVICE["general"])))
    else:
        parts.append(random.choice(CTA_DEMO if DEMO_PHONE else CTA_REPLY).format(**fmt))
    return "\n\n".join(parts)


def assemble(biz: dict, core: str, angle: str = "general") -> str:
    """Assemble a transparent, personal email without inventing how the lead was found."""
    nm = display_name(biz["name"])
    source = str(biz.get("source", "")).strip().lower().replace("_", " ")
    source_note = ""
    if source in ("google maps", "google map", "maps", "rapidapi"):
        source_note = (
            "I came across your business listing on Google Maps while researching local businesses."
        )
    parts = [greeting(nm)]
    if source_note:
        parts.append(source_note)
    parts.append(core)
    parts.append(closing_block(nm, CATEGORIES[biz['category']], angle))
    parts.append(signature(biz.get("country", "")))
    return "\n\n".join(parts)


def service_pitch(angle: str) -> str:
    """Choose a truthful service angle from the observed business need, not a one-size-fits-all AI receptionist pitch."""
    pitches = {
        "no_site": (
            "I could help with a professional, mobile-friendly website that clearly presents your services "
            "and makes it easy for customers to contact you. I also build custom web applications and mobile apps."
        ),
        "no_chat": (
            "I build and improve business websites and web apps, including useful enquiry flows and customer "
            "communication features where they fit. I also develop mobile apps and AI-powered tools."
        ),
        "no_booking": (
            "I build business websites and web apps that can make enquiries and appointment requests easier "
            "to manage. I also develop mobile apps and custom software when a business needs them."
        ),
        "after_hours": (
            "I develop AI phone receptionists to help handle calls when a team is busy or closed. I also build "
            "business websites, web apps, mobile apps and custom software."
        ),
        "busy": (
            "I develop AI phone receptionists to help handle calls when a team is busy or cannot pick up. I also "
            "build business websites, web apps, mobile apps and custom software."
        ),
        "general": (
            "I build business websites, custom web applications, mobile apps and AI-powered automation, "
            "choosing the solution around the business's actual needs."
        ),
    }
    return pitches.get(angle, pitches["general"])


def write_email(biz: dict, angle: str, fact: str) -> Optional[dict]:
    """Gemini writes a personalized email around the most relevant software service, with safe factual claims."""
    info = CATEGORIES[biz["category"]]
    nm = display_name(biz["name"]) or "your business"
    hook_hint = random.choice(HOOKS[angle]).format(name=nm, customer=info["customer"], call_for=info["call_for"])
    a, b = pick_features(info)
    avoid = "\n".join(f"- {r['subject']}  |  {' '.join(r['body'].split()[:14])}..." for r in _recent[-5:]) or "- (none)"
    fact_rule = f"Include this fact once, in one sentence (rephrase lightly): {fact}" if fact else "Do not state any fact about their business."
    for attempt in range(3):
        identity = (
        f"I'm {SENDER_NAME}, an independent freelance developer. {COMPANY_WHO}"
        if not COMPANY_NAME else
        f"I'm {SENDER_NAME}, a freelance developer working under {COMPANY_NAME}. {COMPANY_WHO}"
    )
    prompt = f"""
Write the BODY of a short cold email from {SENDER_NAME}, a freelance developer, to the owner of "{nm}"
({biz['category']}, {biz['city']}, {biz.get('country', '')}).

The sender is an independent freelancer, NOT a large company or a team. Never describe the sender as "we", a company team, or an agency unless an explicitly configured, truthful company identity says otherwise.
The greeting, call to action and signature are added automatically: do NOT write them.
Write exactly two short paragraphs, 45 to 75 words in total, plain text, no markdown, no emojis, no links. Make it sound like a thoughtful individual who briefly researched the business, not a mass campaign.
Explain naturally that the sender found the business listing on Google Maps, only when this is true of the lead's source.

PARAGRAPH 1: Start with the verified observation only: {fact_rule}
  Do NOT invent a problem, imply the business is losing leads, or ask about missed phone calls unless the selected angle is "after_hours" or "busy".
  For "no_site", frame the idea around making the business easier to discover and contact online.
  For "no_chat" or "no_booking", frame it around making customer enquiries or appointment requests easier.
  For "after_hours" or "busy", you may ask one practical question about handling calls.
  For "general", use a relevant, non-assumptive opening.
  Do not repeat the business name more than once in the whole text (the greeting already uses it).
PARAGRAPH 2: Introduce the sender honestly as a freelance developer using this description: "{identity}"
  Primary service angle for this prospect, based on the measured evidence: {service_pitch(angle)}
  Available services: {SERVICE_OFFERINGS}
  Pick ONE primary service only for this email. For "no_site", lead with website development. For "no_chat" or "no_booking", lead with website/web-app improvements. For "after_hours" or "busy", lead with the AI receptionist. For "general", choose a service only if available evidence supports it; otherwise ask a neutral qualification question. Mention other services only if they naturally fit, not as a laundry list.
  Describe AI receptionist features (such as {a} and {b}) only when the selected angle is "after_hours" or "busy". Do not imply the recipient has a problem that was not measured.
STYLE FOR THIS EMAIL: {random.choice(STYLES)}

RULES:
- Sound like one real person writing to another. No hype, no urgency tricks, no exclamation marks.
- When relevant to the evidence, call it "an AI receptionist" or "AI phone assistant"; do not force an AI receptionist pitch into every email.
- Do not list all services in every email. Sell one relevant service per email; the company website/signature can show the broader service range.
- Keep the primary service tied to the observed business need; do not claim that a business has no website, booking system or chat unless the code actually measured that fact.
- Do NOT invent clients, results, statistics, prices, awards, languages, integrations or guarantees. Never claim we called them or tested their phone.
- Avoid sales clichés such as "I hope you're doing well", "just checking in", "game-changer", "transform your business", "boost growth", "unlock potential", or fake familiarity.
- Never use the words: free, offer, discount, guarantee, urgent, sale, outreach.
- Do not reuse the wording of these recent emails (use different sentence openings and structure):
{avoid}
- Subject: 15 to 45 characters, plain and specific to the service or observation; no fake "Re:", "quick question", "urgent", "!" or ALL CAPS.
- CTA: ask for one small next step (permission to send an idea, a simple yes/no, or the right contact), not a forced meeting/demo.
- Do not claim a custom audit, mockup, proposal, research, or result already exists unless supplied in the input.

Return ONLY JSON: {{"subject": "...", "body": "..."}}
"""
        data = parse_json(gemini_call(prompt, json_mode=True, temperature=0.95))
        if not data:
            continue
        subject, core = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
        body = assemble(biz, core, angle)
        problems = spam_problems(subject, body)
        if re.match(r"\s*(hi|hello|dear)\b", core, re.I):
            problems.append("greeting must not be written by the AI")
        if not 35 <= len(core.split()) <= 95:
            problems.append(f"core length {len(core.split())} words")
        sim = max_similarity(core)
        if sim >= SIM_MAX:
            problems.append(f"too similar to a recent email ({sim:.0%})")
        if subject.lower() in {r["subject"].lower() for r in _recent}:
            problems.append("subject already used")
        if not problems:
            return {"subject": subject, "core": core, "body": body, "source": "gemini"}
        log.warning(f"Rejected generated email (attempt {attempt + 1}): {'; '.join(problems)}")
    return None


def compose_fallback(biz: dict, angle: str, fact: str) -> dict:
    """Template email built from many interchangeable parts. Thousands of different combinations."""
    info = CATEGORIES[biz["category"]]
    nm = display_name(biz["name"]) or "your business"
    fmt = {"name": nm, "customer": info["customer"], "call_for": info["call_for"]}
    hook = random.choice(HOOKS[angle]).format(**fmt)
    p1 = f"{fact} {hook}".strip() if fact else hook
    who = COMPANY_WHO if _env("COMPANY_WHO") else random.choice(WHO_VARIANTS)
    intro = random.choice(INTROS).format(sender=SENDER_NAME, company=COMPANY_NAME, who=who)
    pitch = service_pitch(angle)
    positioning = random.choice(POSITIONING) if angle in ("after_hours", "busy") else ""
    p2 = " ".join(x for x in (intro, pitch, positioning) if x)
    core = f"{p1}\n\n{p2}"
    subject = random.choice(SUBJECTS[angle]).format(name=nm)
    return {"subject": subject[:55], "core": core, "body": assemble(biz, core, angle), "source": "template"}


def fallback_email(biz: dict, angle: str, fact: str) -> dict:
    """Used when Gemini is unavailable or keeps failing. Still unique and spam-checked."""
    best, best_sim = None, 9.0
    used = {r["subject"].lower() for r in _recent}
    for _ in range(12):
        mail = compose_fallback(biz, angle, fact)
        if spam_problems(mail["subject"], mail["body"]):
            continue
        sim = max_similarity(mail["core"]) + (0.5 if mail["subject"].lower() in used else 0.0)
        if sim < best_sim:
            best, best_sim = mail, sim
        if sim < SIM_MAX:
            break
    return best or compose_fallback(biz, angle, fact)


def signature(country: str = "", followup: bool = False) -> str:
    """Honest individual freelancer signature; do not imply an agency if none is configured."""
    lines = ["", "Regards,", SENDER_NAME]
    if COMPANY_NAME:
        lines.append(COMPANY_NAME)
    lines.append("Freelance Web & AI Developer")
    if COMPANY_PHONE:
        lines.append(f"WhatsApp / call: {COMPANY_PHONE}")
    if COMPANY_SITE:
        lines.append(COMPANY_SITE)
    if COMPANY_ADDRESS:
        lines.append(COMPANY_ADDRESS)
    lines += ["", "If this isn't relevant, let me know and I won't follow up."]
    return "\n".join(lines)


def check_sender_auth() -> None:
    """Warn early if the sending domain lacks SPF / DMARC. This matters far more for inbox placement than wording."""
    dom = FROM_EMAIL.split("@")[-1].lower() if "@" in (FROM_EMAIL or "") else ""
    if not dom:
        return
    if dom in FREE_MAIL:
        log.info(f"ℹ️  Sending from a free mailbox ({dom}). Fine for low volume; a dedicated domain with "
                 "SPF/DKIM/DMARC gives better inbox placement.")
        return
    try:
        res = dns.resolver.Resolver()
        res.lifetime = 8

        def txt(name: str) -> list:
            try:
                return [b"".join(r.strings).decode("utf-8", "ignore") for r in res.resolve(name, "TXT")]
            except Exception:  # noqa: BLE001
                return []

        if not any(t.lower().startswith("v=spf1") for t in txt(dom)):
            log.warning(f"⚠️  No SPF record found for {dom}: emails are much more likely to land in spam. Add one.")
        if not any(t.lower().startswith("v=dmarc1") for t in txt("_dmarc." + dom)):
            log.warning(f"⚠️  No DMARC record found for _dmarc.{dom}: Gmail/Yahoo expect one. Add at least p=none.")
        log.info("ℹ️  DKIM cannot be checked automatically: make sure your email provider signs outgoing mail with DKIM.")
        if SMTP_HOST == "smtp.gmail.com" and not SMTP_USER.lower().endswith(("@gmail.com", "@googlemail.com")) \
                and SMTP_USER.split("@")[-1].lower() != dom:
            log.warning("⚠️  FROM_EMAIL domain differs from the Gmail SMTP login: SPF/DKIM will not align.")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Sender authentication check skipped: {e}")


# ─────────────────────────────────────────────
# SENDING
# ─────────────────────────────────────────────
def send_email(to_email: str, subject: str, body: str, in_reply_to: Optional[str] = None) -> Optional[str]:
    msg = MIMEMultipart()
    msg["From"] = formataddr((f"{SENDER_NAME} | {COMPANY_NAME}", FROM_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = EMAIL_USER or FROM_EMAIL
    msg["Date"] = formatdate(localtime=True)
    msg["List-Unsubscribe"] = f"<mailto:{EMAIL_USER or FROM_EMAIL}?subject=unsubscribe>"
    mid = make_msgid(domain=FROM_EMAIL.split("@")[-1])
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        smtp_cls = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with smtp_cls(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_PORT != 465:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        log.info(f"✅ Sent to {to_email}")
        return mid
    except smtplib.SMTPAuthenticationError:
        log.error("❌ SMTP auth failed. Check EMAIL_USER / EMAIL_PASS (Gmail App Password).")
    except smtplib.SMTPRecipientsRefused:
        log.error(f"❌ Recipient refused: {to_email}")
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ SMTP error: {e}")
    return None


# ─────────────────────────────────────────────
# INBOX SYNC (replies, bounces, opt-outs) + FOLLOW-UPS
# ─────────────────────────────────────────────
OPTOUT_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (?:emailing|sending|contacting|writing)|"
    r"do not (?:email|contact|write)|don'?t (?:email|contact|write)|not interested|no,? thanks?|no thank you)\b"
    r"|^\s*no\W*$", re.I | re.M)


def _reply_text(raw: bytes) -> str:
    """The new text of a reply (quoted history removed), first 800 characters."""
    msg = message_from_bytes(raw)
    text = ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                break
    lines = []
    for ln in text.splitlines():
        if ln.startswith(">") or re.match(r"\s*On .+wrote:", ln):
            break
        lines.append(ln)
    return "\n".join(lines)[:800]


def inbox_signals(targets: list) -> Optional[tuple]:
    """targets: [(email, since_datetime)] -> (replied, bounced, optouts) or None if the inbox can't be read."""
    replied, bounced, optouts = set(), set(), set()
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        m.login(EMAIL_USER, EMAIL_PASS)
        m.select("INBOX", readonly=True)
        for email, since in targets:
            s = since.strftime("%d-%b-%Y")
            dom = email.split("@")[-1]
            who = email if dom in FREE_MAIL else "@" + dom
            typ, data = m.search(None, f'(FROM "{who}" SINCE {s})')
            if typ == "OK" and data and data[0] and data[0].split():
                replied.add(email)
                try:
                    typ2, msgdata = m.fetch(data[0].split()[-1], "(BODY.PEEK[])")
                    if typ2 == "OK" and msgdata and isinstance(msgdata[0], tuple) \
                            and OPTOUT_RE.search(_reply_text(msgdata[0][1])):
                        optouts.add(email)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Could not read reply body from {email}: {e}")
            typ, data = m.search(None, f'(OR FROM "mailer-daemon" FROM "postmaster" SINCE {s} TEXT "{email}")')
            if typ == "OK" and data and data[0] and data[0].split():
                bounced.add(email)
        m.logout()
        return replied, bounced, optouts
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not read inbox via IMAP ({e})")
        return None


BASE_KEYS = ("business", "city", "website", "domain", "email", "main_issue", "country", "category", "score")


def sync_inbox() -> Optional[dict]:
    """Record replies, bounces and opt-outs for recent sends. Returns stats, or None if the inbox could not be read."""
    rows, now = load_rows(), datetime.now(timezone.utc)
    by_domain = {}
    for r in rows:
        by_domain.setdefault(r.get("domain", ""), []).append(r)

    pending, recent, bounced_n, replied_n = [], 0, 0, 0
    for dom, rs in by_domain.items():
        first = next((r for r in rs if r.get("status") == "sent"), None)
        sent_at = parse_dt(first["date"]) if first else None
        if not first or not sent_at or (now - sent_at).days > FOLLOWUP_MAX_AGE_DAYS:
            continue
        recent += 1
        statuses = {r.get("status") for r in rs}
        if "bounced" in statuses:
            bounced_n += 1
        elif statuses & {"replied", "optout"}:
            replied_n += 1
        else:
            pending.append((first, sent_at, (first.get("email") or "").lower()))

    if pending:
        signals = inbox_signals([(e, t) for _, t, e in pending if e])
        if signals is None:
            return None
        replied, bounced, optouts = signals
        for first, _, email in pending:
            base = {k: first.get(k, "") for k in BASE_KEYS}
            if email in bounced:
                record({**base, "status": "bounced"})
                bounced_n += 1
            elif email in optouts:
                log.info(f"🚫 OPT-OUT from {email} ({first.get('business')}): added to suppression list")
                record({**base, "status": "optout"})
                add_suppression(email)
                replied_n += 1
            elif email in replied:
                log.info(f"💬 REPLY from {email} ({first.get('business')}), answer it!")
                record({**base, "status": "replied"})
                replied_n += 1

    rate = bounced_n / recent if recent else 0.0
    stats = {"recent_sent": recent, "bounced": bounced_n, "replied": replied_n, "bounce_rate": rate,
             "paused": (not FORCE_RESUME and recent >= BOUNCE_MIN_SAMPLE and bounced_n >= BOUNCE_MIN_COUNT
                        and rate > BOUNCE_PAUSE_RATE)}
    log.info(f"📊 Last {FOLLOWUP_MAX_AGE_DAYS} days: sent={recent} replied={replied_n} bounced={bounced_n} (rate {rate:.0%})")
    return stats


def followup_body(row: dict) -> str:
    """One concise, relevant follow-up; never switch a website lead into an AI receptionist pitch."""
    angle = row.get("main_issue", "general")
    nm = display_name(row.get("business") or "")
    if angle in ("no_site", "no_chat", "no_booking", "general"):
        messages = {
            "no_site": "I reached out because a clear website can help people understand your services and contact you. I can send two ideas tailored to your business if useful.",
            "no_chat": "I was thinking about how customers get from your website to making an enquiry. I can share one or two practical improvements if useful.",
            "no_booking": "I reached out about making online enquiries or appointment requests simpler. Would an example of that flow be useful?",
            "general": "I build websites, web apps and mobile apps for businesses. If improving any of those is on your roadmap, I can send a relevant example."
        }
        return f"{greeting(nm)}\n\n{messages.get(angle, messages['general'])}\n\n{signature(row.get('country', ''))}"
    info = CATEGORIES.get(row.get("category", ""))
    does = info["does"] if info else "answers common questions"
    who = info["customer"] if info else "customer"
    call_for = info["call_for"] if info else "book an appointment"
    ask = (f"You can hear it in a minute: call {DEMO_PHONE} and talk to it like a {who} who wants to {call_for}."
           if DEMO_PHONE else "Would a short demo be useful?")
    return (
        f"{greeting(nm)}\n\n"
        "Following up on my earlier note about handling calls when your team is busy. "
        f"The AI call agent I develop can {does}.\n\n{ask}\n\n{signature(row.get('country', ''))}"
    )


def run_followups(suppressed: set, limit: int) -> int:
    """One follow-up per silent lead. Call only after a successful sync_inbox()."""
    if limit <= 0:
        return 0
    by_domain, now, due = {}, datetime.now(timezone.utc), []
    for r in load_rows():
        by_domain.setdefault(r.get("domain", ""), []).append(r)
    for dom, rs in by_domain.items():
        statuses = {r.get("status") for r in rs}
        if "sent" not in statuses or statuses & {"followup_sent", "replied", "bounced", "optout"}:
            continue
        first = next(r for r in rs if r.get("status") == "sent")
        if first.get("main_issue") not in HOOKS:      # sent by an older version of this bot (different pitch)
            continue
        sent_at, email = parse_dt(first.get("date")), (first.get("email") or "").lower()
        if sent_at and email and email not in suppressed and dom not in suppressed \
                and email.split("@")[-1] not in suppressed \
                and FOLLOWUP_AFTER_DAYS <= (now - sent_at).days <= FOLLOWUP_MAX_AGE_DAYS:
            due.append((first, email))

    sent = 0
    for first, email in due:
        if sent >= limit or time.time() - START_TS > MAX_RUNTIME_S:
            break
        base = {k: first.get(k, "") for k in BASE_KEYS}
        subject = "Re: " + (first.get("subject") or "your phone calls")
        body = followup_body(first) + "\n" + signature(first.get("country", ""), followup=True)
        print(f"\n--- FOLLOW-UP to {email} ---\n{body}")
        mid = send_email(email, subject, body, in_reply_to=first.get("message_id") or None)
        if mid:
            record({**base, "subject": subject, "status": "followup_sent", "message_id": mid})
            sent += 1
            time.sleep(random.randint(SLEEP_MIN, SLEEP_MAX))
    return sent


# ─────────────────────────────────────────────
# ONE LEAD
# ─────────────────────────────────────────────
_seen_emails: set = set()


def process_lead(biz: dict, suppressed: set) -> bool:
    """True if an email was sent (or drafted in dry-run). Handles businesses with AND without a website."""
    host, key = biz["host"], biz["key"]
    base = {"business": biz["name"], "city": biz["city"], "website": f"https://{host}" if host else "", "domain": key,
            "country": biz.get("country", ""), "category": biz["category"]}

    r, site = None, "none"
    if host:
        if host in suppressed or not is_public_host(host):
            log.info(f"⏭️  Skipping {host} (suppressed or non-public)")
            return False
        r = fetch_site(host)
        site = "ok" if r is not None else "unknown"
        if r is None:
            log.info(f"ℹ️  {host}: website not reachable from our server (scoring without website data)")
    sig = site_signals(r) if r is not None else {}

    phone = (site_phone(r.text) if r is not None else "") or biz.get("phone") or ""
    hrs = parse_hours(biz.get("hours"))
    score, angle, fact = score_lead(biz, site, sig, hrs)
    if angle in ("after_hours", "busy") and not phone:
        log.info(f"⏭️  {key}: no phone number for a phone-agent pitch")
        record({**base, "main_issue": angle, "score": score, "status": "no_phone"})
        return False
    if score < SCORE_MIN:
        log.info(f"⏭️  {key}: fit score {score} < {SCORE_MIN}, they probably don't need a call agent")
        record({**base, "main_issue": angle, "score": score, "status": "low_fit"})
        return False
    log.info(f"🎯 {key}: fit score {score}, angle = {angle}")

    email = pick_api_email(biz.get("api_emails"), biz["city"], host)
    if not email and r is not None:
        email = find_public_email(host, r)
    add_call_lead(biz, base["website"] or key, phone, email, angle, score, fact)      # even when we have no email

    if not email:
        log.info(f"⏭️  {key}: no usable email (call list entry saved)")
        record({**base, "main_issue": angle, "score": score, "status": "no_email"})
        return False
    dom = email.split("@")[-1]
    if email in suppressed or dom in suppressed:
        return False
    if biz.get("country") in STRICT_COUNTRIES and dom in FREE_MAIL:
        log.info(f"⏭️  {key}: private address ({email}) in {biz.get('country')}, not allowed without consent")
        record({**base, "email": email, "main_issue": angle, "score": score, "status": "no_email"})
        return False
    if email in _seen_emails:
        record({**base, "email": email, "main_issue": angle, "score": score, "status": "dup_email"})
        return False

    ok, reason = verify_email(email)
    if not ok:
        log.info(f"⏭️  {key}: {email} failed verification ({reason})")
        record({**base, "email": email, "main_issue": angle, "score": score, "status": "bad_email"})
        return False

    mail = write_email(biz, angle, fact) or fallback_email(biz, angle, fact)
    body = mail["body"] + "\n" + signature(biz.get("country", ""))

    print("\n" + "=" * 60)
    print(f"🏢 {biz['name']} ({biz['city']}, {biz.get('country', '')})  🌐 {base['website'] or 'no website'}  [{biz.get('source', '')}]")
    print(f"📧 {email}   📌 {mail['subject']}   🎯 score {score} / {angle}   ✍️ {mail.get('source', '')}")
    print(f"🔬 Fact used: {fact}")
    print(body)
    print("=" * 60)

    if DRY_RUN:
        remember_email(mail["subject"], mail.get("core") or mail["body"])
        record({**base, "email": email, "subject": mail["subject"], "main_issue": angle, "score": score,
                "status": "dry_run"})
        return True

    mid = send_email(email, mail["subject"], body)
    if mid:
        _seen_emails.add(email)
        remember_email(mail["subject"], mail.get("core") or mail["body"])
        record({**base, "email": email, "subject": mail["subject"], "main_issue": angle, "score": score,
                "status": "sent", "message_id": mid})
        return True
    record({**base, "email": email, "subject": mail["subject"], "main_issue": angle, "score": score,
            "status": "send_failed"})
    return False


# ─────────────────────────────────────────────
# RUN SUMMARY (shown at the top of every GitHub Actions run)
# ─────────────────────────────────────────────
def write_summary(done, followups, cap, remaining, stats, n_combos, usage, reason) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## AI call agent outreach: run summary", "", "| | |", "|---|---|",
             f"| Result | **{reason}** |",
             f"| New emails {'drafted' if DRY_RUN else 'sent'} | {done} |",
             f"| Follow-ups sent | {followups} |",
             f"| Daily cap (warm-up aware) | {cap} (slots left at start: {remaining}) |",
             f"| Minimum fit score | {SCORE_MIN} |",
             f"| City/category searches picked | {n_combos} |",
             f"| RapidAPI calls today | {usage.get('calls', 0)} |"]
    if stats:
        lines.append(f"| Bounce rate (14 days) | {stats['bounced']}/{stats['recent_sent']} = {stats['bounce_rate']:.0%} "
                     f"(pauses above {BOUNCE_PAUSE_RATE:.0%} once >= {BOUNCE_MIN_SAMPLE} sends and >= {BOUNCE_MIN_COUNT} bounces) |")
        lines.append(f"| Replies (14 days) | {stats['replied']} |")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main() -> None:
    if not DRY_RUN and not (EMAIL_USER and EMAIL_PASS):
        raise SystemExit("Missing EMAIL_USER / EMAIL_PASS (set them as environment variables / GitHub Secrets)")

    log.info(f"🚀 Freelance Web & AI Outreach | dry_run={DRY_RUN} | daily_limit={DAILY_LIMIT} | "
             f"min_score={SCORE_MIN} | hours_only={BUSINESS_HOURS_ONLY}")
    if not RAPIDAPI_KEY:
        log.warning("RAPIDAPI_KEY not set: using free OpenStreetMap discovery only")
    if not COMPANY_ADDRESS:
        log.warning("COMPANY_ADDRESS not set: United States leads are skipped (US law requires a postal address)")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set: using template emails")
    if not DEMO_PHONE:
        log.warning("DEMO_PHONE not set: emails will ask for a reply instead of letting owners call and hear the agent "
                    "(much lower reply rate). Set it to a live demo number.")
    if not PROOF_LINE:
        log.warning("PROOF_LINE not set: emails carry no proof (client / pilot / result). Add one TRUE sentence.")

    ensure_log()
    ensure_call_log()
    _recent.extend(load_recent())
    check_sender_auth()
    rows = load_rows()
    seen, suppressed = load_seen(rows), load_suppressed()
    _seen_emails.update(load_seen_emails(rows))
    usage = load_usage()
    done, followups, paused, stats, combos = 0, 0, False, None, []
    eff_limit = DAILY_LIMIT if DRY_RUN else effective_daily_limit(rows)
    log.info(f"Today's sending cap: {eff_limit} (warm-up {'on' if WARMUP else 'off'}, max {DAILY_LIMIT})")

    if not DRY_RUN:
        stats = sync_inbox()
        if stats is None:
            log.warning("Inbox unreadable: skipping follow-ups this run (never follow up blindly)")
        else:
            paused = stats["paused"]
            if paused:
                log.error(f"🛑 PAUSED: bounce rate {stats['bounce_rate']:.0%} ({stats['bounced']}/{stats['recent_sent']}) is above "
                          f"{BOUNCE_PAUSE_RATE:.0%}. No new emails until it recovers. Override: FORCE_RESUME=true")
            suppressed = load_suppressed()            # opt-outs found during the sync apply immediately
            followups = run_followups(suppressed, min(FOLLOWUP_LIMIT, eff_limit) - count_today(rows, "followup_sent"))

    remaining = eff_limit - (0 if DRY_RUN else count_today(rows, "sent"))
    if paused or remaining <= 0:
        log.info("Nothing to send today (paused or daily limit reached).")
    else:
        combos = pick_combos(MAX_COMBOS_PER_RUN)
        if not combos:
            log.warning("No city is in business hours right now (set BUSINESS_HOURS_ONLY=false to override).")
        for city, category in combos:
            if done >= remaining or time.time() - START_TS > MAX_RUNTIME_S:
                break
            leads = discover_any(city, category, usage)
            save_usage(usage)
            for biz in leads:
                if done >= remaining or time.time() - START_TS > MAX_RUNTIME_S:
                    break
                if biz["key"] in seen:
                    continue
                seen.add(biz["key"])
                if not local_ok(biz.get("tz")):
                    continue                          # outside that business's working hours: skip quietly
                try:
                    if process_lead(biz, suppressed):
                        done += 1
                        if not DRY_RUN and done < remaining:
                            time.sleep(random.randint(SLEEP_MIN, SLEEP_MAX))
                except Exception as e:  # noqa: BLE001
                    log.exception(f"Unexpected error on {biz.get('key')}: {e}")

    log.info(f"✅ Finished. {'Drafted' if DRY_RUN else 'Sent'} {done} new + {followups} follow-up(s). "
             f"API calls today: {usage['calls']}. Call list: {CALL_CSV}")
    reason = ("PAUSED by bounce-rate guard" if paused else "daily cap reached" if remaining <= 0
              else "no city in business hours right now" if not combos else "ok")
    write_summary(done, followups, eff_limit, remaining, stats, len(combos), usage, reason)


if __name__ == "__main__":
    main()
