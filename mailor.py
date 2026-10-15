"""
Himalaya IT Outreach Engine v2  (small businesses worldwide)

Pipeline per run:
  1. OpenStreetMap (free Overpass API, no key) finds real local businesses that list a website
  2. We fetch each website OURSELVES and audit it (real, measurable problems only)
  3. We scrape a PUBLICLY LISTED email from the site (no guessing / no LLM-invented emails)
  4. MX-verify the email domain
  5. Gemini writes a short email based ONLY on the measured findings
  6. Send via Gmail SMTP, log to sent_log.csv (so nobody is emailed twice)

All secrets come from environment variables (GitHub Actions secrets). Nothing is hardcoded.
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
import time
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# 1. LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("himalaya_outreach.log"), logging.StreamHandler()],
)
log = logging.getLogger("himalaya")

# ─────────────────────────────────────────────
# 2. CONFIG  (secrets from env, never hardcode)
# ─────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


GEMINI_API_KEY = "AIzaSyDL6mu7ePmgpPgz-Lgp-uS9VMj4eitHbM4"
EMAIL_USER     = "ashishsinghbag02@gmail.com"
EMAIL_PASS     = "mamppvuvxekabqkx"

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
DAILY_LIMIT = int(_env("DAILY_LIMIT", "10"))          # keep low at first (Gmail reputation)
MAX_COMBOS_PER_RUN = int(_env("MAX_COMBOS", "6"))     # city x category searches per run
LEADS_PER_SEARCH = int(_env("LEADS_PER_SEARCH", "20"))
FOLLOWUP_AFTER_DAYS = int(_env("FOLLOWUP_AFTER_DAYS", "3"))   # send ONE follow-up after this many days
FOLLOWUP_MAX_AGE_DAYS = int(_env("FOLLOWUP_MAX_AGE_DAYS", "14"))
FOLLOWUP_LIMIT = int(_env("FOLLOWUP_LIMIT", "10"))            # separate cap from DAILY_LIMIT
SLEEP_MIN, SLEEP_MAX = 45, 90                          # seconds between real sends

SENDER_NAME = _env("SENDER_NAME", "Ashish")
COMPANY_NAME = _env("COMPANY_NAME", "Himalaya IT")
COMPANY_PHONE = _env("COMPANY_PHONE", "+916302449432")
COMPANY_SITE = _env("COMPANY_WEBSITE", "opentry.in")
COMPANY_ADDRESS = _env("COMPANY_ADDRESS", "")   # postal address; REQUIRED for US recipients (CAN-SPAM)
DELIVERY_DAYS = _env("DELIVERY_DAYS", "5-7 working days")

COMPANY_PITCH = (
    "Himalaya IT is a 25-member team based in Greater Noida. We fix and rebuild business "
    "websites: speed, mobile layout, SEO basics, and lead capture (WhatsApp, call and enquiry buttons)."
)

MODEL = "gemini-2.5-flash"
LOG_CSV = "sent_log.csv"
SUPPRESS_FILE = "suppression.txt"   # one email or domain per line (people who asked to stop)
LOG_FIELDS = ["date", "business", "city", "website", "domain", "email", "subject", "main_issue", "status", "message_id"]
BLOCKING_STATUSES = {"sent", "no_email", "no_issue", "unreachable", "bad_email"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HimalayaITAudit/2.0)",
    "Accept-Language": "en;q=0.9,*;q=0.5",
}

# (city, country, latitude, longitude). English-friendly business markets worldwide.
CITIES = [
    # India
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
    # United States
    ("New York", "United States", 40.71, -74.01), ("Los Angeles", "United States", 34.05, -118.24),
    ("Chicago", "United States", 41.88, -87.63), ("Houston", "United States", 29.76, -95.37),
    ("Phoenix", "United States", 33.45, -112.07), ("Dallas", "United States", 32.78, -96.80),
    ("Austin", "United States", 30.27, -97.74), ("San Diego", "United States", 32.72, -117.16),
    ("Miami", "United States", 25.76, -80.19), ("Atlanta", "United States", 33.75, -84.39),
    ("Seattle", "United States", 47.61, -122.33), ("Denver", "United States", 39.74, -104.99),
    ("Boston", "United States", 42.36, -71.06), ("Orlando", "United States", 28.54, -81.38),
    ("Charlotte", "United States", 35.23, -80.84),
    # Canada (blocked by default, see BLOCKED_COUNTRIES)
    ("Toronto", "Canada", 43.65, -79.38), ("Vancouver", "Canada", 49.28, -123.12),
    ("Calgary", "Canada", 51.05, -114.07), ("Montreal", "Canada", 45.50, -73.57),
    # United Kingdom and Ireland
    ("London", "United Kingdom", 51.51, -0.13), ("Manchester", "United Kingdom", 53.48, -2.24),
    ("Birmingham", "United Kingdom", 52.49, -1.89), ("Leeds", "United Kingdom", 53.80, -1.55),
    ("Glasgow", "United Kingdom", 55.86, -4.25), ("Bristol", "United Kingdom", 51.45, -2.59),
    ("Dublin", "Ireland", 53.35, -6.26),
    # Australia and New Zealand
    ("Sydney", "Australia", -33.87, 151.21), ("Melbourne", "Australia", -37.81, 144.96),
    ("Brisbane", "Australia", -27.47, 153.03), ("Perth", "Australia", -31.95, 115.86),
    ("Adelaide", "Australia", -34.93, 138.60), ("Auckland", "New Zealand", -36.85, 174.76),
    # Middle East, Asia
    ("Dubai", "United Arab Emirates", 25.20, 55.27), ("Abu Dhabi", "United Arab Emirates", 24.45, 54.38),
    ("Singapore", "Singapore", 1.35, 103.82), ("Kuala Lumpur", "Malaysia", 3.14, 101.69),
    ("Manila", "Philippines", 14.60, 120.98), ("Hong Kong", "Hong Kong", 22.32, 114.17),
    ("Karachi", "Pakistan", 24.86, 67.01), ("Lahore", "Pakistan", 31.55, 74.34),
    ("Dhaka", "Bangladesh", 23.81, 90.41), ("Colombo", "Sri Lanka", 6.93, 79.86), ("Kathmandu", "Nepal", 27.72, 85.32),
    # Africa
    ("Lagos", "Nigeria", 6.52, 3.38), ("Accra", "Ghana", 5.60, -0.19), ("Nairobi", "Kenya", -1.29, 36.82),
    ("Johannesburg", "South Africa", -26.20, 28.05), ("Cape Town", "South Africa", -33.92, 18.42),
    # Europe (high English use)
    ("Amsterdam", "Netherlands", 52.37, 4.90), ("Stockholm", "Sweden", 59.33, 18.07),
    ("Copenhagen", "Denmark", 55.68, 12.57),
]
BLOCKED_COUNTRIES = {c.strip().lower() for c in os.environ.get("BLOCKED_COUNTRIES", "Canada").split(",")
                     if c.strip() and c.strip().lower() != "none"}
SEARCH_RADIUS_M = int(_env("SEARCH_RADIUS_M", "12000"))
INCLUDE_CHAINS = _env("INCLUDE_CHAINS", "false").lower() == "true"
# category -> OpenStreetMap tags (key, value). Only places that list a website are returned.
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

# ─────────────────────────────────────────────
# 3. GEMINI HELPERS
# ─────────────────────────────────────────────
_client = None


def gemini() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def gemini_call(prompt: str, json_mode: bool = False, retries: int = 3) -> str:
    """Plain Gemini text call (no Google Search tool, so it stays on the free tier)."""
    cfg = {"temperature": 0.6}
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    for attempt in range(1, retries + 1):
        try:
            resp = gemini().models.generate_content(
                model=MODEL, contents=prompt, config=types.GenerateContentConfig(**cfg)
            )
            return resp.text or ""
        except Exception as e:  # noqa: BLE001
            log.warning(f"Gemini call failed (attempt {attempt}/{retries}): {e}")
            time.sleep(20 * attempt)       # free tier is rate-limited per minute
    return ""


def parse_json(text: str, want=dict):
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, want) else None
    except json.JSONDecodeError:
        pattern = r"\[[\s\S]*\]" if want is list else r"\{[\s\S]*\}"
        m = re.search(pattern, cleaned)
        if m:
            try:
                data = json.loads(m.group())
                return data if isinstance(data, want) else None
            except json.JSONDecodeError:
                return None
    return None


# ─────────────────────────────────────────────
# 4. STATE (sent log + suppression list)
# ─────────────────────────────────────────────
def ensure_log() -> None:
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def load_seen() -> set:
    seen = set()
    with open(LOG_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") in BLOCKING_STATUSES:
                seen.add(row.get("domain", ""))
    return seen


def load_suppressed() -> set:
    if not os.path.exists(SUPPRESS_FILE):
        return set()
    with open(SUPPRESS_FILE, encoding="utf-8") as f:
        return {ln.strip().lower() for ln in f if ln.strip() and not ln.startswith("#")}


def record(row: dict) -> None:
    row.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow({k: row.get(k, "") for k in LOG_FIELDS})


# ─────────────────────────────────────────────
# 5. LEAD DISCOVERY
# ─────────────────────────────────────────────
def host_of(url: str) -> str:
    if "//" not in url:
        url = "https://" + url
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_public_host(host: str) -> bool:
    """Basic SSRF guard: never fetch private / local addresses."""
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
)


OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def clean_osm_email(raw: Optional[str]) -> Optional[str]:
    """OSM sometimes lists a public business email; accept it only if it looks like a real address."""
    if not raw:
        return None
    for e in re.split(r"[;,\s]+", raw.lower()):
        if EMAIL_RE.fullmatch(e) and not any(j in e for j in JUNK_SUBSTR) and not e.endswith(JUNK_EXT):
            return e
    return None


def overpass_query(lat: float, lon: float, tags: list) -> str:
    parts = "".join(
        f'nwr["{k}"="{v}"][~"^(contact:)?website$"~"."](around:{SEARCH_RADIUS_M},{lat},{lon});' for k, v in tags
    )
    return f"[out:json][timeout:90];({parts});out tags 200;"


def discover(city: tuple, category: str) -> list:
    """Free lead source: OpenStreetMap businesses near `city` that list their own website."""
    name_, country, lat, lon = city
    log.info(f"🔎 Discovering (OpenStreetMap): {category} in {name_}, {country}")
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
    random.Random(f"{date.today()}-{name_}-{category}").shuffle(elements)   # vary leads day to day
    out, seen_hosts = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        site = tags.get("website") or tags.get("contact:website")
        biz_name = (tags.get("name") or "").strip()
        if not site or not biz_name:
            continue
        if not INCLUDE_CHAINS and (tags.get("brand") or tags.get("brand:wikidata") or tags.get("operator:wikidata")):
            continue                      # focus on small independent businesses
        h = host_of(str(site))
        if not h or "." not in h or h in seen_hosts or any(d in h for d in DIRECTORY_DOMAINS):
            continue
        seen_hosts.add(h)
        out.append({
            "name": biz_name, "host": h, "city": name_, "country": country, "category": category,
            "osm_email": tags.get("email") or tags.get("contact:email"),
        })
        if len(out) >= LEADS_PER_SEARCH:
            break
    log.info(f"   → {len(out)} candidate websites")
    return out


# ─────────────────────────────────────────────
# 6. WEBSITE AUDIT (real measurements only)
# ─────────────────────────────────────────────
def fetch_site(host: str):
    notes = {"ssl_error": False}
    for scheme in ("https", "http"):
        try:
            t0 = time.time()
            r = requests.get(f"{scheme}://{host}/", headers=HEADERS, timeout=15, allow_redirects=True)
            elapsed = time.time() - t0
            if r.status_code < 400 and "html" in r.headers.get("Content-Type", "").lower():
                return r, elapsed, notes
        except requests.exceptions.SSLError:
            notes["ssl_error"] = True
        except requests.RequestException:
            pass
    return None, None, notes


def audit_site(r: requests.Response, elapsed: float, notes: dict) -> list:
    """Return list of issues sorted worst-first. Each: {severity, key, detail}."""
    issues = []

    def add(sev: int, key: str, detail: str):
        issues.append({"severity": sev, "key": key, "detail": detail})

    soup = BeautifulSoup(r.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    final_url = r.url

    # Security / trust
    if notes.get("ssl_error"):
        add(3, "ssl_invalid", "The HTTPS certificate is invalid/expired, so browsers show a 'Not secure' warning to visitors")
    elif not final_url.startswith("https://"):
        add(3, "no_https", "The site loads over plain HTTP (no HTTPS), so browsers mark it 'Not secure'")
    else:
        mixed = soup.select("img[src^='http://'], script[src^='http://'], iframe[src^='http://'], link[rel=stylesheet][href^='http://']")
        if mixed:
            add(2, "mixed_content", f"{len(mixed)} resources load over insecure HTTP on an HTTPS page")

    # Mobile
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        add(3, "no_viewport", "No mobile viewport tag, so the site likely displays as a shrunken desktop page on phones")

    # Speed (server-side measurement)
    if elapsed and elapsed > 3.0:
        add(3, "slow", f"The homepage took {elapsed:.1f}s just to download its HTML in our test")
    elif elapsed and elapsed > 1.8:
        add(2, "slow", f"The homepage took {elapsed:.1f}s just to download its HTML in our test")

    if len(r.content) > 2_000_000:
        add(2, "heavy_page", f"The homepage HTML alone is {len(r.content)/1e6:.1f} MB")

    imgs = soup.find_all("img")
    eager = [i for i in imgs if (i.get("loading") or "").lower() != "lazy"]
    if len(eager) > 25:
        add(2, "heavy_images", f"{len(eager)} images load upfront without lazy-loading, which slows phones on mobile data")
    if len(imgs) >= 5:
        no_alt = [i for i in imgs if not (i.get("alt") or "").strip()]
        if len(no_alt) / len(imgs) > 0.5:
            add(1, "no_alt", f"{len(no_alt)} of {len(imgs)} images have no alt text (hurts Google Images and accessibility)")

    # Lead capture (most important for local business)
    hrefs = [(a.get("href") or "").lower() for a in soup.find_all("a")]
    has_tel = any(h.startswith("tel:") for h in hrefs)
    has_wa = any("wa.me" in h or "whatsapp" in h for h in hrefs)
    has_form = soup.find("form") is not None
    if not (has_tel or has_wa or has_form):
        add(3, "no_lead_capture", "The homepage has no click-to-call, WhatsApp button or enquiry form, so interested visitors have no easy way to reach out")
    elif not (has_tel or has_wa):
        add(2, "no_quick_contact", "No click-to-call or WhatsApp button, which most mobile visitors expect")

    # SEO basics
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    if not title:
        add(2, "no_title", "The homepage has no page title, so Google has nothing to show in search results")
    elif len(title) < 15:
        add(1, "short_title", f"The page title is only '{title}', too generic to rank for local searches")
    if not soup.find("meta", attrs={"name": re.compile("^description$", re.I)}):
        add(2, "no_meta_desc", "No meta description, so Google picks random text for your search listing")
    if not soup.find("h1"):
        add(1, "no_h1", "No main heading (H1) on the homepage")

    # Looks abandoned
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:20\d{2}\s*[-–]\s*)?(20\d{2})", page_text, re.I)]
    if years and max(years) < date.today().year - 1:
        add(2, "stale_year", f"The footer still says © {max(years)}, which makes the business look inactive")

    issues.sort(key=lambda x: -x["severity"])
    return issues


# ─────────────────────────────────────────────
# 7. EMAIL DISCOVERY (publicly listed only) + VERIFICATION
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
    for el in soup.select("[data-cfemail]"):          # Cloudflare-obfuscated emails
        try:
            found.add(_decode_cf(el["data-cfemail"]))
        except Exception:  # noqa: BLE001
            pass
    return found


def find_public_email(host: str, home: requests.Response) -> Optional[str]:
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
            continue          # third-party address (web agency, hosting, etc.) – ignore
        pref = 0 if e.split("@")[0] in PREFERRED_PREFIX else 1
        scored.append((rank, pref, e))
    scored.sort()
    return scored[0][2] if scored else None


def verify_email(email: str) -> tuple:
    """
    MX check only. SMTP RCPT probing is NOT used: GitHub Actions blocks outbound port 25,
    and many servers accept-all, so the probe gives false confidence.
    """
    if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", email):
        return False, "bad syntax"
    domain = email.split("@")[-1]
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 8
        answers = resolver.resolve(domain, "MX")
        if not len(answers):
            return False, "no MX"
        return True, "MX ok"
    except Exception as e:  # noqa: BLE001
        return False, f"MX lookup failed: {e}"


# ─────────────────────────────────────────────
# 8. EMAIL WRITING
# ─────────────────────────────────────────────
def write_email(biz: dict, issues: list, website: str) -> Optional[dict]:
    top = issues[:3]
    findings = "\n".join(f"{i+1}. {x['detail']}" for i, x in enumerate(top))
    prompt = f"""
Write a short cold email from {SENDER_NAME} at {COMPANY_NAME} to the owner of "{biz['name']}"
({biz['category']}, {biz['city']}, {biz.get('country', '')}, website: {website}).

ABOUT US: {COMPANY_PITCH}
DELIVERY TIME TO FIX: {DELIVERY_DAYS}

WHAT WE MEASURED ON THEIR WEBSITE (the ONLY facts you may state about their site):
{findings}

RULES:
- Plain text, 90 to 130 words, no markdown, no emojis.
- Start with "Hi," (we do not know the owner's name). Do not write a signature or sign-off; it is added automatically.
- Sentence 1: say we looked at their website and name the MAIN problem (finding #1) in plain words, and what it costs them (lost enquiries, lower Google ranking, visitors leaving).
- Optionally mention finding #2 in one short clause. Do not mention anything not in the list.
- Then one sentence on who we are, one on what we will fix, and say we can do it in {DELIVERY_DAYS}.
- End with a soft call to action: reply "yes" and we will send a free one-page report of the fixes.
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
        if subject and 60 <= words <= 170 and not re.search(r"\[[^\]]+\]|\{[^}]+\}", body + subject):
            return {"subject": subject[:90], "body": body}
        log.warning(f"Rejected generated email (words={words}); retrying")
    return None


def fallback_email(biz: dict, issues: list, website: str) -> dict:
    """Template email used if Gemini is rate-limited/unavailable. Uses only measured findings."""
    main = ISSUE_PHRASES.get(issues[0]["key"], "there are a few fixable issues on the site")
    extra = ""
    if len(issues) > 1:
        extra = f" I also noticed {ISSUE_PHRASES.get(issues[1]['key'], 'another fixable issue')}."
    body = (
        "Hi,\n\n"
        f"I took a look at {website} and noticed that {main}.{extra} "
        "Issues like this can mean lost enquiries and a lower Google ranking.\n\n"
        f"I'm with {COMPANY_NAME}, a web and software development team. We build modern websites and apps and fix "
        f"existing ones: speed, mobile layout, SEO and lead capture. We can fix this in {DELIVERY_DAYS}.\n\n"
        "If you'd like, reply \"yes\" and we'll send a free one-page report of the fixes."
    )
    return {"subject": f"Quick note about {biz['name']}'s website"[:90], "body": body}


def signature() -> str:
    lines = ["", "Regards,", SENDER_NAME, COMPANY_NAME]
    if COMPANY_PHONE:
        lines.append(COMPANY_PHONE)
    if COMPANY_SITE:
        lines.append(COMPANY_SITE)
    if COMPANY_ADDRESS:
        lines.append(COMPANY_ADDRESS)
    lines += ["", "P.S. If this isn't relevant, just reply \"no\" and I won't email you again."]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 9. SENDING
# ─────────────────────────────────────────────
def send_email(to_email: str, subject: str, body: str, in_reply_to: Optional[str] = None) -> Optional[str]:
    msg = MIMEMultipart()
    msg["From"] = formataddr((f"{SENDER_NAME} | {COMPANY_NAME}", EMAIL_USER))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = EMAIL_USER
    msg["Date"] = formatdate(localtime=True)
    mid = make_msgid(domain=EMAIL_USER.split("@")[-1])
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["List-Unsubscribe"] = f"<mailto:{EMAIL_USER}?subject=unsubscribe>"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        log.info(f"✅ Sent to {to_email}")
        return mid
    except smtplib.SMTPAuthenticationError:
        log.error("❌ Gmail auth failed. Check EMAIL_USER / EMAIL_PASS (App Password).")
    except smtplib.SMTPRecipientsRefused:
        log.error(f"❌ Recipient refused: {to_email}")
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ SMTP error: {e}")
    return None


# ─────────────────────────────────────────────
# 10. MAIN
# ─────────────────────────────────────────────
ISSUE_PHRASES = {
    "ssl_invalid": "the site's security certificate is invalid, so visitors see a 'Not secure' warning",
    "no_https": "the site has no HTTPS, so browsers label it 'Not secure'",
    "mixed_content": "some parts of the site load insecurely, which can trigger browser warnings",
    "no_viewport": "the site isn't set up for mobile screens, so it looks shrunken on phones",
    "slow": "the homepage loads slowly, and slow sites lose visitors before they ever enquire",
    "heavy_page": "the homepage is very heavy, which slows it down on mobile data",
    "heavy_images": "lots of images load upfront, which slows the site on phones",
    "no_alt": "most images have no alt text, which hurts Google Images and accessibility",
    "no_lead_capture": "there's no call, WhatsApp or enquiry button, so interested visitors can't easily reach you",
    "no_quick_contact": "there's no click-to-call or WhatsApp button, which most mobile visitors expect",
    "no_title": "the homepage has no page title, so Google has nothing to show in search",
    "short_title": "the page title is too generic to rank for local searches",
    "no_meta_desc": "there's no meta description, so Google picks random text for your listing",
    "no_h1": "the homepage has no main heading, which weakens search ranking",
    "stale_year": "the footer year is out of date, which makes the business look inactive",
}


def load_rows() -> list:
    with open(LOG_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def inbox_signals(targets: list) -> Optional[tuple]:
    """
    targets: [(email, since_date)]. Returns (replied_emails, bounced_emails) or None if the
    inbox could not be checked (then we must NOT send follow-ups blindly).
    """
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
        log.warning(f"Could not check inbox via IMAP ({e}); skipping follow-ups this run")
        return None


def followup_body(row: dict) -> str:
    phrase = ISSUE_PHRASES.get(row.get("main_issue", ""), "there are a few fixable issues on the site")
    return (
        "Hi,\n\n"
        f"Just a quick follow-up on my note from a few days ago about {row.get('website', 'your website')}. "
        f"The main thing I noticed: {phrase}.\n\n"
        "If it helps, reply \"yes\" and I'll send a free one-page report with the fixes. "
        f"If you'd like us to handle it, it takes about {DELIVERY_DAYS}.\n"
    )


def run_followups(suppressed: set) -> int:
    """One follow-up per lead, only if no reply/bounce is found. Returns number sent."""
    rows = load_rows()
    by_domain = {}
    for r in rows:
        by_domain.setdefault(r.get("domain", ""), []).append(r)

    now, due = datetime.now(timezone.utc), []
    for dom, rs in by_domain.items():
        statuses = {r.get("status") for r in rs}
        if "sent" not in statuses or statuses & {"followup_sent", "replied", "bounced"}:
            continue
        first = next(r for r in rs if r.get("status") == "sent")
        try:
            sent_at = datetime.strptime(first["date"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        email = (first.get("email") or "").lower()
        age = (now - sent_at).days
        if FOLLOWUP_AFTER_DAYS <= age <= FOLLOWUP_MAX_AGE_DAYS and email and email not in suppressed and dom not in suppressed:
            due.append((first, sent_at, email))

    if not due:
        return 0
    log.info(f"📬 {len(due)} lead(s) due for follow-up; checking inbox for replies/bounces")
    signals = inbox_signals([(e, t) for _, t, e in due])
    if signals is None:
        return 0
    replied, bounced = signals

    sent_count = 0
    for first, _, email in due:
        base = {k: first.get(k, "") for k in ("business", "city", "website", "domain", "email", "main_issue")}
        if email in bounced:
            log.info(f"↩️  {email} bounced")
            record({**base, "status": "bounced"})
            continue
        if email in replied:
            log.info(f"💬 {email} replied: handle manually, no follow-up")
            record({**base, "status": "replied"})
            continue
        if sent_count >= FOLLOWUP_LIMIT:
            continue
        subject = "Re: " + (first.get("subject") or "your website")
        body = followup_body(first) + "\n" + signature()
        print(f"\n--- FOLLOW-UP to {email} ---\n{body}")
        mid = send_email(email, subject, body, in_reply_to=first.get("message_id") or None)
        if mid:
            record({**base, "subject": subject, "status": "followup_sent", "message_id": mid})
            sent_count += 1
            time.sleep(random.randint(SLEEP_MIN, SLEEP_MAX))
    return sent_count


def pick_combos(n: int) -> list:
    rng = random.Random(date.today().toordinal())     # different mix every day, reproducible per day
    cities = [c for c in CITIES
              if c[1].lower() not in BLOCKED_COUNTRIES
              and not (c[1] == "United States" and not COMPANY_ADDRESS)]
    combos = [(c, k) for c in cities for k in CATEGORIES]
    rng.shuffle(combos)
    return combos[:n]


def process_lead(biz: dict, suppressed: set) -> bool:
    """Returns True if an email was sent (or dry-run produced)."""
    host = biz["host"]
    base = {"business": biz["name"], "city": biz["city"], "website": f"https://{host}", "domain": host}

    if host in suppressed or not is_public_host(host):
        log.info(f"⏭️  Skipping {host} (suppressed or non-public)")
        return False

    r, elapsed, notes = fetch_site(host)
    if r is None:
        log.info(f"⏭️  {host}: unreachable")
        record({**base, "status": "unreachable"})
        return False

    issues = audit_site(r, elapsed, notes)
    if not issues:
        log.info(f"⏭️  {host}: no real issues found, not emailing")
        record({**base, "status": "no_issue"})
        return False
    main_issue = issues[0]["key"]
    log.info(f"🔬 {host}: {len(issues)} issues, main = {main_issue}")

    email = find_public_email(host, r) or clean_osm_email(biz.get("osm_email"))
    if not email:
        log.info(f"⏭️  {host}: no public email on site")
        record({**base, "main_issue": main_issue, "status": "no_email"})
        return False
    if email in suppressed or email.split("@")[-1] in suppressed:
        return False

    ok, reason = verify_email(email)
    if not ok:
        log.info(f"⏭️  {host}: {email} failed verification ({reason})")
        record({**base, "email": email, "main_issue": main_issue, "status": "bad_email"})
        return False

    mail = write_email(biz, issues, r.url)
    if not mail:
        log.warning(f"{host}: Gemini unavailable/invalid, using template email")
        mail = fallback_email(biz, issues, r.url)
    body = mail["body"] + "\n" + signature()

    print("\n" + "=" * 60)
    print(f"🏢 {biz['name']} ({biz['city']})  🌐 {r.url}")
    print(f"📧 {email}   📌 {mail['subject']}")
    print(f"🔬 Findings: {[i['detail'] for i in issues[:3]]}")
    print(body)
    print("=" * 60)

    if DRY_RUN:
        record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue, "status": "dry_run"})
        return True

    mid = send_email(email, mail["subject"], body)
    if mid:
        record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue,
                "status": "sent", "message_id": mid})
        return True
    record({**base, "email": email, "subject": mail["subject"], "main_issue": main_issue, "status": "send_failed"})
    return False


def main() -> None:
    missing = [n for n, v in (("GEMINI_API_KEY", GEMINI_API_KEY), ("EMAIL_USER", EMAIL_USER), ("EMAIL_PASS", EMAIL_PASS)) if not v]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    log.info(f"🚀 Himalaya IT Outreach Engine v2 | dry_run={DRY_RUN} | daily_limit={DAILY_LIMIT}")
    if not COMPANY_ADDRESS:
        log.warning("COMPANY_ADDRESS not set: United States leads are skipped (US law requires a postal address)")
    if BLOCKED_COUNTRIES:
        log.info(f"Blocked countries: {sorted(BLOCKED_COUNTRIES)}")
    ensure_log()
    seen, suppressed = load_seen(), load_suppressed()
    done = 0
    followups = 0
    if not DRY_RUN:
        try:
            followups = run_followups(suppressed)
        except Exception as e:  # noqa: BLE001
            log.exception(f"Follow-up stage failed: {e}")

    for city, category in pick_combos(MAX_COMBOS_PER_RUN):
        if done >= DAILY_LIMIT:
            break
        for biz in discover(city, category):
            if done >= DAILY_LIMIT:
                break
            if biz["host"] in seen:
                continue
            seen.add(biz["host"])
            try:
                if process_lead(biz, suppressed):
                    done += 1
                    if not DRY_RUN and done < DAILY_LIMIT:
                        time.sleep(random.randint(SLEEP_MIN, SLEEP_MAX))
            except Exception as e:  # noqa: BLE001  (one bad site must never kill the run)
                log.exception(f"Unexpected error on {biz['host']}: {e}")

    log.info(f"✅ Finished. {'Drafted' if DRY_RUN else 'Sent'} {done} new email(s) + {followups} follow-up(s) this run.")


if __name__ == "__main__":
    main()
