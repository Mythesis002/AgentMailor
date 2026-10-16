"""
Himalaya IT Outreach Engine v4  (businesses worldwide, Google Maps data via RapidAPI)

Per run:
  0. Sync inbox: mark replies / bounces, pause sending if the bounce rate is too high
  1. Follow up (once) with leads that stayed silent for 3+ days
  2. RapidAPI 'Local Business Data' (Google Maps) finds real businesses, only in cities where it is business hours NOW
     (free OpenStreetMap is the automatic fallback if the API key/quota is not available)
  3. We audit each website ourselves (real, measured problems only; no guessing)
  4. Worth-it filter: only email when the problem is serious enough to matter to the owner
  5. Public email from the site (or OSM) + MX check
  6. Gemini (free tier) writes the email; a template is used if Gemini is unavailable
  7. Send, log to sent_log.csv; every serious lead with a phone number goes to call_list.csv

Secrets come ONLY from environment variables (GitHub Actions secrets).
Free report for a lead who replies "yes":   python mailor.py --report their-domain.com
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
# ── LOCAL TESTING ONLY ───────────────────────────────────────────────────────────
# Paste keys here to test on your own computer. Environment variables / GitHub Secrets win over this block.
# REMOVE these values before you upload this file to GitHub (the script warns you if it finds them there).
LOCAL_TEST = {
    "RAPIDAPI_KEY": "628e474f5amsh0457d8f1b4fb50cp16b75cjsn70408f276e9b",
    "GEMINI_API_KEY": "AIzaSyDL6mu7ePmgpPgz-Lgp-uS9VMj4eitHbM4",
    "EMAIL_USER": "ashishsinghbag02@gmail.com",
    "EMAIL_PASS": "mamppvuvxekabqkx",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or LOCAL_TEST.get(name) or default


RAPIDAPI_KEY = _env("RAPIDAPI_KEY")
RAPID_MAX_CALLS_PER_DAY = int(_env("RAPID_MAX_CALLS_PER_DAY", "6"))   # protect your free plan quota
RAPID_LIMIT = int(_env("RAPID_LIMIT", "30"))                          # businesses per API call
GEMINI_API_KEY = _env("GEMINI_API_KEY")                 # optional: template email is used without it
EMAIL_USER = _env("EMAIL_USER")                         # inbox that receives replies (IMAP)
EMAIL_PASS = _env("EMAIL_PASS")
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")         # change later to a proper sending service
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
BOUNCE_PAUSE_RATE = float(_env("BOUNCE_PAUSE_RATE", "0.10"))
MAX_RUNTIME_S = int(_env("MAX_RUNTIME_MIN", "40")) * 60  # stop early so the log always gets saved
SLEEP_MIN, SLEEP_MAX = 45, 90

SENDER_NAME = _env("SENDER_NAME", "Ashish")
COMPANY_NAME = _env("COMPANY_NAME", "Himalaya IT")
COMPANY_PHONE = _env("COMPANY_PHONE", "+916302449432")
COMPANY_SITE = _env("COMPANY_WEBSITE", "opentry.in")
COMPANY_ADDRESS = _env("COMPANY_ADDRESS", "")           # REQUIRED to email US businesses (CAN-SPAM)
DELIVERY_DAYS = _env("DELIVERY_DAYS", "5-7 working days")
COMPANY_PITCH = _env(
    "COMPANY_PITCH",
    "Himalaya IT is a 25-member team based in Greater Noida. We fix and rebuild business "
    "websites: speed, mobile layout, SEO basics, and lead capture (WhatsApp, call and enquiry buttons).",
)

MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")        # change here if Google renames/retires it
LOG_CSV = "sent_log.csv"
CALL_CSV = "call_list.csv"
SUPPRESS_FILE = "suppression.txt"
LOG_FIELDS = ["date", "business", "city", "website", "domain", "email", "subject", "main_issue", "status", "message_id"]
CALL_FIELDS = ["date", "business", "category", "city", "country", "website", "phone", "email", "instagram", "facebook",
               "rating", "reviews", "main_issue", "what_to_say"]
BLOCKING_STATUSES = {"sent", "no_email", "no_issue", "weak", "bad_email", "dup_email"}
START_TS = time.time()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HimalayaITAudit/3.0)",
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
SEARCH_RADIUS_M = int(_env("SEARCH_RADIUS_M", "12000"))
INCLUDE_CHAINS = _env("INCLUDE_CHAINS", "false").lower() == "true"

CATEGORIES = {
    "dental clinics": [("amenity", "dentist")],
    "clinics and hospitals": [("amenity", "clinic"), ("amenity", "hospital"), ("amenity", "doctors")],
    "diagnostic labs": [("healthcare", "laboratory")],
    "restaurants and cafes": [("amenity", "restaurant"), ("amenity", "cafe")],
    "hotels and guest houses": [("tourism", "hotel"), ("tourism", "guest_house")],
    "gyms and fitness studios": [("leisure", "fitness_centre")],
    "salons and spas": [("shop", "hairdresser"), ("shop", "beauty")],
    "real estate agencies": [("office", "estate_agent")],
    "furniture and interior shops": [("shop", "furniture"), ("shop", "interior_decoration")],
    "lawyers and accountants": [("office", "lawyer"), ("office", "accountant")],
    "schools, colleges and coaching": [("amenity", "school"), ("amenity", "college"), ("amenity", "language_school")],
    "travel agencies": [("shop", "travel_agency")],
    "car dealers and garages": [("shop", "car"), ("shop", "car_repair")],
    "event and wedding venues": [("amenity", "events_venue")],
}

ISSUE_PHRASES = {
    "ssl_invalid": "the site's security certificate is expired or invalid, so visitors see a 'Not secure' warning",
    "no_https": "the site has no HTTPS, so browsers label it 'Not secure'",
    "mixed_content": "some parts of the site load insecurely, which can trigger browser warnings",
    "no_viewport": "the site isn't set up for mobile screens, so it looks shrunken on phones",
    "slow": "the homepage loads slowly, and slow sites lose visitors before they ever enquire",
    "heavy_page": "the homepage is very heavy, which slows it down on mobile data",
    "heavy_images": "lots of images load upfront, which slows the site on phones",
    "no_alt": "most images have no alt text, which hurts Google Images and accessibility",
    "no_lead_capture": "there's no call, WhatsApp, contact or enquiry option on the homepage, so interested visitors can't easily reach you",
    "no_quick_contact": "there's no click-to-call or WhatsApp button, which most mobile visitors expect",
    "no_title": "the homepage has no page title, so Google has nothing to show in search",
    "short_title": "the page title is too generic to rank for local searches",
    "no_meta_desc": "there's no meta description, so Google picks random text for your listing",
    "no_h1": "the homepage has no main heading, which weakens search ranking",
    "stale_year": "the footer year is out of date, which makes the business look inactive",
    "no_website": "your Google Maps listing has no website of its own, so people who find you can't see your services or contact you easily",
}
FIXES = {
    "ssl_invalid": "Renew/replace the SSL certificate and force HTTPS (free via Let's Encrypt or Cloudflare).",
    "no_https": "Install an SSL certificate and redirect all HTTP traffic to HTTPS.",
    "mixed_content": "Update every http:// resource link to https:// so no browser warnings appear.",
    "no_viewport": "Add a mobile viewport tag and make the layout responsive for phones.",
    "slow": "Enable caching and compression, use a CDN, and trim heavy scripts and images.",
    "heavy_page": "Reduce page weight: compress assets and remove unused code.",
    "heavy_images": "Compress images, use modern formats (WebP) and lazy-load below-the-fold images.",
    "no_alt": "Add descriptive alt text to images for Google Images and accessibility.",
    "no_lead_capture": "Add a click-to-call button, WhatsApp chat button and a short enquiry/booking form on the homepage.",
    "no_quick_contact": "Add a sticky click-to-call and WhatsApp button for mobile visitors.",
    "no_title": "Write a clear page title with the service and city, e.g. 'Dentist in <City> | <Name>'.",
    "short_title": "Expand the page title to include the service and location.",
    "no_meta_desc": "Write a 150-character meta description that sells the click.",
    "no_h1": "Add one clear main heading (H1) describing what you do and where.",
    "stale_year": "Update the footer year and refresh dated content so the site looks active.",
    "no_website": "Build a fast, mobile-friendly website with call/WhatsApp buttons and link it from the Google listing.",
}

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


def gemini_call(prompt: str, json_mode: bool = False, retries: int = 2) -> str:
    """Plain Gemini text call. After 2 failed calls it is switched off for the run (template email is used)."""
    global _gemini_failures
    if not GEMINI_API_KEY or _gemini_failures >= 2:
        return ""
    cfg = {"temperature": 0.6}
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
def ensure_log() -> None:
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


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
    """Domains we never process again. 'unreachable' only blocks after 2 failures (could be a blip)."""
    seen, unreachable = set(), {}
    for r in rows:
        d, st = r.get("domain", ""), r.get("status")
        if st in BLOCKING_STATUSES:
            seen.add(d)
        elif st == "unreachable":
            unreachable[d] = unreachable.get(d, 0) + 1
    return seen | {d for d, n in unreachable.items() if n >= 2}


def load_seen_emails(rows: list) -> set:
    return {(r.get("email") or "").lower() for r in rows if r.get("status") in ("sent", "followup_sent") and r.get("email")}


def load_suppressed() -> set:
    if not os.path.exists(SUPPRESS_FILE):
        return set()
    with open(SUPPRESS_FILE, encoding="utf-8") as f:
        return {ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")}


def count_today(rows: list, status: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for r in rows if r.get("status") == status and (r.get("date") or "").startswith(today))


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


def add_call_lead(biz: dict, website: str, phone: str, email: str, issues: list) -> None:
    """Serious problem + phone number = a lead you can call / WhatsApp / DM yourself (converts better than cold email)."""
    if not phone or website in _call_seen:
        return
    _call_seen.add(website)
    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "business": biz["name"], "category": biz["category"],
        "city": biz["city"], "country": biz.get("country", ""), "website": website, "phone": phone,
        "email": email or "", "instagram": biz.get("instagram") or "", "facebook": biz.get("facebook") or "",
        "rating": biz.get("rating") or "", "reviews": biz.get("reviews") or "",
        "main_issue": issues[0]["key"], "what_to_say": ISSUE_PHRASES.get(issues[0]["key"], ""),
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
    parts = "".join(
        f'nwr["{k}"="{v}"][~"^(contact:)?website$"~"."](around:{SEARCH_RADIUS_M},{lat},{lon});' for k, v in tags
    )
    return f"[out:json][timeout:90];({parts});out tags 200;"


def discover(city: tuple, category: str) -> list:
    name_, country, lat, lon = city
    log.info(f"🔎 Discovering (OpenStreetMap): {category} in {name_}, {country}")
    time.sleep(2)                                   # be polite to the free public servers
    query, data = overpass_query(lat, lon, CATEGORIES[category]), None
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
    out, seen_hosts = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        site = tags.get("website") or tags.get("contact:website")
        biz_name = (tags.get("name") or "").strip()
        if not site or not biz_name:
            continue
        if not INCLUDE_CHAINS and (tags.get("brand") or tags.get("brand:wikidata") or tags.get("operator:wikidata")):
            continue
        h = host_of(str(site))
        if not h or "." not in h or h in seen_hosts or any(d in h for d in DIRECTORY_DOMAINS):
            continue
        seen_hosts.add(h)
        out.append({
            "name": biz_name, "host": h, "city": name_, "country": country, "category": category,
            "key": h, "tz": None, "place_id": "", "rating": None, "reviews": None, "instagram": "", "facebook": "", "source": "osm",
            "api_emails": [e for e in [clean_osm_email(tags.get("email") or tags.get("contact:email"))] if e],
            "phone": clean_phone(tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile") or tags.get("mobile")),
        })
        if len(out) >= LEADS_PER_SEARCH:
            break
    log.info(f"   → {len(out)} candidate websites")
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
# Clean, natural Google-Maps search phrases (a messy query like "Hotels in San Francisco, viatnam" returns junk)
API_QUERY = {
    "dental clinics": ["dental clinic", "dentist"],
    "clinics and hospitals": ["clinic", "skin clinic", "physiotherapy clinic"],
    "diagnostic labs": ["diagnostic lab", "pathology lab"],
    "restaurants and cafes": ["restaurant", "cafe"],
    "hotels and guest houses": ["hotel", "guest house"],
    "gyms and fitness studios": ["gym", "yoga studio"],
    "salons and spas": ["beauty salon", "spa"],
    "real estate agencies": ["real estate agency"],
    "furniture and interior shops": ["interior designer", "furniture store"],
    "lawyers and accountants": ["lawyer", "accountant"],
    "schools, colleges and coaching": ["coaching institute", "tutoring center", "private school"],
    "travel agencies": ["travel agency"],
    "car dealers and garages": ["car repair", "car dealer"],
    "event and wedding venues": ["wedding venue", "banquet hall"],
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
    phrase = random.Random(f"{date.today()}-{name_}-{category}").choice(API_QUERY[category])
    okey = f"{name_}|{phrase}"
    offset = int(usage["offsets"].get(okey, 0))
    query = f"{phrase} in {name_}, {country}"
    log.info(f"🔎 Discovering (Google Maps via RapidAPI): '{query}' offset={offset}")
    data = rapid_search(query, lat, lon, COUNTRY_CODE.get(country, "us"), RAPID_LIMIT, offset)
    usage["calls"] += 1
    if data is None:
        return None
    usage["offsets"][okey] = offset + RAPID_LIMIT if len(data) >= RAPID_LIMIT else 0   # next page, or start over

    out, skipped = [], {"closed": 0, "big_or_chain": 0, "nothing_to_contact": 0, "duplicate": 0}
    seen_keys = set()
    for it in data:
        if not isinstance(it, dict) or not (it.get("name") or "").strip():
            continue
        if (it.get("business_status") or "OPEN") != "OPEN":
            skipped["closed"] += 1                    # e.g. CLOSED_PERMANENTLY (your sample had one)
            continue
        ec = it.get("emails_and_contacts") or {}
        emails = [e for e in (ec.get("emails") or []) if isinstance(e, str)]
        phones = ec.get("phone_numbers") or []
        reviews = int(it.get("review_count") or 0)
        # Many different emails/phones = a multi-location brand (like your sample result); 3000+ reviews = large business
        if not INCLUDE_CHAINS and (reviews > 3000 or len(set(emails)) >= 4 or len(set(phones)) >= 4):
            skipped["big_or_chain"] += 1
            continue
        site = it.get("website") or ""
        host = host_of(site) if site else ""
        if host and ("." not in host or any(d in host for d in DIRECTORY_DOMAINS)):
            host = ""                                 # a Facebook / listing page is not their own website
        phone = clean_phone(it.get("phone_number"))
        if not host and not phone:
            skipped["nothing_to_contact"] += 1
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
# WEBSITE AUDIT (measured facts only)
# ─────────────────────────────────────────────
def fetch_site(host: str):
    """Returns (response, seconds, notes). notes: ssl_problem in {None,'real','unsure'}, https_timeout."""
    notes = {"ssl_problem": None, "https_timeout": False}
    for scheme in ("https", "http"):
        for verify in ((True, False) if scheme == "https" else (True,)):
            try:
                url = f"{scheme}://{host}/"
                t0 = time.time()
                r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True, verify=verify)
                elapsed = time.time() - t0
                if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "").lower():
                    break
                if elapsed > 3:                       # re-measure: first request may include cold start
                    t1 = time.time()
                    try:
                        requests.get(url, headers=HEADERS, timeout=15, verify=verify)
                        elapsed = min(elapsed, time.time() - t1)
                    except requests.RequestException:
                        pass
                return r, elapsed, notes
            except requests.exceptions.SSLError as e:
                if verify:
                    msg = str(e).lower()
                    # Only an expired / mismatched / self-signed cert is a REAL problem. "unable to get local
                    # issuer" is often our runner's CA store, so we make no claim about it.
                    real = any(k in msg for k in ("expired", "hostname", "doesn't match", "self-signed", "self signed"))
                    notes["ssl_problem"] = "real" if real else (notes["ssl_problem"] or "unsure")
                    continue
                break
            except requests.Timeout:
                if scheme == "https":
                    notes["https_timeout"] = True
                break
            except requests.RequestException:
                break
    return None, None, notes


def audit_site(r, elapsed: float, notes: dict) -> list:
    issues = []

    def add(sev: int, key: str, detail: str):
        issues.append({"severity": sev, "key": key, "detail": detail})

    soup = BeautifulSoup(r.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    final_url = r.url
    js_shell = len(page_text) < 300      # page is built by JavaScript: content checks would be guesses

    if notes.get("ssl_problem") == "real":
        add(3, "ssl_invalid", "The HTTPS certificate is expired, mismatched or self-signed, so browsers show a 'Not secure' warning to visitors")
    elif not final_url.startswith("https://") and not notes.get("https_timeout") and not notes.get("ssl_problem"):
        add(3, "no_https", "The site loads over plain HTTP (no HTTPS), so browsers mark it 'Not secure'")
    elif final_url.startswith("https://"):
        mixed = soup.select("img[src^='http://'], script[src^='http://'], iframe[src^='http://'], link[rel=stylesheet][href^='http://']")
        if mixed:
            add(2, "mixed_content", f"{len(mixed)} resources load over insecure HTTP on an HTTPS page")

    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        add(3, "no_viewport", "No mobile viewport tag, so the site likely displays as a shrunken desktop page on phones")

    if elapsed and elapsed > 6.0:
        add(3, "slow", f"The homepage took {elapsed:.0f}s to load its HTML in our test")
    elif elapsed and elapsed > 4.0:
        add(2, "slow", f"The homepage took {elapsed:.0f}s to load its HTML in our test")
    if len(r.content) > 2_000_000:
        add(2, "heavy_page", f"The homepage HTML alone is {len(r.content)/1e6:.1f} MB")

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    if not title:
        add(2, "no_title", "The homepage has no page title, so Google has nothing to show in search results")
    elif len(title) < 15 and not js_shell:
        add(1, "short_title", f"The page title is only '{title}', too generic to rank for local searches")
    if not soup.find("meta", attrs={"name": re.compile("^description$", re.I)}):
        add(1, "no_meta_desc", "No meta description, so Google picks random text for your search listing")

    if not js_shell:
        imgs = soup.find_all("img")
        eager = [i for i in imgs if (i.get("loading") or "").lower() != "lazy"]
        if len(eager) > 25:
            add(2, "heavy_images", f"{len(eager)} images load upfront without lazy-loading, which slows phones on mobile data")
        if len(imgs) >= 5:
            no_alt = [i for i in imgs if not (i.get("alt") or "").strip()]
            if len(no_alt) / len(imgs) > 0.5:
                add(1, "no_alt", f"{len(no_alt)} of {len(imgs)} images have no alt text")

        hrefs = [(a.get("href") or "").lower() for a in soup.find_all("a")]
        has_tel = any(h.startswith("tel:") for h in hrefs)
        has_wa = any("wa.me" in h or "whatsapp" in h for h in hrefs)
        has_form = soup.find("form") is not None
        has_contact_link = any(k in h for h in hrefs for k in ("contact", "enquir", "inquir", "booking", "appointment"))
        if not (has_tel or has_wa or has_form):
            if has_contact_link:
                add(2, "no_quick_contact", "No click-to-call or WhatsApp button, which most mobile visitors expect")
            else:
                add(3, "no_lead_capture", "The homepage has no click-to-call, WhatsApp, contact link or enquiry form, so interested visitors have no easy way to reach out")
        elif not (has_tel or has_wa):
            add(2, "no_quick_contact", "No click-to-call or WhatsApp button, which most mobile visitors expect")

        if not soup.find("h1"):
            add(1, "no_h1", "No main heading (H1) on the homepage")
        years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:20\d{2}\s*[-–]\s*)?(20\d{2})", page_text, re.I)]
        if years and max(years) < date.today().year - 1:
            add(2, "stale_year", f"The footer still says © {max(years)}, which makes the business look inactive")

    issues.sort(key=lambda x: -x["severity"])
    return issues


def worth_emailing(issues: list) -> bool:
    """Only email when the problem matters to the owner: one serious issue, or several medium ones."""
    if not issues:
        return False
    return issues[0]["severity"] >= 3 or sum(i["severity"] for i in issues if i["severity"] >= 2) >= 4


# ─────────────────────────────────────────────
# EMAIL + PHONE DISCOVERY / VERIFICATION
# ─────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.uk", "yahoo.com.au", "outlook.com",
             "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
             "proton.me", "protonmail.com", "rediffmail.com", "zoho.com", "mail.com"}
JUNK_SUBSTR = ("sentry", "wixpress", "example", "yourdomain", "domain.com", "email.com", "noreply",
               "no-reply", "donotreply", "u003e", "@2x", "godaddy", "privacy@", "abuse@")
JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff")
PREFERRED_PREFIX = ("info", "contact", "hello", "enquiry", "inquiry", "sales", "care", "support", "admin", "office")


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
        if not len(resolver.resolve(email.split("@")[-1], "MX")):
            return False, "no MX"
        return True, "MX ok"
    except Exception as e:  # noqa: BLE001
        return False, f"MX lookup failed: {e}"


# ─────────────────────────────────────────────
# EMAIL WRITING
# ─────────────────────────────────────────────
BANNED = re.compile(r"guarantee|100%|best price|cheapest|act now|limited time|[$₹€£]\s?\d", re.I)


def write_email(biz: dict, issues: list, website: str) -> Optional[dict]:
    no_site = issues[0]["key"] == "no_website"
    findings = "\n".join(f"{i+1}. {x['detail']}" for i, x in enumerate(issues[:3]))
    facts = ""
    if biz.get("rating") and (biz.get("reviews") or 0) >= 10:
        facts = (f"\nFACT FROM GOOGLE MAPS (optional, mention at most once, numbers exactly as given): "
                 f"rated {biz['rating']} from {biz['reviews']} reviews.\n")
    if no_site:
        opening = ("- Sentence 1: say we found their business on Google Maps but it lists no website of its own, and what "
                   "that costs them (people who search cannot see services or contact options; competitors with a site win the enquiry).")
        target = "their Google Maps listing (no website listed)"
    else:
        opening = ("- Sentence 1: say we looked at their website and name the MAIN problem (finding #1) in plain words, "
                   "and what it costs them (lost enquiries, lower Google ranking, visitors leaving).")
        target = f"website: {website}"
    prompt = f"""
Write a short cold email from {SENDER_NAME} at {COMPANY_NAME} to the owner of "{biz['name']}"
({biz['category']}, {biz['city']}, {biz.get('country', '')}, {target}).

ABOUT US: {COMPANY_PITCH}
DELIVERY TIME: {DELIVERY_DAYS}

WHAT WE FOUND (the ONLY facts you may state about their business):
{findings}
{facts}
RULES:
- Plain text, 90 to 130 words, no markdown, no emojis.
- Start with "Hi," (we do not know the owner's name). Do not write a signature or sign-off; it is added automatically.
{opening}
- Optionally mention finding #2 in one short clause. Do not mention anything not in the list.
- Then one sentence on who we are, one on what we will do, and say we can do it in {DELIVERY_DAYS}.
- End with a soft call to action: reply "yes" and we will send a short free one-page {"plan for their site" if no_site else "report of the fixes"}.
- Do NOT invent clients, results, statistics, prices, awards or guarantees. No hype, no urgency tricks.
- Subject: under 60 characters, specific to their business, not clickbait, no ALL CAPS, no "!".

Return ONLY JSON: {{"subject": "...", "body": "..."}}
"""
    for _ in range(2):
        data = parse_json(gemini_call(prompt, json_mode=True))
        if not data:
            continue
        subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
        words = len(body.split())
        if (subject and 60 <= words <= 170 and not re.search(r"\[[^\]]+\]|\{[^}]+\}", body + subject)
                and not BANNED.search(body + " " + subject)):
            return {"subject": subject[:90], "body": body}
        log.warning(f"Rejected generated email (words={words}); retrying")
    return None


def fallback_email(biz: dict, issues: list, website: str) -> dict:
    main = ISSUE_PHRASES.get(issues[0]["key"], "there are a few fixable issues on the site")
    no_site = issues[0]["key"] == "no_website"
    extra = ""
    if len(issues) > 1 and issues[1]["severity"] >= 2:
        extra = f" I also noticed {ISSUE_PHRASES.get(issues[1]['key'], 'another fixable issue')}."
    intro = (f"I came across {biz['name']} on Google Maps and noticed that {main}." if no_site
             else f"I took a look at {website} and noticed that {main}.{extra}")
    what = ("We build fast, mobile-friendly websites with call and WhatsApp buttons" if no_site
            else "We build modern websites and apps and fix existing ones: speed, mobile layout, SEO and lead capture")
    body = (
        "Hi,\n\n"
        f"{intro} Issues like this can mean lost enquiries and customers choosing a competitor.\n\n"
        f"I'm with {COMPANY_NAME}, a web and software development team. {what}, and we can have this done in {DELIVERY_DAYS}.\n\n"
        f"If you'd like, reply \"yes\" and we'll send a short free one-page {'plan for your site' if no_site else 'report of the fixes'}."
    )
    return {"subject": f"Quick note about {biz['name']}"[:90], "body": body}


def signature() -> str:
    lines = ["", "Regards,", SENDER_NAME, COMPANY_NAME]
    for extra in (COMPANY_PHONE, COMPANY_SITE, COMPANY_ADDRESS):
        if extra:
            lines.append(extra)
    lines += ["", "P.S. If this isn't relevant, just reply \"no\" and I won't email you again."]
    return "\n".join(lines)


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
    mid = make_msgid(domain=FROM_EMAIL.split("@")[-1])
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["List-Unsubscribe"] = f"<mailto:{EMAIL_USER or FROM_EMAIL}?subject=unsubscribe>"
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
# INBOX SYNC (replies, bounces) + FOLLOW-UPS
# ─────────────────────────────────────────────
def inbox_signals(targets: list) -> Optional[tuple]:
    """targets: [(email, since_datetime)] -> (replied, bounced) or None if the inbox can't be read."""
    replied, bounced = set(), set()
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
            typ, data = m.search(None, f'(OR FROM "mailer-daemon" FROM "postmaster" SINCE {s} TEXT "{email}")')
            if typ == "OK" and data and data[0] and data[0].split():
                bounced.add(email)
        m.logout()
        return replied, bounced
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not read inbox via IMAP ({e})")
        return None


def sync_inbox() -> Optional[dict]:
    """Record replies and bounces for recent sends. Returns stats, or None if the inbox could not be read."""
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
        elif "replied" in statuses:
            replied_n += 1
        else:
            pending.append((first, sent_at, (first.get("email") or "").lower()))

    if pending:
        signals = inbox_signals([(e, t) for _, t, e in pending if e])
        if signals is None:
            return None
        replied, bounced = signals
        for first, _, email in pending:
            base = {k: first.get(k, "") for k in ("business", "city", "website", "domain", "email", "main_issue")}
            if email in bounced:
                record({**base, "status": "bounced"})
                bounced_n += 1
            elif email in replied:
                log.info(f"💬 REPLY from {email} ({first.get('business')}), answer it!")
                record({**base, "status": "replied"})
                replied_n += 1

    rate = bounced_n / recent if recent else 0.0
    stats = {"recent_sent": recent, "bounced": bounced_n, "replied": replied_n, "bounce_rate": rate,
             "paused": recent >= 10 and rate > BOUNCE_PAUSE_RATE}
    log.info(f"📊 Last {FOLLOWUP_MAX_AGE_DAYS} days: sent={recent} replied={replied_n} bounced={bounced_n} (rate {rate:.0%})")
    return stats


def followup_body(row: dict) -> str:
    phrase = ISSUE_PHRASES.get(row.get("main_issue", ""), "there are a few fixable issues on the site")
    return (
        "Hi,\n\n"
        f"Just a quick follow-up on my note from a few days ago about {row.get('website') or row.get('business') or 'your business'}. "
        f"The main thing I noticed: {phrase}.\n\n"
        "If it helps, reply \"yes\" and I'll send a free one-page report with the fixes. "
        f"If you'd like us to handle it, it takes about {DELIVERY_DAYS}.\n"
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
        if "sent" not in statuses or statuses & {"followup_sent", "replied", "bounced"}:
            continue
        first = next(r for r in rs if r.get("status") == "sent")
        sent_at, email = parse_dt(first.get("date")), (first.get("email") or "").lower()
        if sent_at and email and email not in suppressed and dom not in suppressed \
                and FOLLOWUP_AFTER_DAYS <= (now - sent_at).days <= FOLLOWUP_MAX_AGE_DAYS:
            due.append((first, email))

    sent = 0
    for first, email in due:
        if sent >= limit or time.time() - START_TS > MAX_RUNTIME_S:
            break
        base = {k: first.get(k, "") for k in ("business", "city", "website", "domain", "email", "main_issue")}
        subject = "Re: " + (first.get("subject") or "your website")
        body = followup_body(first) + "\n" + signature()
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
    base = {"business": biz["name"], "city": biz["city"], "website": f"https://{host}" if host else "", "domain": key}

    r = None
    if host:
        if host in suppressed or not is_public_host(host):
            log.info(f"⏭️  Skipping {host} (suppressed or non-public)")
            return False
        r, elapsed, notes = fetch_site(host)
        if r is None:
            log.info(f"⏭️  {host}: unreachable from our server")
            record({**base, "status": "unreachable"})
            return False
        issues = audit_site(r, elapsed, notes)
    else:   # Google Maps lists no website of its own: a perfect lead for a web developer
        issues = [{"severity": 3, "key": "no_website", "detail": "Google Maps lists no website of its own for this business"}]

    if not issues:
        log.info(f"⏭️  {key}: no issues found")
        record({**base, "status": "no_issue"})
        return False
    if not worth_emailing(issues):
        log.info(f"⏭️  {key}: only minor issues ({issues[0]['key']}), not worth an email")
        record({**base, "main_issue": issues[0]["key"], "status": "weak"})
        return False
    main_issue = issues[0]["key"]
    log.info(f"🔬 {key}: {len(issues)} issue(s), main = {main_issue}")

    phone = (site_phone(r.text) if r is not None else "") or biz.get("phone") or ""
    email = pick_api_email(biz.get("api_emails"), biz["city"], host)
    if not email and r is not None:
        email = find_public_email(host, r)
    add_call_lead(biz, base["website"] or key, phone, email, issues)      # even when we have no email

    if not email:
        log.info(f"⏭️  {key}: no usable email (call list entry saved: {bool(phone)})")
        record({**base, "main_issue": main_issue, "status": "no_email"})
        return False
    if email in suppressed or email.split("@")[-1] in suppressed:
        return False
    if email in _seen_emails:
        record({**base, "email": email, "main_issue": main_issue, "status": "dup_email"})
        return False

    ok, reason = verify_email(email)
    if not ok:
        log.info(f"⏭️  {key}: {email} failed verification ({reason})")
        record({**base, "email": email, "main_issue": main_issue, "status": "bad_email"})
        return False

    target = r.url if r is not None else ""
    mail = write_email(biz, issues, target) or fallback_email(biz, issues, target)
    body = mail["body"] + "\n" + signature()

    print("\n" + "=" * 60)
    print(f"🏢 {biz['name']} ({biz['city']}, {biz.get('country', '')})  🌐 {target or 'no website'}  [{biz.get('source', '')}]")
    print(f"📧 {email}   📌 {mail['subject']}")
    print(f"🔬 Findings: {[i['detail'] for i in issues[:3]]}")
    print(body)
    print("=" * 60)

    if DRY_RUN:
        record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue, "status": "dry_run"})
        return True

    mid = send_email(email, mail["subject"], body)
    if mid:
        _seen_emails.add(email)
        record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue,
                "status": "sent", "message_id": mid})
        return True
    record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue, "status": "send_failed"})
    return False


# ─────────────────────────────────────────────
# FREE REPORT for leads who reply "yes"
# ─────────────────────────────────────────────
def make_report(site: str) -> str:
    host = host_of(site)
    r, elapsed, notes = fetch_site(host)
    if r is None:
        return f"Could not load {host} from this machine."
    issues = audit_site(r, elapsed, notes)
    lines = [f"WEBSITE REPORT: {host}", "=" * 40, ""]
    if not issues:
        lines.append("No major technical issues found. Nice work.")
    for i, it in enumerate(issues, 1):
        lines += [f"{i}. {it['detail']}", f"   Fix: {FIXES.get(it['key'], 'We will review and fix this.')}", ""]
    lines.append(f"We can implement these fixes in {DELIVERY_DAYS}.  {COMPANY_NAME}  {COMPANY_PHONE}  {COMPANY_SITE}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main() -> None:
    if not DRY_RUN and not (EMAIL_USER and EMAIL_PASS):
        raise SystemExit("Missing EMAIL_USER / EMAIL_PASS (set them as environment variables / GitHub Secrets)")

    log.info(f"🚀 Himalaya IT Outreach Engine v4 | dry_run={DRY_RUN} | daily_limit={DAILY_LIMIT} | hours_only={BUSINESS_HOURS_ONLY}")
    if os.environ.get("GITHUB_ACTIONS") == "true" and any(LOCAL_TEST.values()):
        log.error("🔑 Keys are written inside mailor.py. Remove the LOCAL_TEST values and use GitHub Secrets instead!")
    if not RAPIDAPI_KEY:
        log.warning("RAPIDAPI_KEY not set: using free OpenStreetMap discovery only")
    if not COMPANY_ADDRESS:
        log.warning("COMPANY_ADDRESS not set: United States leads are skipped (US law requires a postal address)")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set: using template emails")

    ensure_log()
    ensure_call_log()
    rows = load_rows()
    seen, suppressed = load_seen(rows), load_suppressed()
    _seen_emails.update(load_seen_emails(rows))
    usage = load_usage()
    done, followups, paused = 0, 0, False

    if not DRY_RUN:
        stats = sync_inbox()
        if stats is None:
            log.warning("Inbox unreadable: skipping follow-ups this run (never follow up blindly)")
        else:
            paused = stats["paused"]
            if paused:
                log.error(f"🛑 Bounce rate {stats['bounce_rate']:.0%} is too high: no new emails until it recovers.")
            followups = run_followups(suppressed, FOLLOWUP_LIMIT - count_today(rows, "followup_sent"))

    remaining = DAILY_LIMIT - (0 if DRY_RUN else count_today(rows, "sent"))
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


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--report":
        print(make_report(sys.argv[2]))
    else:
        main()
