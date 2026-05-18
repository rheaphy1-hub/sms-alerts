"""
Hotline — SMS Alert System. Single-file Vercel deployment.
"""
import os, re, json, logging, io
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None
from contextlib import contextmanager
from fastapi import FastAPI, Form, Response, Query, Request
from fastapi.responses import JSONResponse

# PDF + QR generation (required at top level for Vercel to bundle correctly)
import urllib.request as _urllib_req
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    from PIL import Image as PILImage
    from reportlab.lib import colors as rl_colors
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader as RLImageReader
    _PDF_LIBS_OK = True
except ImportError as _pdf_import_err:
    _PDF_LIBS_OK = False
    logging.getLogger("sms").warning(f"PDF/QR libs not available: {_pdf_import_err}")

import hmac, hashlib, secrets, time as _time
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sms")

# --- Database ---
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres")
if USE_POSTGRES:
    import psycopg2, psycopg2.extras
else:
    import sqlite3

def _pg_connect():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL.startswith("postgres://") else DATABASE_URL
    c = psycopg2.connect(url); c.autocommit = False; return c

def _sqlite_connect():
    c = sqlite3.connect(os.getenv("DB_PATH", "alerts.db")); c.row_factory = sqlite3.Row; c.execute("PRAGMA journal_mode=WAL"); return c

@contextmanager
def get_db():
    conn = _pg_connect() if USE_POSTGRES else _sqlite_connect()
    try: yield conn; conn.commit()
    finally: conn.close()

def _fetchone(conn, q, p=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
    cur.execute(q, p); row = cur.fetchone(); return dict(row) if row else None

def _fetchall(conn, q, p=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
    cur.execute(q, p); return [dict(r) for r in cur.fetchall()]

def _execute(conn, q, p=()):
    cur = conn.cursor(); cur.execute(q, p); return cur

def _q(query): return query.replace("?", "%s") if USE_POSTGRES else query
def _normalize_phone(p): return p.replace(" ","").replace("-","").replace("(","").replace(")","")

def init_db():
    s = "SERIAL" if USE_POSTGRES else "INTEGER"
    pk = "PRIMARY KEY" if USE_POSTGRES else "PRIMARY KEY AUTOINCREMENT"
    # Each statement in its own transaction — prevents a pre-existing table
    # from aborting the batch and silently skipping new tables.
    statements = [
        f"""CREATE TABLE IF NOT EXISTS businesses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT \'\', owner_phone TEXT NOT NULL,
            alert_phones TEXT NOT NULL DEFAULT \'\', email TEXT NOT NULL DEFAULT \'\',
            business_code TEXT NOT NULL DEFAULT \'\',
            digest_freq TEXT NOT NULL DEFAULT \'weekly\', alert_tier3 INTEGER DEFAULT 0,
            website_url TEXT NOT NULL DEFAULT \'\', website_info TEXT NOT NULL DEFAULT \'\',
            twilio_number TEXT NOT NULL DEFAULT \'\', muted_until TEXT, paused INTEGER DEFAULT 0,
            created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id {s} {pk}, business_id TEXT NOT NULL, from_number TEXT NOT NULL,
            message_text TEXT NOT NULL, tier INTEGER, category TEXT, sentiment TEXT,
            confidence REAL, summary TEXT, acknowledged INTEGER DEFAULT 0,
            alerted INTEGER DEFAULT 0, created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS alert_log (
            id {s} {pk}, message_id INTEGER NOT NULL, business_id TEXT NOT NULL,
            alert_type TEXT NOT NULL, sent_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS pending_signups (
            id {s} {pk}, name TEXT NOT NULL, owner_phone TEXT NOT NULL,
            phone2 TEXT NOT NULL DEFAULT \'\', email TEXT NOT NULL DEFAULT \'\',
            website_url TEXT NOT NULL DEFAULT \'\',
            provisioned INTEGER DEFAULT 0, created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS conversation_state (
            id {s} {pk}, business_id TEXT NOT NULL, customer_phone TEXT NOT NULL,
            last_owner_reply_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_phone)",
        "CREATE INDEX IF NOT EXISTS idx_msg_biz ON messages(business_id, tier, acknowledged)",
        "CREATE INDEX IF NOT EXISTS idx_conv_biz_cust ON conversation_state(business_id, customer_phone)",
    ]
    for stmt in statements:
        try:
            with get_db() as c: _execute(c, stmt)
        except Exception as e: logger.warning(f"init_db stmt skipped: {e}")
    for col, default in [("alert_phones","\'\'"),("email","\'\'"),("digest_freq","\'weekly\'"),
                         ("alert_tier3","0"),("website_url","\'\'"),("website_info","\'\'"),
                         ("owner_context","\'0\'"),("owner_reply_mode","\'0\'"),
                         ("business_code","\'\'"),("trial_ends_at","\'\'"),
                         ("sub_status","\'trialing\'"),("stripe_customer_id","\'\'"),
                         ("stripe_sub_id","\'\'"),("zip","\'\'"),("city","\'\'"),("state","\'\'")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE businesses ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except: pass
    # messages table additive columns
    for col, default in [("auto_reply","\'\'")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE messages ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except: pass


def _gen_business_code():
    """Generate a unique 6-char business code like BC4729."""
    import random, string
    while True:
        code = "BC" + "".join(random.choices(string.digits, k=4))
        with get_db() as c:
            existing = _fetchone(c, _q("SELECT id FROM businesses WHERE business_code=?"), (code,))
        if not existing:
            return code

def backfill_business_codes():
    """Assign business_code to any existing businesses that don't have one."""
    with get_db() as c:
        rows = _fetchall(c, "SELECT id FROM businesses WHERE business_code='' OR business_code IS NULL")
    for row in rows:
        code = _gen_business_code()
        with get_db() as c:
            _execute(c, _q("UPDATE businesses SET business_code=? WHERE id=?"), (code, row["id"]))
        logger.info(f"Backfilled business_code {code} for {row['id']}")

def get_business_by_code(code):
    """Look up a business by its BC#### code (case-insensitive)."""
    clean = code.upper().strip()
    with get_db() as c:
        return _fetchone(c, _q("SELECT * FROM businesses WHERE business_code=?"), (clean,))

def _lookup_zip(zip_code):
    """Return (city, state) from a US zip code using zippopotam.us. Returns ('','') on any failure."""
    if not zip_code or not re.match(r"^\d{5}$", zip_code.strip()):
        return ("", "")
    try:
        url = f"https://api.zippopotam.us/us/{zip_code.strip()}"
        req = _urllib_req.Request(url, headers={"User-Agent": "Hotline/1.0"})
        with _urllib_req.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        city = data["places"][0]["place name"]
        state = data["places"][0]["state abbreviation"]
        return (city, state)
    except Exception as e:
        logger.warning(f"[ZIP LOOKUP] Failed for {zip_code}: {e}")
        return ("", "")

def create_business(biz_id, name, owner_phone, twilio_number="", extra_phones="", email="", website_url="", business_code="", zip_code=""):
    now = datetime.now(timezone.utc).isoformat()
    all_phones = ",".join([owner_phone] + [p.strip() for p in extra_phones.split(",") if p.strip()]) if extra_phones else owner_phone
    website_info = scrape_website_info(website_url) if website_url else ""
    if not business_code:
        business_code = _gen_business_code()
    trial_end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    city, state = _lookup_zip(zip_code) if zip_code else ("", "")
    try:
        with get_db() as c:
            _execute(c, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,email,website_url,website_info,twilio_number,business_code,trial_ends_at,sub_status,zip,city,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
                     (biz_id, name, owner_phone, all_phones, email or "", website_url or "", website_info, twilio_number or "", business_code, trial_end, "trialing", zip_code or "", city, state, now))
        return business_code
    except: return None

def get_alert_phones(biz):
    phones = [p.strip() for p in (biz.get("alert_phones") or biz.get("owner_phone") or "").split(",") if p.strip()]
    owner = biz.get("owner_phone","")
    if owner and owner not in phones: phones.insert(0, owner)
    return phones

def get_business_by_twilio(twilio_number):
    clean = _normalize_phone(twilio_number)
    with get_db() as c:
        row = _fetchone(c, _q("SELECT * FROM businesses WHERE twilio_number=?"), (clean,))
        if row: return row
        for r in _fetchall(c, "SELECT * FROM businesses"):
            if _normalize_phone(r["twilio_number"])[-10:] == clean[-10:]: return r
    return None

def get_business_by_owner(owner_phone):
    clean = _normalize_phone(owner_phone)
    with get_db() as c:
        row = _fetchone(c, _q("SELECT * FROM businesses WHERE owner_phone=?"), (clean,))
        if row: return row
        for r in _fetchall(c, "SELECT * FROM businesses"):
            if _normalize_phone(r["owner_phone"])[-10:] == clean[-10:]: return r
            for p in (r.get("alert_phones") or "").split(","):
                if p.strip() and _normalize_phone(p.strip())[-10:] == clean[-10:]: return r
    return None

def store_message(bid, fn, mt, cl):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as c:
        q = _q("INSERT INTO messages (business_id,from_number,message_text,tier,category,sentiment,confidence,summary,created_at) VALUES (?,?,?,?,?,?,?,?,?)")
        p = (bid,fn,mt,cl.get("tier"),cl.get("category"),cl.get("sentiment"),cl.get("confidence"),cl.get("summary",""),now)
        if USE_POSTGRES: cur = _execute(c, q+" RETURNING id", p); return cur.fetchone()[0]
        else: return _execute(c, q, p).lastrowid

def log_alert(mid, bid, at):
    with get_db() as c: _execute(c, _q("INSERT INTO alert_log (message_id,business_id,alert_type,sent_at) VALUES (?,?,?,?)"), (mid,bid,at,datetime.now(timezone.utc).isoformat()))

def update_auto_reply(mid, text):
    with get_db() as c: _execute(c, _q("UPDATE messages SET auto_reply=? WHERE id=?"), (text or "", mid))

# --- Live conversation state (15-min owner-takeover window) ---
CONVERSATION_WINDOW_MIN = 15

def mark_owner_replied(bid, customer_phone):
    """Record that the owner just replied to this customer. Suppresses AI auto-replies for CONVERSATION_WINDOW_MIN minutes."""
    now = datetime.now(timezone.utc).isoformat()
    cust = _normalize_phone(customer_phone)
    try:
        with get_db() as c:
            row = _fetchone(c, _q("SELECT id FROM conversation_state WHERE business_id=? AND customer_phone=?"), (bid, cust))
            if row:
                _execute(c, _q("UPDATE conversation_state SET last_owner_reply_at=? WHERE id=?"), (now, row["id"]))
            else:
                _execute(c, _q("INSERT INTO conversation_state (business_id,customer_phone,last_owner_reply_at) VALUES (?,?,?)"), (bid, cust, now))
        logger.info(f"[CONVO MARK] bid={bid} cust=...{cust[-4:]}")
    except Exception as e:
        logger.error(f"[CONVO MARK] {e}")

def end_conversation(bid, customer_phone):
    """Clear the active window so AI resumes immediately."""
    cust = _normalize_phone(customer_phone)
    try:
        with get_db() as c:
            _execute(c, _q("DELETE FROM conversation_state WHERE business_id=? AND customer_phone=?"), (bid, cust))
        logger.info(f"[CONVO END] bid={bid} cust=...{cust[-4:]}")
    except Exception as e:
        logger.error(f"[CONVO END] {e}")

def is_conversation_active(bid, customer_phone):
    """True if owner replied to this customer in the last CONVERSATION_WINDOW_MIN minutes."""
    cust = _normalize_phone(customer_phone)
    try:
        with get_db() as c:
            row = _fetchone(c, _q("SELECT last_owner_reply_at FROM conversation_state WHERE business_id=? AND customer_phone=?"), (bid, cust))
        if not row: return False
        last = datetime.fromisoformat(row["last_owner_reply_at"])
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        active = (datetime.now(timezone.utc) - last) < timedelta(minutes=CONVERSATION_WINDOW_MIN)
        logger.info(f"[CONVO CHECK] bid={bid} cust=...{cust[-4:]} active={active}")
        return active
    except Exception as e:
        logger.error(f"[CONVO CHECK] {e}")
        return False


def mark_acknowledged(mid):
    with get_db() as c: _execute(c, _q("UPDATE messages SET acknowledged=1 WHERE id=?"), (mid,))

def mark_alerted(mid):
    with get_db() as c: _execute(c, _q("UPDATE messages SET alerted=1 WHERE id=?"), (mid,))

def get_latest_unacked(bid):
    with get_db() as c: return _fetchone(c, _q("SELECT * FROM messages WHERE business_id=? AND tier IN (1,2) AND acknowledged=0 ORDER BY created_at DESC LIMIT 1"), (bid,))

def get_message_by_id(mid):
    with get_db() as c: return _fetchone(c, _q("SELECT * FROM messages WHERE id=?"), (mid,))

def get_recent_flagged(bid, limit=5):
    with get_db() as c: return _fetchall(c, _q("SELECT * FROM messages WHERE business_id=? AND tier IN (1,2) ORDER BY created_at DESC LIMIT ?"), (bid, limit))

def get_recent_all(bid, limit=5):
    # Order by id (monotonic insertion order) rather than created_at (text
    # column — vulnerable to format mismatches across legacy rows).
    with get_db() as c: return _fetchall(c, _q("SELECT * FROM messages WHERE business_id=? ORDER BY id DESC LIMIT ?"), (bid, limit))

def get_recent_alert_count(bid, minutes=10):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with get_db() as c:
        row = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM alert_log WHERE business_id=? AND sent_at>?"), (bid, cutoff))
        return row["cnt"] if row else 0

def is_alerts_silenced(biz):
    if biz.get("paused"): return True
    mu = biz.get("muted_until")
    if mu:
        try:
            if datetime.fromisoformat(mu) > datetime.now(timezone.utc): return True
        except: pass
    return False

def set_muted_until(bid, until):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET muted_until=? WHERE id=?"), (until.isoformat() if until else None, bid))

def set_paused(bid, paused):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET paused=? WHERE id=?"), (1 if paused else 0, bid))

def set_digest_freq(bid, freq):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET digest_freq=? WHERE id=?"), (freq, bid))

def set_alert_tier3(bid, on):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET alert_tier3=? WHERE id=?"), (1 if on else 0, bid))

def get_all_businesses():
    with get_db() as c: return _fetchall(c, "SELECT * FROM businesses")

# --- Trial / Subscription helpers ---
def can_send_alerts(biz):
    """Allow alerts if trialing (within window) or active. Block if expired/canceled."""
    status = biz.get("sub_status") or "trialing"
    if status == "active":
        return True
    trial_end = (biz.get("trial_ends_at") or "").strip()
    if trial_end:
        try:
            if datetime.fromisoformat(trial_end) > datetime.now(timezone.utc):
                return True
        except Exception:
            pass
    return False

def trial_days_left(biz):
    trial_end = (biz.get("trial_ends_at") or "").strip()
    if not trial_end:
        return 0
    try:
        delta = datetime.fromisoformat(trial_end) - datetime.now(timezone.utc)
        return max(0, delta.days)
    except Exception:
        return 0

def set_sub_status(bid, status, stripe_customer_id="", stripe_sub_id=""):
    with get_db() as c:
        if stripe_customer_id and stripe_sub_id:
            _execute(c, _q("UPDATE businesses SET sub_status=?,stripe_customer_id=?,stripe_sub_id=? WHERE id=?"),
                     (status, stripe_customer_id, stripe_sub_id, bid))
        elif stripe_customer_id:
            _execute(c, _q("UPDATE businesses SET sub_status=?,stripe_customer_id=? WHERE id=?"),
                     (status, stripe_customer_id, bid))
        else:
            _execute(c, _q("UPDATE businesses SET sub_status=? WHERE id=?"), (status, bid))

def get_business_by_stripe_customer(stripe_customer_id):
    with get_db() as c:
        return _fetchone(c, _q("SELECT * FROM businesses WHERE stripe_customer_id=?"), (stripe_customer_id,))

def send_trial_warnings():
    """Daily cron: warn on day 13, expire on day 14+."""
    sent = 0
    PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "")
    for biz in get_all_businesses():
        status = biz.get("sub_status") or "trialing"
        if status != "trialing":
            continue
        days = trial_days_left(biz)
        phones = get_alert_phones(biz)
        if days == 1:
            link_part = f"\nSubscribe so you don't miss a critical issue from your customers \u26a0\ufe0f\n{PAYMENT_LINK}" if PAYMENT_LINK else ""
            msg = f"Your free Hotline trial ends tomorrow.{link_part}"
            for p in phones: send_sms(p, msg)
            logger.info(f"[TRIAL WARNING] {biz['id']}")
            sent += 1
        elif days == 0:
            set_sub_status(biz["id"], "expired")
            link_part = f"\n{PAYMENT_LINK}" if PAYMENT_LINK else " Reply BILLING to reactivate."
            msg = f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers \u26a0\ufe0f{link_part}"
            for p in phones: send_sms(p, msg)
            logger.info(f"[TRIAL EXPIRED] {biz['id']}")
            sent += 1
    return sent


def save_pending_signup(name, phone, phone2, email, website_url):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as c:
            _execute(c, _q("INSERT INTO pending_signups (name,owner_phone,phone2,email,website_url,created_at) VALUES (?,?,?,?,?,?)"),
                     (name, phone, phone2 or "", email or "", website_url or "", now))
        return True
    except Exception as e: logger.error(f"Save pending signup failed: {e}"); return False

def get_pending_signups():
    with get_db() as c: return _fetchall(c, "SELECT * FROM pending_signups WHERE provisioned=0 ORDER BY created_at DESC")

def mark_pending_provisioned(pending_id):
    with get_db() as c: _execute(c, _q("UPDATE pending_signups SET provisioned=1 WHERE id=?"), (pending_id,))

def get_stats(bid, days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as c:
        total = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND created_at>?"), (bid, cutoff))["cnt"]
        flagged = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND created_at>?"), (bid, cutoff))["cnt"]
        acked = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND acknowledged=1 AND created_at>?"), (bid, cutoff))["cnt"]
        top = _fetchone(c, _q("SELECT category,COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND created_at>? GROUP BY category ORDER BY cnt DESC LIMIT 1"), (bid, cutoff))
        return {"total_messages":total,"flagged_issues":flagged,"acknowledged":acked,"top_category":top["category"] if top else "none"}


# --- Website scraping ---
def scrape_website_info(url):
    if not url: return ""
    try:
        import urllib.request
        if not url.startswith("http"): url = "https://" + url
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Hotline Bot)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="ignore")[:10000]
        # Extract text from common meta tags and body
        info_parts = []
        for tag in ["description","og:description"]:
            m = re.search(rf'<meta[^>]*(?:name|property)="{tag}"[^>]*content="([^"]*)"', html, re.I)
            if m: info_parts.append(m.group(1))
        title_m = re.search(r"<title>([^<]*)</title>", html, re.I)
        if title_m: info_parts.insert(0, title_m.group(1).strip())
        # Try to find address/hours patterns
        for pattern in [r'(\d+\s+\w+\s+(?:St|Ave|Blvd|Rd|Dr|Ln|Way|Ct)[^<]{0,60})', r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*[-\u2013]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))']:
            matches = re.findall(pattern, html, re.I)
            info_parts.extend(matches[:2])
        result = " | ".join(info_parts)[:500]
        logger.info(f"Scraped website info: {result[:100]}")
        return result
    except Exception as e:
        logger.error(f"Website scrape failed for {url}: {e}")
        return ""


# --- SMS / Twilio ---
_twilio_client = None
_twilio_from = ""

SHARED_NUMBER = "+18888235592"   # Single shared Twilio number

def init_sms():
    global _twilio_client, _twilio_from
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    _twilio_from = SHARED_NUMBER   # Always use shared number
    if sid and token:
        from twilio.rest import Client; _twilio_client = Client(sid, token); logger.info("Twilio ready")
    else: logger.warning("Twilio not configured")

def send_sms(to, body, from_number=""):
    sender = from_number or _twilio_from
    if not _twilio_client: logger.info(f"[DRY-RUN] {sender} -> {to}: {body}"); return True
    try: _twilio_client.messages.create(body=body, from_=sender, to=to); return True
    except Exception as e: logger.error(f"SMS failed to {to}: {e}"); return False

# buy_twilio_number removed — single shared number model


# --- Email (SendGrid) ---
SENDGRID_KEY = (os.getenv("SENDGRID_API_KEY") or "").strip()
DIGEST_FROM_EMAIL = os.getenv("DIGEST_FROM_EMAIL", "Connect@HotlineTXT.com")

def send_email(to_email, subject, html_body):
    if not SENDGRID_KEY: logger.info(f"[DRY-RUN] Email to {to_email}: {subject}"); return True
    try:
        import urllib.request
        data = json.dumps({"personalizations":[{"to":[{"email":to_email}]}],"from":{"email":DIGEST_FROM_EMAIL,"name":"Hotline"},"subject":subject,"content":[{"type":"text/html","value":html_body}]}).encode()
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=data, headers={"Authorization":f"Bearer {SENDGRID_KEY}","Content-Type":"application/json"}, method="POST")
        urllib.request.urlopen(req); return True
    except Exception as e: logger.error(f"Email failed: {e}"); return False


# --- AI Classifier ---
_ai_client = None  # Stores API key string; HTTP calls used directly

CLASSIFICATION_PROMPT = """You are a business issue classifier for an SMS alert system called Hotline. Analyze customer messages and return structured JSON.

TIER DEFINITIONS:
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, flooding, gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, water damage in progress (burst pipe, overflowing toilet/sink). Flooding IS always Tier 1 (slip hazards, electrical risk, property damage).
  NOT Tier 1: Figurative language. "fire her", "dumpster fire", "killing it", "blowing up", "on fire today", "she got fired" — these are complaints or compliments, never emergencies.
- Tier 2: Business-Critical (Orange Alert) — Operations broken, customers being lost right now. Equipment failures (broken machines, payment systems down, gates stuck, pumps not working), no staff present, supply outages (no toilet paper, soap, napkins), extreme wait times (20+ min, threatening to leave), access blocked (can't get in door), health/hygiene issues (disgusting bathroom, unsanitary).
- Tier 3: Reputation Risk (Yellow) — Customer unhappy, no operational failure. Rude staff, music too loud, temperature complaints, general disappointment, "never coming back."
- Tier 4: Routine (Gray) — No action needed. Positive feedback, compliments, general questions (hours, location, menu), neutral messages.

Categories: cleanliness, staffing, equipment, wait_time, safety, supply, access, payment, inquiry, other
- "access" = customer cannot enter the business (locked door, blocked entry, no one answering)
- "equipment" = machinery broken/jammed (washer, dryer, carwash bay, arcade machine, gas pump, parking gate, ATM, payment reader, kiosk)
- "payment" = payment processing issues (card reader down, payment jam, coins stuck, online system down)
- "inquiry" = any question about the business (hours, directions, menu, policies, parking, accessibility)
- "supply" = out of something (toilet paper, soap, napkins, cups, fuel)
- "safety" = anything involving physical danger (Tier 1)

AUTO-REPLY TONE:
- Tier 1: Urgent, direct. ALWAYS start with "Thank you for alerting us." Then tell customer to call 911. NEVER say "we've contacted emergency services."
- Tier 2: Professional, serious. ALWAYS start with "Thank you for reporting this." Confirm issue type, say management notified. No exclamation marks. NEVER promise specific action.
- Tier 3: Empathetic. ALWAYS start with "Thank you for reaching out." Acknowledge frustration. Invite more details. No exclamation marks.
- Tier 4 positive: Warm, friendly. ALWAYS start with "Thank you!" Genuine appreciation, use exclamation marks.
- Tier 4 inquiry: ALWAYS start with "Thank you for contacting us." NEVER answer factual questions (hours, address, menu, prices, directions). If vague or needs clarification, ask follow-up. Forward to management.

FOLLOW-UP QUESTIONS (ask for clarity on):
- Vague issues: "Which [machine/bathroom/area/location]?"
- Unclear descriptions: "Can you tell us more about what's happening?"
- Timing: "Is this still happening?"
- Multi-location: "Which unit/location/station are you at?"
- Technical: "What's the specific error message?"
- When to ask: Tier 3 (reputation), Tier 4 inquiry (vague), only if TRULY unclear.
- When NOT to ask: Tier 1 (emergency), Tier 2 clear issues (management knows), Tier 4 positive.

HARD RULES:
- NEVER fabricate business information.
- NEVER promise action will be taken. Business decides. You acknowledge and forward.
- NEVER claim to have contacted emergency services.
- NEVER ask follow-up questions for Tier 1 or 2 if issue is clear. Just acknowledge and notify.
- Keep auto_reply under 160 characters.
- Vary responses naturally. Don't repeat same template.
- ALWAYS thank customer first in every response.

EDGE CASES — ACCESS (all Tier 2, category "access"):
- "The door is locked" = Tier 2. Customer cannot enter = business is losing them right now.
- "Door is locked", "locked door", "can't get in", "can't get inside", "door won't open" = Tier 2.
- "Nobody answered", "no one at the door" = Tier 2 (access blocked).
- Any message where a customer cannot physically enter or access the business = Tier 2.

EDGE CASES — EQUIPMENT & PAYMENT (all Tier 2):
- "Washer #3 is broken" = Tier 2, equipment.
- "Carwash bay won't take my card" = Tier 2, payment.
- "Arcade machine is stuck" = Tier 2, equipment.
- "Gas pump 2 is showing an error" = Tier 2, payment/equipment.
- "Parking gate is jammed" = Tier 2, equipment.
- "Payment system is down" = Tier 2, payment.
- "Kiosk won't read my license" = Tier 2, equipment.
- Any equipment failure, payment failure, or machinery jam = Tier 2 (customers cannot complete transactions).

OTHER EDGE CASES:
- "Music is too loud" = Tier 3 (preference, not operational). Acknowledge, don't promise change.
- "What time do you close?" = Tier 4, inquiry. Don't answer. Forward.
- "You should fire her" = Tier 3, staffing. Employment complaint, NOT emergency.
- "Bathroom is flooding!" = Tier 1, safety. Always emergency.
- "Out of toilet paper" = Tier 2, supply.
- "The dryer isn't heating" = Tier 2, equipment (revenue loss per unit).
- "Coins are jammed in the machine" = Tier 2, payment (customer loses money, business loses revenue).
- "I dropped my food/drink/item" = Tier 4. Customer accident, not a business issue.

{website_context}

Respond ONLY with JSON: {{"tier":<int>,"category":"<str>","sentiment":"<str>","confidence":<float>,"summary":"<str>","auto_reply":"<str>"}}"""

def init_classifier():
    global _ai_client
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        _ai_client = key   # Store key directly; calls use raw HTTP (no SDK)
        logger.info("Anthropic API key loaded")
    else: logger.warning("No ANTHROPIC_API_KEY")

def _anthropic_http(system_prompt, user_msg, model="claude-haiku-4-5-20251001", max_tokens=300):
    """Call Anthropic Messages API directly via HTTP — no SDK, no vendor conflicts."""
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}]
    }).encode()
    req = _urllib_req.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": _ai_client,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST"
    )
    with _urllib_req.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data["content"][0]["text"].strip()


def classify_message(text, website_info=""):
    ctx = f"Business website info (use ONLY for answering basic questions like hours/address): {website_info}" if website_info else "No business website info available. Do NOT guess answers to customer questions."
    prompt = CLASSIFICATION_PROMPT.replace("{website_context}", ctx)
    if _ai_client:
        try:
            raw = _anthropic_http(prompt, f'Classify this customer SMS:\n\n"{text}"')
            if raw.startswith("```"): raw = raw.split("\n",1)[1].rsplit("```",1)[0].strip()
            r = json.loads(raw)
            r["tier"] = max(1,min(4,int(r.get("tier",4))))
            r["confidence"] = max(0.0,min(1.0,float(r.get("confidence",0.5))))
            for k,v in [("category","other"),("sentiment","neutral"),("summary",text[:50]),("auto_reply","Thanks for reaching out. We've received your message.")]:
                r.setdefault(k,v)
            return r
        except Exception as e: logger.error(f"AI classify failed: {e}")
    return _classify_fallback(text)

def _classify_fallback(text):
    import re as _re
    t = text.lower()
    # Strip punctuation for reliable word matching (e.g. "fire!" still matches "fire")
    t_clean = _re.sub(r"[^a-z0-9 ]", " ", t)
    # Check for figurative "fire" (fire her, fire him, dumpster fire, etc)
    fire_is_literal = "fire" in t_clean and not any(p in t_clean for p in ["fire her","fire him","fire them","fire that","fire the ","fire this","dumpster fire","on fire with","on fire today","fired","crossfire","campfire","open fire on","gunfire"])
    emergency = ["emergency","injury","hurt","bleeding","attack","weapon","gun","violence","ambulance","911",
                 "collapsed","unconscious","not breathing","heart attack","seizure","overdose","stabbed","shot",
                 "flood","flooding","gas leak","smoke","sparks","electrical","water leak","burst pipe"]
    if fire_is_literal: emergency.append("fire")
    # Use word-boundary matching on cleaned text so punctuation never blocks a match
    if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in emergency):
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.8,"summary":"Possible emergency reported",
                "auto_reply":"If this is an emergency, please call 911 immediately. We have notified the business owner."}
    question_words = ["what time","when do","where is","where are","do you have","is there","how do i","how much","can i","are you open"]
    if any(w in t for w in question_words) or t.endswith("?"):
        return {"tier":4,"category":"inquiry","sentiment":"neutral","confidence":0.7,"summary":"Customer inquiry",
                "auto_reply":"Great question! We've forwarded this to management and someone will get back to you shortly."}
    crit = {"cleanliness":(["dirty","disgusting","filthy","mess","bathroom","gross","unsanitary"],
            "We've flagged this as a cleanliness issue and notified management. Thank you for letting us know."),
        "access":(["door is locked","locked door","can't get in","cant get in","door won't open","door wont open","locked","nobody answered","no one at the door","can't enter","cant enter"],
            "We've notified management that the entrance is inaccessible. We apologize for the inconvenience."),
        "staffing":(["no one","nobody","empty","no staff","where is everyone","closed"],
            "We've flagged this as a staffing issue and notified management. We apologize for the inconvenience."),
        "equipment":(["broken","machine","not working","out of order","malfunction"],
            "We've flagged this as an equipment issue and notified management. Thank you for letting us know."),
        "supply":(["out of","no more","need more","empty dispenser","toilet paper","soap","napkins","paper towels"],
            "We've noted the supply issue and notified management. Thank you for letting us know."),
        "wait_time":(["waited","waiting","slow","forever","leaving","20 minutes","30 minutes"],
            "We're sorry about the wait. Management has been notified about the delay.")}
    for cat,(words,reply) in crit.items():
        if any(w in t for w in words):
            return {"tier":2,"category":cat,"sentiment":"negative","confidence":0.85,
                    "summary":f"{cat.replace('_',' ').title()} issue reported","auto_reply":reply}
    neg = ["bad","terrible","awful","rude","worst","hate","angry","disappointed","unhappy","never coming back","too loud","too cold","too hot"]
    if any(w in t for w in neg):
        return {"tier":3,"category":"other","sentiment":"negative","confidence":0.6,"summary":"Unhappy customer feedback",
                "auto_reply":"We're sorry to hear that. If you're willing to share more details, it helps us make it right."}
    return {"tier":4,"category":"other","sentiment":"neutral","confidence":0.5,"summary":"General message received",
            "auto_reply":"Thanks for reaching out. We've received your message."}


# --- Owner commands ---
# Context and reply-mode are stored in DB so they survive server restarts (Vercel serverless)

def set_context(bid, mid):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET owner_context=? WHERE id=?"), (str(mid), bid))

def get_context(bid):
    with get_db() as c:
        row = _fetchone(c, _q("SELECT owner_context FROM businesses WHERE id=?"), (bid,))
        try: return int(row["owner_context"]) if row else 0
        except: return 0

def set_reply_mode(bid, mid):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET owner_reply_mode=? WHERE id=?"), (str(mid), bid))

def clear_reply_mode(bid):
    with get_db() as c: _execute(c, _q("UPDATE businesses SET owner_reply_mode='0' WHERE id=?"), (bid,))

def get_reply_mode(bid):
    with get_db() as c:
        row = _fetchone(c, _q("SELECT owner_reply_mode FROM businesses WHERE id=?"), (bid,))
        try: v = int(row["owner_reply_mode"]) if row else 0; return v if v else 0
        except: return 0

# US area code -> IANA timezone (covers continental US + AK/HI; multi-tz states
# default to the dominant zone for that area code). Used to display alert times
# in the owner's local time without asking them to configure anything.
_AREA_CODE_TZ = {
    # Eastern
    "201":"America/New_York","202":"America/New_York","203":"America/New_York","207":"America/New_York",
    "212":"America/New_York","215":"America/New_York","216":"America/New_York","217":"America/Chicago",
    "218":"America/Chicago","219":"America/Chicago","220":"America/New_York","223":"America/New_York",
    "224":"America/Chicago","225":"America/Chicago","227":"America/New_York","228":"America/Chicago",
    "229":"America/New_York","231":"America/New_York","234":"America/New_York","239":"America/New_York",
    "240":"America/New_York","248":"America/New_York","251":"America/Chicago","252":"America/New_York",
    "253":"America/Los_Angeles","254":"America/Chicago","256":"America/Chicago","260":"America/New_York",
    "262":"America/Chicago","267":"America/New_York","269":"America/New_York","270":"America/Chicago",
    "272":"America/New_York","276":"America/New_York","281":"America/Chicago","283":"America/New_York",
    "301":"America/New_York","302":"America/New_York","303":"America/Denver","304":"America/New_York",
    "305":"America/New_York","307":"America/Denver","308":"America/Chicago","309":"America/Chicago",
    "310":"America/Los_Angeles","312":"America/Chicago","313":"America/New_York","314":"America/Chicago",
    "315":"America/New_York","316":"America/Chicago","317":"America/New_York","318":"America/Chicago",
    "319":"America/Chicago","320":"America/Chicago","321":"America/New_York","323":"America/Los_Angeles",
    "325":"America/Chicago","330":"America/New_York","331":"America/Chicago","334":"America/Chicago",
    "336":"America/New_York","337":"America/Chicago","339":"America/New_York","346":"America/Chicago",
    "347":"America/New_York","351":"America/New_York","352":"America/New_York","360":"America/Los_Angeles",
    "361":"America/Chicago","364":"America/Chicago","380":"America/New_York","385":"America/Denver",
    "386":"America/New_York","401":"America/New_York","402":"America/Chicago","404":"America/New_York",
    "405":"America/Chicago","406":"America/Denver","407":"America/New_York","408":"America/Los_Angeles",
    "409":"America/Chicago","410":"America/New_York","412":"America/New_York","413":"America/New_York",
    "414":"America/Chicago","415":"America/Los_Angeles","417":"America/Chicago","419":"America/New_York",
    "423":"America/New_York","424":"America/Los_Angeles","425":"America/Los_Angeles","430":"America/Chicago",
    "432":"America/Chicago","434":"America/New_York","435":"America/Denver","440":"America/New_York",
    "442":"America/Los_Angeles","443":"America/New_York","458":"America/Los_Angeles","463":"America/New_York",
    "464":"America/Chicago","469":"America/Chicago","470":"America/New_York","475":"America/New_York",
    "478":"America/New_York","479":"America/Chicago","480":"America/Phoenix","484":"America/New_York",
    "501":"America/Chicago","502":"America/New_York","503":"America/Los_Angeles","504":"America/Chicago",
    "505":"America/Denver","507":"America/Chicago","508":"America/New_York","509":"America/Los_Angeles",
    "510":"America/Los_Angeles","512":"America/Chicago","513":"America/New_York","515":"America/Chicago",
    "516":"America/New_York","517":"America/New_York","518":"America/New_York","520":"America/Phoenix",
    "530":"America/Los_Angeles","531":"America/Chicago","534":"America/Chicago","539":"America/Chicago",
    "540":"America/New_York","541":"America/Los_Angeles","551":"America/New_York","557":"America/Chicago",
    "559":"America/Los_Angeles","561":"America/New_York","562":"America/Los_Angeles","563":"America/Chicago",
    "564":"America/Los_Angeles","567":"America/New_York","570":"America/New_York","571":"America/New_York",
    "573":"America/Chicago","574":"America/New_York","575":"America/Denver","580":"America/Chicago",
    "585":"America/New_York","586":"America/New_York","601":"America/Chicago","602":"America/Phoenix",
    "603":"America/New_York","605":"America/Chicago","606":"America/New_York","607":"America/New_York",
    "608":"America/Chicago","609":"America/New_York","610":"America/New_York","612":"America/Chicago",
    "614":"America/New_York","615":"America/Chicago","616":"America/New_York","617":"America/New_York",
    "618":"America/Chicago","619":"America/Los_Angeles","620":"America/Chicago","623":"America/Phoenix",
    "626":"America/Los_Angeles","628":"America/Los_Angeles","629":"America/Chicago","630":"America/Chicago",
    "631":"America/New_York","636":"America/Chicago","640":"America/New_York","641":"America/Chicago",
    "646":"America/New_York","650":"America/Los_Angeles","651":"America/Chicago","657":"America/Los_Angeles",
    "660":"America/Chicago","661":"America/Los_Angeles","662":"America/Chicago","667":"America/New_York",
    "669":"America/Los_Angeles","678":"America/New_York","680":"America/New_York","681":"America/New_York",
    "682":"America/Chicago","684":"Pacific/Pago_Pago","689":"America/New_York","701":"America/Chicago",
    "702":"America/Los_Angeles","703":"America/New_York","704":"America/New_York","706":"America/New_York",
    "707":"America/Los_Angeles","708":"America/Chicago","712":"America/Chicago","713":"America/Chicago",
    "714":"America/Los_Angeles","715":"America/Chicago","716":"America/New_York","717":"America/New_York",
    "718":"America/New_York","719":"America/Denver","720":"America/Denver","724":"America/New_York",
    "725":"America/Los_Angeles","726":"America/Chicago","727":"America/New_York","731":"America/Chicago",
    "732":"America/New_York","734":"America/New_York","737":"America/Chicago","740":"America/New_York",
    "743":"America/New_York","747":"America/Los_Angeles","754":"America/New_York","757":"America/New_York",
    "758":"America/Port_of_Spain","760":"America/Los_Angeles","762":"America/New_York","763":"America/Chicago",
    "765":"America/New_York","769":"America/Chicago","770":"America/New_York","771":"America/New_York",
    "772":"America/New_York","773":"America/Chicago","774":"America/New_York","775":"America/Los_Angeles",
    "779":"America/Chicago","781":"America/New_York","785":"America/Chicago","786":"America/New_York",
    "801":"America/Denver","802":"America/New_York","803":"America/New_York","804":"America/New_York",
    "805":"America/Los_Angeles","806":"America/Chicago","808":"Pacific/Honolulu","810":"America/New_York",
    "812":"America/New_York","813":"America/New_York","814":"America/New_York","815":"America/Chicago",
    "816":"America/Chicago","817":"America/Chicago","818":"America/Los_Angeles","820":"America/Los_Angeles",
    "828":"America/New_York","830":"America/Chicago","831":"America/Los_Angeles","832":"America/Chicago",
    "835":"America/New_York","838":"America/New_York","843":"America/New_York","845":"America/New_York",
    "847":"America/Chicago","848":"America/New_York","850":"America/Chicago","854":"America/New_York",
    "856":"America/New_York","857":"America/New_York","858":"America/Los_Angeles","859":"America/New_York",
    "860":"America/New_York","862":"America/New_York","863":"America/New_York","864":"America/New_York",
    "865":"America/New_York","870":"America/Chicago","872":"America/Chicago","878":"America/New_York",
    "901":"America/Chicago","903":"America/Chicago","904":"America/New_York","906":"America/New_York",
    "907":"America/Anchorage","908":"America/New_York","909":"America/Los_Angeles","910":"America/New_York",
    "912":"America/New_York","913":"America/Chicago","914":"America/New_York","915":"America/Denver",
    "916":"America/Los_Angeles","917":"America/New_York","918":"America/Chicago","919":"America/New_York",
    "920":"America/Chicago","925":"America/Los_Angeles","928":"America/Phoenix","929":"America/New_York",
    "930":"America/New_York","931":"America/Chicago","934":"America/New_York","936":"America/Chicago",
    "937":"America/New_York","938":"America/Chicago","940":"America/Chicago","941":"America/New_York",
    "947":"America/New_York","949":"America/Los_Angeles","951":"America/Los_Angeles","952":"America/Chicago",
    "954":"America/New_York","956":"America/Chicago","959":"America/New_York","970":"America/Denver",
    "971":"America/Los_Angeles","972":"America/Chicago","973":"America/New_York","975":"America/Chicago",
    "978":"America/New_York","979":"America/Chicago","980":"America/New_York","984":"America/New_York",
    "985":"America/Chicago","986":"America/Denver","989":"America/New_York",
}

# Single-timezone US states (used as a stronger signal than area code when present)
_STATE_TZ = {
    "AL":"America/Chicago","AR":"America/Chicago","CA":"America/Los_Angeles","CO":"America/Denver",
    "CT":"America/New_York","DC":"America/New_York","DE":"America/New_York","GA":"America/New_York",
    "HI":"Pacific/Honolulu","IA":"America/Chicago","IL":"America/Chicago","LA":"America/Chicago",
    "MA":"America/New_York","MD":"America/New_York","ME":"America/New_York","MN":"America/Chicago",
    "MO":"America/Chicago","MS":"America/Chicago","MT":"America/Denver","NC":"America/New_York",
    "NH":"America/New_York","NJ":"America/New_York","NM":"America/Denver","NV":"America/Los_Angeles",
    "NY":"America/New_York","OH":"America/New_York","OK":"America/Chicago","PA":"America/New_York",
    "RI":"America/New_York","SC":"America/New_York","UT":"America/Denver","VA":"America/New_York",
    "VT":"America/New_York","WA":"America/Los_Angeles","WI":"America/Chicago","WV":"America/New_York",
    "WY":"America/Denver",
    # AZ uses Phoenix (no DST except Navajo Nation — accept this approximation)
    "AZ":"America/Phoenix",
}

def _tz_for_business(business):
    """Pick an IANA tz for a business. State (if known) wins over area code.
    Falls back to env DEFAULT_TZ, then UTC."""
    if not business: return os.getenv("DEFAULT_TZ","UTC")
    state = (business.get("state") or "").upper().strip()
    if state in _STATE_TZ: return _STATE_TZ[state]
    phone = business.get("owner_phone") or ""
    digits = re.sub(r"\D","",phone)
    if len(digits)==11 and digits[0]=="1": digits = digits[1:]
    if len(digits)>=10:
        ac = digits[:3]
        if ac in _AREA_CODE_TZ: return _AREA_CODE_TZ[ac]
    return os.getenv("DEFAULT_TZ","UTC")

def _fmt_ts(iso, business=None):
    """Format a stored UTC iso timestamp in the business's local tz.
    Falls back to raw iso slice on any parse error."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        tz_name = _tz_for_business(business)
        if ZoneInfo is not None:
            try: dt = dt.astimezone(ZoneInfo(tz_name))
            except Exception: dt = dt.astimezone(timezone.utc)
        return dt.strftime("%b %d %I:%M%p").replace(" 0"," ")
    except Exception:
        return iso[:16] if iso else ""

def _fmt_phone_short(phone):
    d = _normalize_phone(phone).replace("+","")
    if len(d)==11 and d[0]=="1": d=d[1:]
    if len(d)==10: return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return phone

def handle_owner_command(text, business, sender_phone=""):
    bid = business["id"]
    raw = text.strip()
    cmd = raw.upper()

    # Words that should be interpreted as commands even when we're in reply mode
    # (so the owner can't accidentally text "STATUS" to the customer).
    RESERVED = {
        "NEVERMIND","CANCEL","CLOSE","DONE","WRAP","FINISH","END",
        "MENU","HELP","?",
        "STATUS","ALERTS","TIER2","TIER3","ALERTS CRITICAL","ALERTS ALL",
        "PAUSE","RESUME","BILLING","DIGEST DAILY","DIGEST WEEKLY","REPLY",
    }

    # ── Reply mode (persisted in DB, survives restarts) ───────────────────────
    reply_mid = get_reply_mode(bid)
    if reply_mid:
        if cmd in {"NEVERMIND","CANCEL"}:
            clear_reply_mode(bid)
            return "Reply cancelled."
        if cmd in {"CLOSE","DONE","WRAP","FINISH","END"}:
            msg = get_message_by_id(reply_mid)
            clear_reply_mode(bid)
            if msg: end_conversation(bid, msg["from_number"])
            return "Conversation closed. AI auto-replies resumed."
        # If owner types another reserved command, fall through to handle it
        # instead of texting that word to the customer.
        if cmd in RESERVED:
            clear_reply_mode(bid)
            # fall through below
        else:
            clear_reply_mode(bid)
            msg = get_message_by_id(reply_mid)
            if msg:
                send_sms(msg["from_number"], raw)
                mark_owner_replied(bid, msg["from_number"])
                logger.info(f"[OWNER REPLY] biz={bid} msg_id={reply_mid} to={msg['from_number']}")
                return f"Reply sent. AI quiet for {CONVERSATION_WINDOW_MIN}min.\nType CLOSE when done, or just let it time out."
            return "Could not find the original message."

    if cmd == "BILLING":
        status = business.get("sub_status") or "trialing"
        days = trial_days_left(business)
        PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "")
        if status == "active":
            return "\u2705 Subscription active. Reply BILLING CANCEL to cancel."
        elif status == "trialing":
            link_part = f"\nPay here when ready: {PAYMENT_LINK}" if PAYMENT_LINK else ""
            return f"Trial active \u2014 {days} day(s) left.{link_part}"
        else:
            link_part = f"\n{PAYMENT_LINK}" if PAYMENT_LINK else "\nEmail Connect@HotlineTXT.com to reactivate."
            return f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers \u26a0\ufe0f{link_part}"

    # MENU (and ? shortcut). Note: HELP is intercepted by Twilio at the carrier
    # level for 10DLC compliance, so we use MENU as the in-app command.
    if cmd in ("MENU", "?", "HELP"):
        return ("Commands:\n"
                "REPLY \u2014 Reply to last customer\n"
                "CLOSE \u2014 End active conversation\n"
                "STATUS \u2014 Alert status + level\n"
                "ALERTS \u2014 Change alert level\n"
                "TIER2 \u2014 Critical only\n"
                "TIER3 \u2014 Add reputation alerts\n"
                "PAUSE / RESUME\n"
                "BILLING \u2014 Subscription\n"
                "MENU \u2014 This message")

    if cmd == "REPLY":
        recent = get_recent_all(bid, 1)
        msg = recent[0] if recent else None
        if not msg: return "No messages to reply to."
        set_reply_mode(bid, msg["id"])
        logger.info(f"[REPLY MODE] biz={bid} target_msg_id={msg['id']} text={msg['message_text'][:40]!r}")
        return (f"Replying to: \"{msg['message_text'][:60]}\"\n"
                f"Type your reply now, or NEVERMIND.\n"
                f"Type CLOSE when finished to close the line with customer.")

    if cmd == "STATUS":
        name = business.get("name") or bid
        if business.get("paused"): return f"\U0001f4f4 Alerts PAUSED for {name}.\nReply RESUME to turn back on."
        t3_label = "Tier 2 + Tier 3 (all)" if business.get("alert_tier3") else "Tier 2 critical only"
        return f"\U0001f514 Alerts ON for {name}.\nAlert level: {t3_label}\nReply ALERTS to change."

    if cmd == "ALERTS":
        t3 = "on (Tier 2 + Tier 3)" if business.get("alert_tier3") else "off (Tier 2 critical only)"
        return (f"\U0001f514 Alert level: {t3}\n\n"
                "Reply TIER2 \u2014 Critical issues only\n"
                "Reply TIER3 \u2014 Also get reputation/feedback alerts")
    if cmd in ("TIER2", "ALERTS CRITICAL"): set_alert_tier3(bid, False); return "\U0001f534 Critical only. You'll get Tier 2 (operations, equipment, staffing) and emergencies.\nReply TIER3 to also get reputation alerts."
    if cmd in ("TIER3", "ALERTS ALL"): set_alert_tier3(bid, True); return "\U0001f7e1 All alerts on. You'll now also get Tier 3 reputation/feedback messages.\nReply TIER2 to go back to critical only."

    if cmd == "PAUSE": set_paused(bid, True); return "\U0001f4f4 Alerts PAUSED. Reply RESUME to turn back on."
    if cmd == "RESUME": set_paused(bid, False); return "\U0001f514 Alerts resumed."
    if cmd == "DIGEST DAILY": set_digest_freq(bid, "daily"); return "\U0001f4e7 Digest set to daily."
    if cmd == "DIGEST WEEKLY": set_digest_freq(bid, "weekly"); return "\U0001f4e7 Digest set to weekly."

    if cmd == "DEBUG":
        # Diagnostic: show which biz the owner is tied to and the last 3 messages
        # stored under that biz_id. Lets us see when a customer message is being
        # routed to a different business row than the owner expects.
        recent = get_recent_all(bid, 3)
        lines = [f"biz_id: {bid}", f"name: {business.get('name','')}", f"code: {business.get('business_code','')}", f"messages: {len(recent)}"]
        for m in recent:
            lines.append(f"#{m['id']} tier={m['tier']} from={m['from_number'][-4:]} \"{m['message_text'][:25]}\" raw={m['created_at'][:19]}")
        return "\n".join(lines)

    if any(cmd.startswith(w) for w in ["EMPHASIZED","QUESTIONED","LAUGHED AT","DISLIKED","LIKED","LOVED","THUMBED UP"]): return ""
    return f"Unknown: \"{raw[:20]}\"\nReply MENU for commands."



# --- Digest ---
def build_digest_html(name, stats, period="week"):
    t,f,a = stats["total_messages"],stats["flagged_issues"],stats["acknowledged"]
    tc = stats["top_category"].replace("_"," "); u = f - a
    return f"""<div style="font-family:system-ui;max-width:480px;margin:0 auto;padding:24px">
<h1 style="font-size:20px;margin:0 0 4px">{name}</h1><p style="color:#888;font-size:14px;margin:0 0 24px">Hotline {period}ly digest</p>
<div style="display:flex;gap:12px;margin-bottom:24px">
<div style="flex:1;background:#f5f5f0;padding:16px;border-radius:10px;text-align:center"><div style="font-size:28px;font-weight:700">{t}</div><div style="font-size:12px;color:#888">messages</div></div>
<div style="flex:1;background:#fff4e6;padding:16px;border-radius:10px;text-align:center"><div style="font-size:28px;font-weight:700">{f}</div><div style="font-size:12px;color:#888">flagged</div></div>
<div style="flex:1;background:#e8f5e9;padding:16px;border-radius:10px;text-align:center"><div style="font-size:28px;font-weight:700">{a}</div><div style="font-size:12px;color:#888">acknowledged</div></div></div>
{"<p style='color:#c0392b;font-size:14px'>\u26a0\ufe0f "+str(u)+" unacknowledged</p>" if u>0 else ""}
{"<p style='font-size:14px'>Top category: <strong>"+tc+"</strong></p>" if f>0 else ""}
<p style="font-size:13px;color:#aaa;margin-top:24px">Reply HELP to your Hotline number for commands.</p></div>"""

def send_all_digests(force_freq=None):
    sent = 0
    for biz in get_all_businesses():
        freq = force_freq or biz.get("digest_freq") or "weekly"
        email = biz.get("email","")
        if not email: continue
        days = 1 if freq=="daily" else 7
        stats = get_stats(biz["id"], days=days)
        period = "dai" if freq=="daily" else "week"
        if send_email(email, f"Hotline {period}ly digest for {biz.get('name','')}", build_digest_html(biz.get("name",""), stats, period)): sent += 1
    return sent


# --- FastAPI ---
app = FastAPI(title="Hotline", version="3.0.0")

RATE_LIMIT_MAX = 5; RATE_LIMIT_WINDOW = 10; _initialized = False
_ENV_OWNER = os.getenv("OWNER_PHONE_NUMBER",""); _ENV_TWILIO = os.getenv("TWILIO_PHONE_NUMBER","")
_ENV_NAME = os.getenv("BUSINESS_NAME","MyBusiness"); _ADMIN_KEY = os.getenv("ADMIN_KEY","changeme")

# ── Admin security ────────────────────────────────────────────────────────────
_COOKIE_NAME = "htadmin"
_COOKIE_MAX_AGE = 60 * 60 * 8          # 8-hour session
_LOGIN_FAIL_WINDOW = 15 * 60           # 15 minutes
_LOGIN_FAIL_MAX = 5
_login_attempts: dict = {}             # ip -> [timestamp, ...]

def _warn_weak_key():
    if _ADMIN_KEY in ("changeme", "", "admin", "password", "hotline"):
        logger.warning("⚠️  ADMIN_KEY is set to a weak/default value — set a strong secret in your env vars before going live")

def _sign_cookie(payload: str) -> str:
    """HMAC-SHA256 sign a payload; return payload.signature"""
    sig = hmac.new(_ADMIN_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verify_cookie(token: str) -> bool:
    """Return True if token signature is valid and not expired."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_ADMIN_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        issued_at = int(payload.split(":")[1])
        return (_time.time() - issued_at) < _COOKIE_MAX_AGE
    except Exception:
        return False

def _check_login_rate(ip: str) -> bool:
    """Return True if this IP is allowed to attempt login."""
    now = _time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_FAIL_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_FAIL_MAX

def _record_login_fail(ip: str):
    _login_attempts.setdefault(ip, []).append(_time.time())

def _get_admin_session(request) -> bool:
    """Return True if request carries a valid admin session cookie."""
    token = request.cookies.get(_COOKIE_NAME, "")
    return bool(token) and _verify_cookie(token)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_init():
    global _initialized
    if _initialized: return
    init_db(); init_classifier(); init_sms()
    _warn_weak_key()
    if _ENV_OWNER:
        # Always upsert — env vars are the source of truth
        try:
            with get_db() as c:
                existing = _fetchone(c, "SELECT id FROM businesses WHERE id='default'")
            if not existing:
                code = create_business("default", _ENV_NAME, _ENV_OWNER, "")
                backfill_business_codes()
                logger.info(f"Registered '{_ENV_NAME}' owner={_ENV_OWNER} code={code}")
            else:
                with get_db() as c:
                    _execute(c, _q("UPDATE businesses SET name=?, owner_phone=? WHERE id='default'"),
                             (_ENV_NAME, _ENV_OWNER))
                logger.info(f"Synced '{_ENV_NAME}' owner={_ENV_OWNER}")
        except Exception as e:
            logger.error(f"_ensure_init upsert failed: {e}")
    # Backfill any businesses missing a code
    try: backfill_business_codes()
    except Exception as e: logger.warning(f"backfill_business_codes: {e}")
    _initialized = True

WELCOME_MSG = """Welcome to {name} on Hotline! \U0001f4f2

Your sign + QR code links are on the way in a separate text.
Customers scan the QR to send you private feedback.

Quick commands:
OK \u2014 Close an alert
REPLY \u2014 Respond to a customer
SNOOZE \u2014 Revisit in 1 hour
QUIET 2H \u2014 Silence alerts
DETAILS \u2014 Full alert info
STATUS \u2014 Your current settings
HELP \u2014 Full command list

Emergencies always get through."""


# --- Routes ---
@app.get("/")
def root():
    _ensure_init(); return Response(content=DEMO_HTML, media_type="text/html")

@app.get("/health")
def health(): _ensure_init(); return {"status":"ok"}

@app.get("/debug/env")
def debug_env():
    """Show which env vars are set (values masked). Critical for diagnosing missing config."""
    _ensure_init()
    def _mask(v): return (v[:4]+"…"+v[-2:]) if v and len(v)>6 else ("SET" if v else "MISSING")
    all_biz = get_all_businesses()
    return {
        "TWILIO_ACCOUNT_SID": _mask(os.getenv("TWILIO_ACCOUNT_SID","")),
        "TWILIO_AUTH_TOKEN": _mask(os.getenv("TWILIO_AUTH_TOKEN","")),
        "TWILIO_PHONE_NUMBER": os.getenv("TWILIO_PHONE_NUMBER","MISSING"),
        "OWNER_PHONE_NUMBER": os.getenv("OWNER_PHONE_NUMBER","MISSING"),
        "ANTHROPIC_API_KEY": _mask(os.getenv("ANTHROPIC_API_KEY","")),
        "BUSINESS_NAME": os.getenv("BUSINESS_NAME","MISSING"),
        "twilio_client_ready": _twilio_client is not None,
        "ai_client_ready": _ai_client is not None,
        "registered_businesses": [{"id":b["id"],"name":b["name"],"twilio_number":b["twilio_number"],"owner_phone":b["owner_phone"]} for b in all_biz],
    }

@app.get("/debug/sms")
def debug_sms(from_num:str=Query("+15550001111"), body:str=Query("Bathroom is disgusting"), to_num:str=Query("")):
    """Simulate an incoming SMS without Twilio. Useful for live testing."""
    _ensure_init()
    if not to_num:
        bizzes = get_all_businesses()
        to_num = bizzes[0]["twilio_number"] if bizzes else ""
    import asyncio
    result = asyncio.run(incoming_sms(From=from_num, Body=body, To=to_num))
    # Parse TwiML response to show plain text
    content = result.body.decode() if hasattr(result, "body") else str(result)
    import re as _re
    msg_match = _re.search(r"<Message>(.*?)</Message>", content, _re.DOTALL)
    auto_reply = msg_match.group(1) if msg_match else content
    return {"from": from_num, "to": to_num, "body": body, "auto_reply_sent": auto_reply, "twiml": content}

@app.get("/debug/db")
def debug_db():
    import traceback
    result = {"database_url_set": bool(DATABASE_URL), "use_postgres": USE_POSTGRES, "tables": [], "error": None}
    try:
        _ensure_init()
        with get_db() as c:
            if USE_POSTGRES:
                rows = _fetchall(c, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
                result["tables"] = [r["tablename"] for r in rows]
            else:
                rows = _fetchall(c, "SELECT name FROM sqlite_master WHERE type='table'")
                result["tables"] = [r["name"] for r in rows]
            pending = _fetchall(c, "SELECT COUNT(*) as cnt FROM pending_signups")
            result["pending_signups_count"] = pending[0]["cnt"] if pending else 0
    except Exception as e:
        result["error"] = traceback.format_exc()
    return result

@app.post("/digest")
def digest_endpoint(freq: str = Query("weekly")): _ensure_init(); return {"digests_sent": send_all_digests(force_freq=freq)}

# ── Admin login ───────────────────────────────────────────────────────────────
_LOGIN_PAGE = '''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline Admin</title></head>
<body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f8f8f6">
<div style="text-align:center;width:320px">
  <h2 style="font-size:22px;margin:0 0 8px">Hotline Admin</h2>
  <p style="font-size:13px;color:#888;margin:0 0 24px">hotlinetxt.com</p>
  <div id="err" style="display:none;background:#fef2f2;border:1px solid #fca5a5;color:#dc2626;font-size:13px;padding:8px 12px;border-radius:6px;margin-bottom:16px"></div>
  <form id="f" style="display:flex;flex-direction:column;gap:10px">
    <input id="k" type="password" placeholder="Admin key" autocomplete="current-password"
      style="padding:10px 14px;border:1px solid #ddd;border-radius:6px;font-size:15px;width:100%;box-sizing:border-box">
    <button type="submit"
      style="padding:10px 20px;background:#ea580c;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer">Sign in</button>
  </form>
</div>
<script>
document.getElementById("f").addEventListener("submit",async function(e){
  e.preventDefault();
  const k=document.getElementById("k").value;
  const r=await fetch("/admin/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:k})});
  if(r.ok){location.href="/admin";}
  else{const d=await r.json();const el=document.getElementById("err");el.textContent=d.error||"Invalid key";el.style.display="block";}
});
</script>
</body></html>'''

@app.post("/admin/login")
async def admin_login(request: Request):
    _ensure_init()
    req = request
    ip = req.client.host if req.client else "unknown"
    if not _check_login_rate(ip):
        return JSONResponse({"error": "Too many attempts — try again in 15 minutes"}, status_code=429)
    body = await req.json()
    key = body.get("key", "")
    if not hmac.compare_digest(key, _ADMIN_KEY):
        _record_login_fail(ip)
        logger.warning(f"[ADMIN] Failed login attempt from {ip}")
        return JSONResponse({"error": "Invalid key"}, status_code=401)
    # Issue signed session cookie
    payload = f"admin:{int(_time.time())}"
    token = _sign_cookie(payload)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True, samesite="strict", secure=False)
    logger.info(f"[ADMIN] Login success from {ip}")
    return resp

@app.get("/admin/logout")
def admin_logout():
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp

# ── Admin API routes (POST, cookie-auth) ─────────────────────────────────────
@app.post("/admin/add")
async def admin_add(request: Request):
    _ensure_init()
    if not _get_admin_session(request): return {"error": "Unauthorized"}, 401
    body = await request.json()
    name = body.get("name","").strip(); owner = body.get("owner","").strip()
    twilio = body.get("twilio","").strip(); biz_id = body.get("biz_id","").strip()
    extra_phones = body.get("extra_phones",""); email = body.get("email",""); website = body.get("website","")
    if not owner.startswith("+") or not twilio.startswith("+"): return {"error":"Phone numbers must start with +"}
    if not biz_id: biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    ok = create_business(biz_id,name,owner,twilio,extra_phones=extra_phones,email=email,website_url=website)
    if not ok: return {"error":"Already exists or number in use"}
    msg = WELCOME_MSG.format(name=name,twilio=twilio)
    for p in get_alert_phones({"owner_phone":owner,"alert_phones":f"{owner},{extra_phones}" if extra_phones else owner}): send_sms(p,msg)
    return {"success":True,"business_id":biz_id,"name":name}

@app.post("/admin/welcome")
async def admin_welcome(request: Request):
    _ensure_init()
    if not _get_admin_session(request): return {"error": "Unauthorized"}, 401
    body = await request.json()
    biz_id = body.get("biz_id","")
    with get_db() as c: biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    if not biz: return {"error":"Not found"}
    phones = get_alert_phones(biz)
    # 1. Welcome + commands
    msg = WELCOME_MSG.format(name=biz["name"],twilio=biz["twilio_number"])
    for p in phones: send_sms(p, msg)
    # 2. Asset links (sign PDF + QR)
    code = biz.get("business_code","")
    if code:
        base = os.getenv("BASE_URL", "https://hotlinetxt.com")
        asset_msg = (
            f"Your Hotline assets for {biz['name']}:\n"
            f"Print-ready sign: {base}/signs/{code}.pdf\n"
            f"Plain QR image (custom signage): {base}/qr/{code}.png"
        )
        for p in phones: send_sms(p, asset_msg)
    # 3. Preference prompt
    pref_prompt = (
        "One quick setup \u2014 what alerts do you want?\n\n"
        "Reply TIER2 \u2014 Critical only (equipment failures, no staff, safety issues)\n"
        "Reply TIER3 \u2014 Everything including complaints & feedback\n\n"
        "You can change this anytime by texting ALERTS."
    )
    for p in phones: send_sms(p, pref_prompt)
    return {"success":True}

@app.get("/admin/list")
def admin_list(request: Request):
    _ensure_init()
    if not _get_admin_session(request): return {"error": "Unauthorized"}, 401
    return {"businesses":[{"id":b["id"],"name":b["name"],"owner":b["owner_phone"],"twilio":b["twilio_number"]} for b in get_all_businesses()]}

@app.post("/admin/update-phones")
async def admin_update_phones(request: Request):
    _ensure_init()
    if not _get_admin_session(request): return {"error": "Unauthorized"}, 401
    body = await request.json()
    biz_id = body.get("biz_id","").strip()
    phones_str = body.get("phones","").strip()
    if not biz_id: return {"error":"biz_id required"}
    # Validate phones (comma-separated, each must start with +)
    phones = [p.strip() for p in phones_str.split(",") if p.strip()]
    for p in phones:
        if not p.startswith("+"): return {"error":f"Invalid phone: {p}. All phones must start with +"}, 400
    normalized = ",".join(phones)
    with get_db() as c:
        _execute(c, _q("UPDATE businesses SET alert_phones=? WHERE id=?"), (normalized, biz_id))
    logger.info(f"[ADMIN] Updated alert phones for {biz_id}: {normalized}")
    return {"success":True, "alert_phones": normalized}

@app.post("/admin/remove")
async def admin_remove(request: Request):
    _ensure_init()
    if not _get_admin_session(request): return {"error": "Unauthorized"}, 401
    body = await request.json()
    biz_id = body.get("biz_id","")
    if not biz_id: return {"error":"biz_id required"}
    with get_db() as c: _execute(c,_q("DELETE FROM businesses WHERE id=?"), (biz_id,))
    logger.info(f"[ADMIN] Removed business {biz_id}")
    return {"success":True}

@app.post("/admin/billing")
async def admin_billing(request: Request):
    """Set sub_status, extend trial, or credit months."""
    _ensure_init()
    if not _get_admin_session(request): return JSONResponse({"error":"Unauthorized"}, status_code=401)
    body = await request.json()
    biz_id = body.get("biz_id","").strip()
    action = body.get("action","").strip()  # set_status | extend_trial | credit_months | send_billing_sms
    if not biz_id: return {"error":"biz_id required"}
    with get_db() as c:
        biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    if not biz: return {"error":"Not found"}

    if action == "set_status":
        status = body.get("status","").strip()
        allowed = ("trialing","active","past_due","expired","canceled","comped")
        if status not in allowed: return {"error":f"status must be one of {allowed}"}
        with get_db() as c:
            _execute(c, _q("UPDATE businesses SET sub_status=? WHERE id=?"), (status, biz_id))
        logger.info(f"[ADMIN BILLING] {biz_id} status => {status}")
        return {"success":True, "status":status}

    elif action == "extend_trial":
        days = int(body.get("days", 7))
        if days < 1 or days > 365: return {"error":"days must be 1-365"}
        current = (biz.get("trial_ends_at") or "").strip()
        try:
            base_dt = datetime.fromisoformat(current) if current else datetime.now(timezone.utc)
            # If already expired, extend from now
            if base_dt < datetime.now(timezone.utc):
                base_dt = datetime.now(timezone.utc)
        except Exception:
            base_dt = datetime.now(timezone.utc)
        new_end = (base_dt + timedelta(days=days)).isoformat()
        with get_db() as c:
            _execute(c, _q("UPDATE businesses SET trial_ends_at=?, sub_status='trialing' WHERE id=?"), (new_end, biz_id))
        logger.info(f"[ADMIN BILLING] {biz_id} trial extended +{days}d => {new_end[:10]}")
        return {"success":True, "trial_ends_at": new_end[:10], "days_added": days}

    elif action == "credit_months":
        months = int(body.get("months", 1))
        if months < 1 or months > 24: return {"error":"months must be 1-24"}
        days = months * 30
        current = (biz.get("trial_ends_at") or "").strip()
        try:
            base_dt = datetime.fromisoformat(current) if current else datetime.now(timezone.utc)
            if base_dt < datetime.now(timezone.utc):
                base_dt = datetime.now(timezone.utc)
        except Exception:
            base_dt = datetime.now(timezone.utc)
        new_end = (base_dt + timedelta(days=days)).isoformat()
        with get_db() as c:
            _execute(c, _q("UPDATE businesses SET trial_ends_at=?, sub_status='trialing' WHERE id=?"), (new_end, biz_id))
        logger.info(f"[ADMIN BILLING] {biz_id} credited {months} month(s) => {new_end[:10]}")
        return {"success":True, "trial_ends_at": new_end[:10], "months_credited": months}

    elif action == "send_billing_sms":
        PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK","")
        link_part = f"\n{PAYMENT_LINK}" if PAYMENT_LINK else ""
        status = biz.get("sub_status","trialing")
        days = trial_days_left(biz)
        if status == "active":
            msg = "\u2705 Your Hotline subscription is active."
        elif status in ("expired","canceled","past_due"):
            msg = f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers \u26a0\ufe0f{link_part}"
        else:
            msg = f"\u23f0 Your Hotline trial has {days} day(s) left.{link_part}"
        phones = get_alert_phones(biz)
        for p in phones: send_sms(p, msg)
        logger.info(f"[ADMIN BILLING] Billing SMS sent to {biz_id}")
        return {"success":True, "sms_sent_to": phones}

    return {"error":"Unknown action"}


@app.get("/admin")
def admin_ui(request: Request):
    _ensure_init()
    if not _get_admin_session(request):
        return Response(content=_LOGIN_PAGE, media_type="text/html")

    # --- Pending signups table removed — product is live! ---
    # Previously showed pending_signups; no longer needed.

    # --- Active businesses table ---
    STATUS_BADGE = {
        "trialing": ("<span style='background:#dbeafe;color:#1d4ed8;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>TRIAL</span>", "trialing"),
        "active":   ("<span style='background:#dcfce7;color:#166534;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>ACTIVE</span>", "active"),
        "past_due": ("<span style='background:#fef9c3;color:#854d0e;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>PAST DUE</span>", "past_due"),
        "expired":  ("<span style='background:#fee2e2;color:#991b1b;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>EXPIRED</span>", "expired"),
        "canceled": ("<span style='background:#f3f4f6;color:#6b7280;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>CANCELED</span>", "canceled"),
        "comped":   ("<span style='background:#f3e8ff;color:#6b21a8;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px'>COMPED</span>", "comped"),
    }
    businesses = get_all_businesses()
    rows = ""
    for b in businesses:
        s = get_stats(b["id"])
        bid = b["id"]
        bstatus = (b.get("sub_status") or "trialing")
        badge_html, _ = STATUS_BADGE.get(bstatus, (bstatus, bstatus))
        days = trial_days_left(b)
        trial_info = f"<br><span style='font-size:11px;color:#888'>{days}d left</span>" if bstatus == "trialing" else ""
        trial_end_val = (b.get("trial_ends_at") or "")[:10]
        alert_phones_str = b.get("alert_phones") or ""
        alert_phones_display = alert_phones_str if alert_phones_str else b.get("owner_phone","")
        rows += (
            f'<tr id="row-{bid}">'
            f'<td style="padding:12px 16px;font-weight:600"><a href="#" onclick="openDrawer(\'{bid}\',\'{b["name"].replace(chr(39), "")}\');return false" style="color:#1a1a1a;text-decoration:none;border-bottom:1px solid #e0e0dc">{b["name"]}</a><br><span style="font-size:11px;color:#2563eb;cursor:pointer;text-decoration:underline" onclick="editPhones(\'{bid}\',\'{alert_phones_display.replace("\"","&quot;")}\');">{alert_phones_display}</span></td>'
            f'<td style="padding:12px 16px;font-family:monospace;font-size:13px;color:#ea580c;font-weight:600">{b.get("business_code","—")}</td>'
            f'<td style="padding:12px 16px;text-align:center">{s["total_messages"]}</td>'
            f'<td style="padding:12px 16px;text-align:center">{s["flagged_issues"]}</td>'
            f'<td style="padding:12px 16px">{badge_html}{trial_info}</td>'
            f'<td style="padding:12px 16px;white-space:nowrap">'
            f'<a href="#" onclick="adminResend(\'{bid}\');return false" style="color:#2563eb;font-size:12px;margin-right:10px">Resend</a>'
            f'<a href="#" onclick="openBilling(\'{bid}\',\'{b["name"]}\',\'{bstatus}\',\'{trial_end_val}\');return false" style="color:#7c3aed;font-size:12px;margin-right:10px">Billing</a>'
            f'<a href="#" onclick="adminRemove(\'{bid}\',\'{b["name"]}\');return false" style="color:#dc2626;font-size:12px">Remove</a>'
            f'</td></tr>'
        )
    if not rows: rows = '<tr><td colspan="6" style="padding:24px;text-align:center;color:#999">No businesses yet.</td></tr>'

    # --- Metrics ---
    now_utc    = datetime.now(timezone.utc)
    cutoff_30  = (now_utc - timedelta(days=30)).isoformat()
    cutoff_7   = (now_utc - timedelta(days=7)).isoformat()
    all_biz    = businesses

    active_count   = sum(1 for b in all_biz if (b.get("sub_status") or "trialing") == "active")
    trialing_count = sum(1 for b in all_biz if (b.get("sub_status") or "trialing") == "trialing")
    churned_count  = sum(1 for b in all_biz if (b.get("sub_status") or "") in ("canceled", "expired"))
    total_biz      = len(all_biz)
    mrr            = round(active_count * 19.99, 2)
    new_30d        = sum(1 for b in all_biz if (b.get("created_at") or "") >= cutoff_30)

    churn_denom  = active_count + churned_count
    churn_rate   = f"{round(churned_count / churn_denom * 100)}%" if churn_denom else "—"
    conv_denom   = active_count + churned_count + trialing_count
    conv_rate    = f"{round(active_count / conv_denom * 100)}%" if conv_denom else "—"

    with get_db() as c:
        msg_7d      = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE created_at>?"), (cutoff_7,))["cnt"]
        tier1_7d    = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE tier=1 AND created_at>?"), (cutoff_7,))["cnt"]
        top_cat_row = _fetchone(c, _q("SELECT category, COUNT(*) as cnt FROM messages WHERE created_at>? AND tier IN (1,2) GROUP BY category ORDER BY cnt DESC LIMIT 1"), (cutoff_30,))
        top_cat     = top_cat_row["category"].replace("_"," ").title() if top_cat_row else "—"

    def stat_card(label, value, sub="", color="#1a1a1a"):
        sub_html = f'<div style="font-size:11px;color:#aaa;margin-top:4px">{sub}</div>' if sub else ""
        return (f'<div style="background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 20px;min-width:110px;flex:1">'
                f'<div style="font-size:11px;text-transform:uppercase;color:#aaa;letter-spacing:.05em;margin-bottom:6px">{label}</div>'
                f'<div style="font-size:24px;font-weight:700;color:{color};line-height:1">{value}</div>'
                f'{sub_html}</div>')

    metrics_html = f'''
  <h2 style="font-size:16px;font-weight:700;margin:0 0 12px">Overview</h2>
  <div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:28px">
    {stat_card("MRR", f"${mrr:,.2f}", f"{active_count} active subs", "#166534")}
    {stat_card("Total", total_biz, f"{new_30d} new last 30d")}
    {stat_card("Trialing", trialing_count, "in free trial")}
    {stat_card("Churned", churned_count, f"of {churn_denom} ever paid" if churn_denom else "", "#991b1b" if churned_count else "#1a1a1a")}
    {stat_card("Trial → Paid", conv_rate, "conversion rate", "#ea580c")}
    {stat_card("Churn Rate", churn_rate, "paid who left", "#991b1b" if churned_count else "#1a1a1a")}
    {stat_card("Messages (7d)", msg_7d, f"{tier1_7d} emergencies")}
    {stat_card("Top Issue (30d)", top_cat)}
  </div>'''

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline Admin</title>
<style>
#drawer{{position:fixed;top:0;right:-480px;width:480px;max-width:100vw;height:100vh;background:#fff;border-left:1px solid #e0e0dc;box-shadow:-4px 0 24px rgba(0,0,0,0.08);transition:right 0.25s ease;z-index:200;overflow-y:auto;padding:24px}}
#drawer.open{{right:0}}
#drawer-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.25);z-index:199}}
#drawer-overlay.open{{display:block}}
.tier-1{{background:#fee2e2;color:#991b1b;font-size:11px;padding:2px 7px;border-radius:4px;font-weight:700}}
.tier-2{{background:#fef9c3;color:#854d0e;font-size:11px;padding:2px 7px;border-radius:4px;font-weight:700}}
.tier-3{{background:#f5f5f0;color:#888;font-size:11px;padding:2px 7px;border-radius:4px}}
.tier-4{{background:#f5f5f0;color:#aaa;font-size:11px;padding:2px 7px;border-radius:4px}}
.msg-row{{padding:10px 0;border-bottom:1px solid #f0f0ec;font-size:13px;line-height:1.4}}
.msg-row:last-child{{border-bottom:none}}
</style>
</head>
<body style="font-family:system-ui;margin:0;padding:24px;background:#f8f8f6">
<div style="max-width:960px;margin:0 auto">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:28px">
    <h1 style="font-size:24px;margin:0">Hotline Admin</h1>
    <a href="/admin/logout" style="font-size:13px;color:#888;text-decoration:none">Sign out</a>
  </div>
  <div id="toast" style="display:none;background:#166534;color:#fff;font-size:13px;padding:8px 14px;border-radius:6px;margin-bottom:16px"></div>
  {metrics_html}
  <h2 style="font-size:16px;font-weight:700;margin:0 0 12px">Businesses</h2>
  <div style="background:#fff;border:1px solid #e0e0dc;border-radius:10px;overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <thead><tr style="background:#f5f5f0;border-bottom:1px solid #e0e0dc">
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Business</th>
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Code</th>
        <th style="padding:10px 16px;text-align:center;font-size:12px;text-transform:uppercase;color:#888">Msgs</th>
        <th style="padding:10px 16px;text-align:center;font-size:12px;text-transform:uppercase;color:#888">Flagged</th>
        <th style="padding:10px 16px;font-size:12px;text-transform:uppercase;color:#888">Status</th>
        <th style="padding:10px 16px;font-size:12px;text-transform:uppercase;color:#888">Actions</th>
      </tr></thead>
      <tbody id="biz-tbody">{rows}</tbody>
    </table>
  </div>
</div>

<!-- Customer drill-down drawer -->
<div id="drawer-overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
    <h2 id="drawer-title" style="font-size:17px;font-weight:700;margin:0"></h2>
    <a href="#" onclick="closeDrawer();return false" style="color:#aaa;font-size:22px;text-decoration:none;line-height:1">&times;</a>
  </div>
  <div id="drawer-body" style="color:#444;font-size:14px">Loading...</div>
</div>

<!-- Billing Modal -->
<div id="billing-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:300;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:12px;padding:28px;width:360px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.15)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h3 id="bm-title" style="margin:0;font-size:16px"></h3>
      <a href="#" onclick="closeBilling();return false" style="color:#888;font-size:20px;text-decoration:none">&times;</a>
    </div>
    <div style="margin-bottom:16px">
      <label style="font-size:12px;font-weight:600;color:#888;text-transform:uppercase">Set Status</label>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
        <button onclick="setBillingStatus('trialing')" style="font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid #bfdbfe;background:#dbeafe;color:#1d4ed8;cursor:pointer">Trial</button>
        <button onclick="setBillingStatus('active')" style="font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid #bbf7d0;background:#dcfce7;color:#166534;cursor:pointer">Active</button>
        <button onclick="setBillingStatus('comped')" style="font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid #e9d5ff;background:#f3e8ff;color:#6b21a8;cursor:pointer">Comped</button>
        <button onclick="setBillingStatus('expired')" style="font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid #fecaca;background:#fee2e2;color:#991b1b;cursor:pointer">Expired</button>
        <button onclick="setBillingStatus('canceled')" style="font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid #e5e7eb;background:#f3f4f6;color:#6b7280;cursor:pointer">Canceled</button>
      </div>
    </div>
    <div style="border-top:1px solid #f0f0ec;padding-top:16px;margin-bottom:16px">
      <label style="font-size:12px;font-weight:600;color:#888;text-transform:uppercase">Extend Trial</label>
      <div style="display:flex;gap:8px;margin-top:8px">
        <input id="bm-days" type="number" min="1" max="365" value="7" style="width:70px;padding:6px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px">
        <span style="line-height:32px;font-size:13px;color:#888">days</span>
        <button onclick="doExtendTrial()" style="flex:1;padding:6px 12px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer">Extend</button>
      </div>
    </div>
    <div style="border-top:1px solid #f0f0ec;padding-top:16px;margin-bottom:16px">
      <label style="font-size:12px;font-weight:600;color:#888;text-transform:uppercase">Credit Months</label>
      <div style="display:flex;gap:8px;margin-top:8px">
        <input id="bm-months" type="number" min="1" max="24" value="1" style="width:70px;padding:6px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px">
        <span style="line-height:32px;font-size:13px;color:#888">months</span>
        <button onclick="doCreditMonths()" style="flex:1;padding:6px 12px;background:#7c3aed;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer">Credit</button>
      </div>
      <p style="font-size:11px;color:#aaa;margin:6px 0 0">1 month = 30 days added to trial window.</p>
    </div>
    <div style="border-top:1px solid #f0f0ec;padding-top:16px">
      <button onclick="doSendBillingSms()" style="width:100%;padding:8px;background:#f5f5f0;color:#333;border:1px solid #e0e0dc;border-radius:6px;font-size:13px;cursor:pointer">📱 Send Billing SMS to Owner</button>
    </div>
  </div>
</div>
<script>
var _bmBizId="",_bmName="";
function toast(msg,ok){{var el=document.getElementById("toast");el.textContent=msg;el.style.background=ok?"#166534":"#991b1b";el.style.display="block";setTimeout(()=>el.style.display="none",3000);}}
async function adminPost(path,body){{
  const r=await fetch(path,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(body)}});
  if(r.status===401){{location.href="/admin";return null;}}
  return r.json();
}}
async function adminResend(bizId){{
  const d=await adminPost("/admin/welcome",{{biz_id:bizId}});
  if(d&&d.success)toast("Welcome SMS resent",true);
  else toast((d&&d.error)||"Failed",false);
}}
async function adminRemove(bizId,name){{
  if(!confirm("Remove "+name+"? This cannot be undone."))return;
  const d=await adminPost("/admin/remove",{{biz_id:bizId}});
  if(d&&d.success){{document.getElementById("row-"+bizId).remove();toast("Removed "+name,true);}}
  else toast((d&&d.error)||"Failed",false);
}}
function openBilling(bizId,name,status,trialEnd){{
  _bmBizId=bizId; _bmName=name;
  document.getElementById("bm-title").textContent="Billing: "+name;
  document.getElementById("billing-modal").style.display="flex";
}}
function closeBilling(){{document.getElementById("billing-modal").style.display="none";}}
async function setBillingStatus(status){{
  const d=await adminPost("/admin/billing",{{biz_id:_bmBizId,action:"set_status",status:status}});
  if(d&&d.success){{toast("Status → "+status,true);setTimeout(()=>location.reload(),800);}}
  else toast((d&&d.error)||"Failed",false);
}}
async function doExtendTrial(){{
  const days=parseInt(document.getElementById("bm-days").value);
  const d=await adminPost("/admin/billing",{{biz_id:_bmBizId,action:"extend_trial",days:days}});
  if(d&&d.success){{toast("Trial extended +"+days+"d (ends "+d.trial_ends_at+")",true);setTimeout(()=>location.reload(),1200);}}
  else toast((d&&d.error)||"Failed",false);
}}
async function doCreditMonths(){{
  const months=parseInt(document.getElementById("bm-months").value);
  const d=await adminPost("/admin/billing",{{biz_id:_bmBizId,action:"credit_months",months:months}});
  if(d&&d.success){{toast("Credited "+months+" month(s), ends "+d.trial_ends_at,true);setTimeout(()=>location.reload(),1200);}}
  else toast((d&&d.error)||"Failed",false);
}}
async function doSendBillingSms(){{
  const d=await adminPost("/admin/billing",{{biz_id:_bmBizId,action:"send_billing_sms"}});
  if(d&&d.success)toast("Billing SMS sent ✓",true);
  else toast((d&&d.error)||"Failed",false);
}}
async function editPhones(bizId,currentPhones){{
  const newPhones=prompt("Enter alert phone numbers (comma-separated, with country codes):\\n\\nExample: +12075551234, +12075555678",currentPhones);
  if(newPhones===null)return;
  const d=await adminPost("/admin/update-phones",{{biz_id:bizId,phones:newPhones}});
  if(d&&d.success){{toast("Alert phones updated ✓",true);setTimeout(()=>location.reload(),800);}}
  else toast((d&&d.error)||"Failed",false);
}}
function closeDrawer(){{
  document.getElementById("drawer").classList.remove("open");
  document.getElementById("drawer-overlay").classList.remove("open");
}}
async function openDrawer(bizId, bizName){{
  document.getElementById("drawer-title").textContent=bizName;
  document.getElementById("drawer-body").innerHTML="<div style='color:#aaa;padding:40px 0;text-align:center'>Loading...</div>";
  document.getElementById("drawer").classList.add("open");
  document.getElementById("drawer-overlay").classList.add("open");
  try{{
    const r=await fetch("/admin/business/"+encodeURIComponent(bizId));
    if(!r.ok){{document.getElementById("drawer-body").innerHTML="<p style='color:#dc2626'>Failed to load.</p>";return;}}
    const d=await r.json();
    const b=d.business; const s=d.stats; const msgs=d.messages;
    const loc=b.city&&b.state?b.city+", "+b.state:b.zip||"—";
    const signed=b.created_at?b.created_at.slice(0,10):"—";
    const days_ago=b.created_at?Math.floor((Date.now()-new Date(b.created_at))/86400000)+" days ago":"";
    const ack_pct=s.flagged_all>0?Math.round(s.acked_all/s.flagged_all*100)+"%":"—";
    const tier_badge=(t)=>{{
      if(t==1)return"<span class='tier-1'>T1 Emergency</span>";
      if(t==2)return"<span class='tier-2'>T2 Critical</span>";
      if(t==3)return"<span class='tier-3'>T3 Reputation</span>";
      return"<span class='tier-4'>T4 Routine</span>";
    }};
    const msg_rows=msgs.map(m=>`<div class="msg-row">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px">
        <span style="color:#444">${{m.message_text.slice(0,120)}}${{m.message_text.length>120?"…":""}}</span>
        ${{tier_badge(m.tier)}}
      </div>
      <div style="color:#aaa;font-size:11px">${{m.category||"—"}} &middot; ${{m.sentiment||"—"}} &middot; ${{(m.created_at||"").slice(0,16).replace("T"," ")}} ${{m.acknowledged?"✓ acked":""}}</div>
    </div>`).join("")||"<p style='color:#aaa;font-size:13px'>No messages yet.</p>";
    document.getElementById("drawer-body").innerHTML=`
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:20px">
        <div style="background:#f8f8f6;border-radius:8px;padding:12px">
          <div style="font-size:11px;text-transform:uppercase;color:#aaa;margin-bottom:8px">Business info</div>
          <table style="font-size:13px;width:100%;border-collapse:collapse">
            <tr><td style="color:#aaa;padding:3px 0;white-space:nowrap;padding-right:10px">Code</td><td style="font-weight:600;color:#ea580c">${{b.business_code||"—"}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Phone</td><td>${{b.owner_phone||"—"}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Email</td><td>${{b.email||"—"}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Location</td><td>${{loc}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Website</td><td>${{b.website_url?`<a href="${{b.website_url}}" target="_blank" style="color:#2563eb">${{b.website_url.replace(/^https?:\\/\\//,"")}}</a>`:"—"}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Signed up</td><td>${{signed}} <span style="color:#aaa">(${{days_ago}})</span></td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Alert tier</td><td>${{b.alert_tier3?"Tier 3 (all)":"Tier 2 (critical)"}}</td></tr>
          </table>
        </div>
        <div style="background:#f8f8f6;border-radius:8px;padding:12px">
          <div style="font-size:11px;text-transform:uppercase;color:#aaa;margin-bottom:8px">Usage</div>
          <table style="font-size:13px;width:100%;border-collapse:collapse">
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">All msgs</td><td style="font-weight:600">${{s.total_all}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Last 30d</td><td>${{s.total_30d}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Last 7d</td><td>${{s.total_7d}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">T1 emergencies</td><td style="color:#991b1b;font-weight:600">${{s.tier1_all}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">T2 critical</td><td style="color:#854d0e">${{s.tier2_all}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Ack rate</td><td>${{ack_pct}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Top category</td><td>${{s.top_category||"—"}}</td></tr>
            <tr><td style="color:#aaa;padding:3px 0;padding-right:10px">Last message</td><td style="font-size:12px">${{s.last_msg_at?s.last_msg_at.slice(0,16).replace("T"," "):"never"}}</td></tr>
          </table>
        </div>
      </div>
      <div style="font-size:13px;font-weight:600;color:#444;margin-bottom:8px">Last 10 messages</div>
      <div>${{msg_rows}}</div>`;
  }}catch(e){{document.getElementById("drawer-body").innerHTML="<p style='color:#dc2626'>Error: "+e.message+"</p>";}}
}}
document.getElementById("billing-modal").addEventListener("click",function(e){{if(e.target===this)closeBilling();}});
</script>
</body></html>'''
    return Response(content=html, media_type="text/html")


@app.get("/admin/business/{biz_id}")
def admin_business_detail(biz_id: str, request: Request):
    _ensure_init()
    if not _get_admin_session(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    with get_db() as c:
        biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    if not biz:
        return JSONResponse({"error": "Not found"}, status_code=404)
    now_utc   = datetime.now(timezone.utc)
    cutoff_30 = (now_utc - timedelta(days=30)).isoformat()
    cutoff_7  = (now_utc - timedelta(days=7)).isoformat()
    with get_db() as c:
        total_all  = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=?"), (biz_id,))["cnt"]
        total_30d  = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND created_at>?"), (biz_id, cutoff_30))["cnt"]
        total_7d   = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND created_at>?"), (biz_id, cutoff_7))["cnt"]
        tier1_all  = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier=1"), (biz_id,))["cnt"]
        tier2_all  = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier=2"), (biz_id,))["cnt"]
        flagged_all= _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2)"), (biz_id,))["cnt"]
        acked_all  = _fetchone(c, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND acknowledged=1"), (biz_id,))["cnt"]
        top_row    = _fetchone(c, _q("SELECT category, COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) GROUP BY category ORDER BY cnt DESC LIMIT 1"), (biz_id,))
        last_msg   = _fetchone(c, _q("SELECT created_at FROM messages WHERE business_id=? ORDER BY created_at DESC LIMIT 1"), (biz_id,))
        msgs       = _fetchall(c, _q("SELECT message_text, tier, category, sentiment, acknowledged, created_at FROM messages WHERE business_id=? ORDER BY created_at DESC LIMIT 10"), (biz_id,))
    stats = {
        "total_all": total_all, "total_30d": total_30d, "total_7d": total_7d,
        "tier1_all": tier1_all, "tier2_all": tier2_all,
        "flagged_all": flagged_all, "acked_all": acked_all,
        "top_category": top_row["category"].replace("_"," ").title() if top_row else None,
        "last_msg_at": last_msg["created_at"] if last_msg else None,
    }
    return JSONResponse({"business": dict(biz), "stats": stats, "messages": msgs})


# --- SMS Incoming ---


# ─── Asset generation: QR PNG + Sign PDF ─────────────────────────────────────

def _make_qr_pil(url: str, size_px: int = 1000):
    """Return a PIL Image of a plain white-background QR code."""
    qr = qrcode.QRCode(
        error_correction=ERROR_CORRECT_H,
        box_size=10, border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size_px, size_px), PILImage.LANCZOS)


def _sms_deep_link(business_code: str, business_name: str = "") -> str:
    """
    SMS deep link: opens native Messages app with number + routing header prefilled.
    Body format:
        HOTLINE BC4729 | Joe's Coffee
        [Describe the issue and hit send]
    Customer types over the second line. First line is scrubbed before AI sees it.
    """
    import urllib.parse as _up
    number = os.getenv("TWILIO_PHONE_NUMBER", SHARED_NUMBER)
    header = f"HOTLINE {business_code.upper()}"
    if business_name:
        header += f" | {business_name}"
    body = f"{header}\n[Describe the issue and hit send]"
    return f"sms:{number}?body={_up.quote(body)}"


def _make_qr_png_bytes(business_code: str, business_name: str = "") -> bytes:
    """1000×1000 plain white QR PNG — encodes SMS deep link."""
    buf = io.BytesIO()
    _make_qr_pil(_sms_deep_link(business_code, business_name)).save(buf, format="PNG")
    return buf.getvalue()


def _make_sign_pdf_bytes(business_code: str, business_name: str = "") -> bytes:
    """
    Sign PDF matching the Hotline template:
    - Cream/off-white background (#F5F0E8)
    - Orange double border with crop marks
    - Dark bold headline "Something wrong?"
    - Orange bold "Text us:" subhead
    - Orange divider line with center dot
    - Large QR code (SMS deep link — opens native messages app)
    - "Powered by H HOTLINE" + "Visit Hotlinetxt.com" footer
    """
    url = _sms_deep_link(business_code, business_name)

    # Build QR image bytes for ReportLab
    qr_pil = _make_qr_pil(url, size_px=900)
    qr_buf = io.BytesIO()
    qr_pil.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_reader = RLImageReader(qr_buf)

    ORANGE      = rl_colors.HexColor("#D4520A")   # template orange
    CREAM       = rl_colors.HexColor("#F5F0E8")   # template background
    DARK        = rl_colors.HexColor("#1C1C1A")   # near-black headline
    GRAY        = rl_colors.HexColor("#888880")   # footer text

    # Page: 8.5 × 11" (letter) — matches template proportions
    PAGE_W, PAGE_H = 8.5 * 72, 11 * 72
    pdf_buf = io.BytesIO()
    c = rl_canvas.Canvas(pdf_buf, pagesize=(PAGE_W, PAGE_H))

    # ── Cream background ────────────────────────────────────────────────────
    c.setFillColor(CREAM)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # ── Crop marks (small corner ticks outside border) ──────────────────────
    MARGIN = 36       # margin from page edge to outer border
    TICK   = 14       # length of crop mark ticks
    GAP    = 6        # gap between border and tick start
    c.setStrokeColor(rl_colors.HexColor("#CCCCCC"))
    c.setLineWidth(0.5)
    # corners: TL, TR, BR, BL
    corners = [
        (MARGIN, PAGE_H - MARGIN),
        (PAGE_W - MARGIN, PAGE_H - MARGIN),
        (PAGE_W - MARGIN, MARGIN),
        (MARGIN, MARGIN),
    ]
    for (cx, cy) in corners:
        # horizontal tick
        dx = -1 if cx < PAGE_W/2 else 1
        c.line(cx + dx*(GAP), cy, cx + dx*(GAP+TICK), cy)
        # vertical tick
        dy = 1 if cy < PAGE_H/2 else -1
        c.line(cx, cy + dy*(GAP), cx, cy + dy*(GAP+TICK))

    # ── Double orange border ─────────────────────────────────────────────────
    OUTER_PAD = MARGIN           # outer rect inset from page edge
    INNER_PAD = OUTER_PAD + 7   # inner rect (gap between borders)
    RADIUS = 18

    c.setStrokeColor(ORANGE)
    c.setFillColor(CREAM)

    # Outer border
    c.setLineWidth(3)
    c.roundRect(OUTER_PAD, OUTER_PAD,
                PAGE_W - 2*OUTER_PAD, PAGE_H - 2*OUTER_PAD,
                RADIUS, fill=0, stroke=1)
    # Inner border
    c.setLineWidth(1.5)
    c.roundRect(INNER_PAD, INNER_PAD,
                PAGE_W - 2*INNER_PAD, PAGE_H - 2*INNER_PAD,
                RADIUS - 4, fill=0, stroke=1)

    # ── "Something wrong?" headline ──────────────────────────────────────────
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 58)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 148, "Something")
    c.drawCentredString(PAGE_W / 2, PAGE_H - 216, "wrong?")

    # ── "Text us:" in orange ─────────────────────────────────────────────────
    c.setFillColor(ORANGE)
    c.setFont("Helvetica-Bold", 52)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 290, "Text us:")

    # ── Orange divider line with center dot ──────────────────────────────────
    div_y = PAGE_H - 330
    line_x1 = PAGE_W * 0.18
    line_x2 = PAGE_W * 0.82
    c.setStrokeColor(ORANGE)
    c.setLineWidth(1.5)
    mid = PAGE_W / 2
    # left segment
    c.line(line_x1, div_y, mid - 12, div_y)
    # right segment
    c.line(mid + 12, div_y, line_x2, div_y)
    # center dot
    c.setFillColor(ORANGE)
    c.circle(mid, div_y, 5, fill=1, stroke=0)

    # ── QR code (large, centered, with thin orange border box) ───────────────
    qr_size = 240
    qr_x = (PAGE_W - qr_size) / 2
    qr_y = div_y - 20 - qr_size

    # Orange border around QR
    pad = 10
    c.setStrokeColor(ORANGE)
    c.setFillColor(rl_colors.white)
    c.setLineWidth(1.5)
    c.roundRect(qr_x - pad, qr_y - pad,
                qr_size + pad*2, qr_size + pad*2,
                6, fill=1, stroke=1)
    c.drawImage(qr_reader, qr_x, qr_y, width=qr_size, height=qr_size)

    # ── Footer ───────────────────────────────────────────────────────────────
    footer_center_y = INNER_PAD + 34
    wordmark_y      = footer_center_y + 4

    # "Powered by" label
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 11)
    powered_w = c.stringWidth("Powered by ", "Helvetica", 11)

    # H box
    box_s = 18
    total_w = powered_w + box_s + 6 + c.stringWidth("HOTLINE", "Helvetica-Bold", 14)
    start_x = (PAGE_W - total_w) / 2

    c.drawString(start_x, wordmark_y, "Powered by ")
    box_x = start_x + powered_w
    c.setFillColor(ORANGE)
    c.roundRect(box_x, wordmark_y - 2, box_s, box_s, 3, fill=1, stroke=0)
    c.setFillColor(rl_colors.white)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(box_x + box_s/2, wordmark_y + 2, "H")

    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(box_x + box_s + 6, wordmark_y, "HOTLINE")

    # "Visit Hotlinetxt.com for more info"
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 10)
    c.drawCentredString(PAGE_W / 2, footer_center_y - 14, "Visit Hotlinetxt.com for more info")

    c.save()
    return pdf_buf.getvalue()


@app.get("/signs/{business_code}.pdf")
def sign_pdf(business_code: str):
    _ensure_init()
    if not _PDF_LIBS_OK:
        return Response(content="PDF generation unavailable: missing reportlab/qrcode/Pillow", status_code=500)
    code = business_code.upper().strip()
    biz = get_business_by_code(code)
    if not biz:
        return Response(content="Business not found", status_code=404)
    try:
        pdf_bytes = _make_sign_pdf_bytes(code, biz.get("name", ""))
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=hotline-sign-{code}.pdf"}
        )
    except Exception as e:
        logger.error(f"Sign PDF generation failed for {code}: {e}")
        return Response(content="PDF generation error", status_code=500)


@app.get("/qr/{business_code}.png")
def qr_png(business_code: str):
    _ensure_init()
    if not _PDF_LIBS_OK:
        return Response(content="QR generation unavailable: missing qrcode/Pillow", status_code=500)
    code = business_code.upper().strip()
    biz = get_business_by_code(code)
    if not biz:
        return Response(content="Business not found", status_code=404)
    try:
        png_bytes = _make_qr_png_bytes(code, biz.get("name", ""))
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f"inline; filename=hotline-qr-{code}.png"}
        )
    except Exception as e:
        logger.error(f"QR PNG generation failed for {code}: {e}")
        return Response(content="QR generation error", status_code=500)


# ── Customer phone → business mapping (permanent) ────────────────────────────
def _parse_business_code_from_body(body: str):
    """Extract BC#### from message body like 'HOTLINE BC4729 bathroom is dirty'."""
    m = re.search(r"\bBC\d{4}\b", body.upper())
    return m.group(0) if m else None


def _scrub_hotline_header(body: str) -> str:
    """
    Remove the prefilled routing header from a customer message, keeping
    anything the customer actually typed — even if it's on the same line
    as the placeholder (Android behaviour).

    Strips:
      - HOTLINE BC#### | Business Name  (whole line)
      - [Describe the issue and hit send]  as a line OR as a prefix on a line

    Examples:
      "HOTLINE BC4729 | Joe's Coffee\nThe bathroom is disgusting"
          → "The bathroom is disgusting"
      "HOTLINE BC4729 | Joe's Coffee\n[Describe the issue and hit send]"
          → ""  (blank — customer hit send without typing)
      "[Describe the issue and hit send] carwash is broken"
          → "carwash is broken"  (Android inline — placeholder + message same line)
      "HOTLINE BC4729 waited 20 minutes"
          → "waited 20 minutes"  (no newline, inline after code)
    """
    lines = body.splitlines() if "\n" in body else [body]
    cleaned = []

    for line in lines:
        upper = line.upper().strip()

        # Drop/trim lines containing the HOTLINE routing header
        if re.search(r"\bHOTLINE\s+BC\d{4}\b", upper):
            # Keep anything after "HOTLINE BC#### | optional name" on the same line
            remainder = re.sub(r"(?i)HOTLINE\s+BC\d{4}(\s*\|[^\n]*)?\s*", "", line).strip()
            # Strip any trailing placeholder on the same line
            remainder = re.sub(r"(?i)^\[describe[^\]]*\]\s*", "", remainder).strip()
            if remainder:
                cleaned.append(remainder)
            continue

        # Handle [Describe...] placeholder — strip it as a prefix, keep remainder
        placeholder_pat = r"(?i)^\[describe[^\]]*\]\s*"
        if re.match(placeholder_pat, line.strip()):
            remainder = re.sub(placeholder_pat, "", line.strip()).strip()
            if remainder:
                cleaned.append(remainder)   # customer typed after the placeholder
            # else: pure placeholder line, drop it entirely
            continue

        cleaned.append(line)

    return "\n".join(cleaned).strip()

def _process_customer_message(biz, sender, body):
    """Classify + alert for a customer message. Returns the auto-reply text (or empty if suppressed)."""
    website_info = biz.get("website_info", "")
    c = classify_message(body, website_info=website_info)
    msg_id = store_message(biz["id"], sender, body, c)
    tier, conf, summary = c["tier"], c["confidence"], c.get("summary", "Issue reported")
    cat = c.get("category", "other")

    # If the owner has recently replied to this customer, the human is on the
    # line. Don't step on them with an AI message. Alerts still fire.
    convo_active = is_conversation_active(biz["id"], sender)
    if convo_active:
        auto_reply = ""
        logger.info(f"[CONVO ACTIVE] Suppressing auto-reply for {sender} \u2192 {biz['id']} (owner active)")
    else:
        auto_reply = c.get("auto_reply") or "Thanks for reaching out. We've received your message."
    update_auto_reply(msg_id, auto_reply)

    alert_phones = get_alert_phones(biz)
    should_alert_t3 = biz.get("alert_tier3") and tier == 3 and conf > 0.5
    should_alert = tier == 1 or (tier == 2 and conf > 0.7) or should_alert_t3
    paused = bool(biz.get("paused"))
    recent_count = get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW)

    logger.info(f"[CLASSIFY] biz={biz['id']} tier={tier} conf={conf:.2f} cat={cat} summary={summary!r}")

    _trial_ok = can_send_alerts(biz)
    if not _trial_ok and tier != 1:
        logger.info(f"[TRIAL BLOCKED] Alert suppressed for {biz['id']} — trial expired or unpaid")
    elif alert_phones and should_alert and not (paused and tier != 1):
        if recent_count < RATE_LIMIT_MAX:
            # Self-contained alert: everything the owner needs in one message.
            if tier == 1:
                header = "\U0001f6a8 URGENT"
            elif cat == "inquiry":
                header = "\u2753 Customer question"
            elif tier == 2:
                header = "\u26a0\ufe0f Issue"
            else:
                header = "\U0001f4ac Feedback"
            when = _fmt_ts(datetime.now(timezone.utc).isoformat(), biz)
            reply_block = f"We replied:\n{auto_reply}\n\n" if auto_reply else "(AI silent \u2014 conversation active)\n\n"
            alert = (f"{header} ({when})\n"
                     f"Category: {cat}\n"
                     f"Customer:\n{body}\n\n"
                     f"{reply_block}"
                     f"Reply REPLY to message customer back.")
            for p in alert_phones:
                ok = send_sms(p, alert)
                logger.info(f"[ALERT SENT] to={p} ok={ok}")
            mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")
        else:
            logger.warning(f"[RATE LIMITED] {biz['id']} hit {recent_count} alerts in {RATE_LIMIT_WINDOW}min window")

    return auto_reply


@app.post("/sms/incoming")
async def incoming_sms(From:str=Form(...), Body:str=Form(...), To:str=Form("")):
    _ensure_init()
    sender, body = From.strip(), Body.strip()
    logger.info(f"[INCOMING] From={sender} Body={body[:80]!r}")

    # If the message body contains a BC#### code, treat it as a customer
    # message even when the sender is a registered owner. This lets owners
    # test their own hotline from their personal phone without their texts
    # getting captured by the owner-command handler.
    code = _parse_business_code_from_body(body)
    if code:
        biz = get_business_by_code(code)
        if biz:
            clean_body = _scrub_hotline_header(body)
            if not clean_body:
                logger.info(f"[BLANK MSG] {sender} \u2192 {biz['id']} \u2014 awaiting message")
                return _twiml("Got it! Now just describe what's wrong and send it to us.")
            auto_reply = _process_customer_message(biz, sender, clean_body)
            return _twiml(auto_reply)
        else:
            logger.warning(f"[NO BIZ] Received code {code!r} but no matching business")
            return _twiml("Thanks for reaching out. We couldn't find that business code.")

    # 1. Check if sender is a registered owner/alert-phone
    owner_biz = get_business_by_owner(sender)
    if owner_biz:
        logger.info(f"[OWNER CMD] biz={owner_biz['id']} cmd={body!r}")
        resp = handle_owner_command(body, owner_biz, sender_phone=sender)
        if not resp: return _twiml("")
        return _twiml(resp)

    # 2. No BC code and not an owner — unknown sender
    logger.info(f"[NO CODE] no BC code from {sender}")
    return _twiml("")

def _twiml(msg):
    if not msg:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Message>'+msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")+'</Message></Response>', media_type="application/xml")


# --- Shared nav + styles ---
NAV_CSS = """
.nav{display:flex;justify-content:space-between;align-items:center;padding:12px 24px;max-width:100%;margin:0 auto}
.nav .logo{font-size:13px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#ea580c;text-decoration:none;position:absolute;left:50%;transform:translateX(-50%)}
.nav .logo span{background:#ea580c;color:#fff;padding:2px 6px;border-radius:3px;margin-right:4px}
.nav-links{display:flex;gap:20px;align-items:center;margin-left:auto}
.nav-links a{font-size:14px;color:#666;text-decoration:none;font-weight:500}
.nav-links a:hover{color:#1a1a1a}
.nav-links .signup-btn{background:#ea580c;color:#fff;padding:8px 16px;border-radius:6px;font-weight:600}
.nav-links .signup-btn:hover{background:#dc2626;color:#fff}
.hamburger{display:none;cursor:pointer;font-size:22px;color:#666}
@media(max-width:600px){.nav{flex-wrap:wrap;padding:8px 16px}.nav .logo{position:static;transform:none;font-size:11px;flex:0 0 auto}.nav-links{display:none;position:absolute;top:48px;right:16px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:12px;flex-direction:column;gap:10px;box-shadow:0 4px 12px rgba(0,0,0,0.08);z-index:10;margin-left:0}.nav-links.open{display:flex}.hamburger{display:block;margin-left:auto}}
"""

NAV_HTML = """<nav class="nav"><a href="/" class="logo"><span>H</span> HOTLINE</a>
<div class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')">&#9776;</div>
<div class="nav-links"><a href="/">Demo</a><a href="/how-it-works">How It Works</a><a href="/industries">Who We Support</a><a href="/resources">Resources</a><a href="/signup" class="signup-btn">Sign Up</a></div></nav>"""


# --- Demo page (homepage) ---
DEMO_PROMPT = """You are simulating a business's customer feedback SMS system for a live demo called Hotline.

TIER DEFINITIONS:
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, flooding, gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, water damage in progress. Flooding IS always Tier 1 (slip hazards, electrical risk, property damage).
  NOT Tier 1: Figurative language. "fire her", "dumpster fire", "killing it", "blowing up", "on fire today", "she got fired" — complaints or compliments, never emergencies.
- Tier 2: Business-Critical — Operations broken. Equipment failures (broken machines, payment systems down, gates stuck, pumps not working), no staff, supply outages (no toilet paper, soap), extreme waits (20+ min), access blocked (can't get in door), health/hygiene issues.
- Tier 3: Reputation Risk — Customer unhappy, no operational failure. Rude staff, music too loud, temperature, disappointment.
- Tier 4: Routine — Positive feedback, compliments, questions, neutral.

Categories: cleanliness, staffing, equipment, wait_time, safety, supply, access, payment, inquiry, other
- "access" = customer cannot enter the business (locked door, blocked entry, no one answering)
- "equipment" = machinery broken/jammed (washer, dryer, carwash bay, arcade machine, gas pump, parking gate, ATM, payment reader, kiosk)
- "payment" = payment processing issues (card reader down, payment jam, coins stuck, online system down)

AUTO-REPLY TONE:
- Tier 1: Urgent. ALWAYS start with "Thank you for alerting us." Tell customer to call 911 immediately. NEVER say "we've contacted emergency services."
- Tier 2: Professional, serious. ALWAYS start with "Thank you for reporting this." Confirm issue, say management notified. No exclamation marks. NEVER promise action.
- Tier 3: Empathetic. ALWAYS start with "Thank you for reaching out." Acknowledge frustration. Invite more details. No exclamation marks.
- Tier 4 positive: Warm, friendly. ALWAYS start with "Thank you!" Use exclamation marks.
- Tier 4 inquiry: ALWAYS start with "Thank you for contacting us." NEVER answer business questions. If vague, ask follow-up. Forward to management.

FOLLOW-UP QUESTIONS (when to ask):
- Tier 3 (reputation): Ask for more detail to help owner respond.
- Tier 4 inquiry: Ask for clarification if vague.
- DON'T ask Tier 1 or clear Tier 2 (just acknowledge and forward).
- Examples: "Which machine/location?", "Can you tell us more?", "Is this still happening?"

HARD RULES:
- NEVER fabricate business information.
- NEVER promise action will be taken.
- NEVER claim to have contacted emergency services.
- NEVER ask follow-up for Tier 1 or clear Tier 2.
- Keep auto_reply under 160 characters.
- Vary responses. Don't repeat templates.
- ALWAYS thank customer first.

CONTEXT AWARENESS:
- If conversation history is provided, USE IT. A follow-up to a complaint stays in that complaint's context.
- "Yeah she was so mean" after "terrible service" = still Tier 3, same complaint.
- Don't reclassify follow-ups from scratch. Read the thread.

EDGE CASES:
- "Your bathroom is flooding!" = Tier 1, safety. ALWAYS emergency.
- "Carwash bay won't take my card" = Tier 2, payment.
- "Washer is leaking water" = Tier 2, equipment.
- "Gas pump is showing an error" = Tier 2, payment/equipment.
- "Arcade machine is jammed" = Tier 2, equipment.
- "Parking gate is stuck" = Tier 2, equipment.
- "Music is too loud" = Tier 3. Acknowledge, don't promise change.
- "Can't get in the front door" = Tier 2 (access blocked).
- "What time do you close?" = Tier 4, inquiry. Don't answer.
- "You should fire her" = Tier 3, staffing complaint. NOT emergency.
- "Out of toilet paper" = Tier 2, supply.
- Any equipment failure, payment failure, or machinery jam = Tier 2 (customers cannot complete transactions).

Respond ONLY with JSON: {"tier":<int>,"category":"<str>","sentiment":"<str>","confidence":<float>,"summary":"<str>","auto_reply":"<str>"}"""


@app.post("/demo/classify")
async def demo_classify(request_data:dict=None):
    _ensure_init()
    if not request_data: return {"error":"No message"}
    text = (request_data.get("message") or "").strip()
    history = request_data.get("history") or []
    if not text: return {"error":"No message"}
    if len(text) > 500: return {"error":"Too long"}
    if _ai_client:
        try:
            user_msg = ""
            if history:
                user_msg = "Conversation so far:\n"
                for h in history[-6:]: user_msg += f'Customer: "{h.get("customer","")}"\nSystem: "{h.get("reply","")}"\n\n'
                user_msg += f'New message from same customer: "{text}"\n\nClassify with full context.'
            else: user_msg = f'Classify this customer SMS:\n\n"{text}"'
            raw = _anthropic_http(DEMO_PROMPT, user_msg, model="claude-haiku-4-5-20251001")
            if raw.startswith("```"): raw = raw.split("\n",1)[1].rsplit("```",1)[0].strip()
            c = json.loads(raw)
            c["tier"]=max(1,min(4,int(c.get("tier",4)))); c["confidence"]=max(0.0,min(1.0,float(c.get("confidence",0.5))))
            for k,v in [("category","other"),("sentiment","neutral"),("summary",text[:50]),("auto_reply","Thanks so much for reaching out!")]: c.setdefault(k,v)
        except Exception as e: logger.error(f"Demo: {e}"); c = _classify_fallback(text)
    else: c = _classify_fallback(text)
    return {"tier":c["tier"],"category":c["category"],"sentiment":c["sentiment"],"confidence":c["confidence"],
            "summary":c["summary"],"auto_reply":c["auto_reply"],
            "tier_label":{1:"Emergency",2:"Business-Critical",3:"Reputation Risk",4:"Routine"}.get(c["tier"],"Unknown"),
            "would_alert":c["tier"]==1 or (c["tier"]==2 and c["confidence"]>0.7)}


DEMO_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotline \u2014 Stop losing customers to fixable problems</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.top{text-align:center;padding:32px 24px 20px;max-width:640px;margin:0 auto}
h1{font-size:clamp(28px,5vw,40px);font-weight:700;line-height:1.15;margin-bottom:12px;letter-spacing:-0.02em;color:#1a1a1a}h1 em{font-style:normal;color:#ea580c}
.sub{font-size:16px;color:#888;max-width:480px;margin:0 auto 20px}
.phones{display:flex;gap:24px;margin:0 auto 20px;justify-content:center;align-items:flex-start;max-width:860px;padding:0 20px}
.device{width:320px;flex-shrink:0}
.frame{background:#fff;border-radius:36px;border:3px solid #e0e0dc;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,0.08)}
.notch{width:100px;height:28px;background:#fff;border-radius:0 0 16px 16px;margin:0 auto;position:relative;z-index:2}.notch::before{content:'';width:8px;height:8px;background:#e8e8e4;border-radius:50%;position:absolute;right:20px;top:8px}
.statusbar{display:flex;justify-content:space-between;padding:2px 20px 6px;font-size:11px;color:#aaa;margin-top:-10px}
.phone-label-bar{text-align:center;padding:6px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.06em;border-bottom:1px solid #f0f0ec}
.phone-label-bar.customer{color:#2563eb}.phone-label-bar.owner{color:#ea580c}
.pref-bar{display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 20px;flex-wrap:wrap}
.pref-label{font-size:13px;color:#888;font-weight:500}
.filter-btn{font-size:12px;padding:6px 14px;border-radius:6px;border:1px solid #e0e0dc;background:#fff;color:#888;cursor:pointer;font-family:inherit;font-weight:600;transition:all 0.2s}
.filter-btn.active{background:#ea580c;color:#fff;border-color:#ea580c}

.msgs{height:320px;overflow-y:auto;padding:12px 14px;background:#fafaf8}
.bubble{padding:9px 13px;border-radius:16px;font-size:13px;margin-bottom:7px;max-width:88%;line-height:1.45;animation:fadeUp 0.3s ease both}
.bubble.in{background:#e8e8e4;color:#333;border-bottom-left-radius:4px}
.bubble.out-blue{background:#2563eb;color:#fff;margin-left:auto;border-bottom-right-radius:4px}
.bubble.alert{background:#fff7ed;border:1px solid #fed7aa;color:#b45309;border-bottom-left-radius:4px}
.bubble.alert-red{background:#fef2f2;border:1px solid #fecaca;color:#dc2626;border-bottom-left-radius:4px}
.bubble.feedback{background:#fefce8;border:1px solid #fef08a;color:#a16207;border-bottom-left-radius:4px}
.bubble.info{background:#f0f0ec;color:#666;border-bottom-left-radius:4px}
.bubble.system{background:#f0f0ec;color:#999;font-size:11px;text-align:center;max-width:100%;border-radius:8px;padding:6px 10px}
.bubble.cmd{background:#e8e8e4;color:#333;margin-left:auto;border-bottom-right-radius:4px;font-family:monospace;font-weight:500}
.bubble.resp{background:#f5f5f0;color:#555;border-bottom-left-radius:4px;font-size:12px;white-space:pre-line;line-height:1.5}
.bubble .lbl{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:#aaa;margin-bottom:3px}
.meta{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}.tag{font-size:10px;padding:2px 7px;border-radius:4px;font-weight:500}
.tag.t1{background:#fee2e2;color:#dc2626}.tag.t2{background:#fff7ed;color:#b45309}.tag.t3{background:#fef9c3;color:#a16207}.tag.t4{background:#f0f0ec;color:#888}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.input-area{padding:8px 12px 12px;border-top:1px solid #f0f0ec;background:#fff}
.input-row{display:flex;gap:6px}.input-row input{flex:1;padding:10px 12px;background:#f5f5f0;border:1px solid #e0e0dc;border-radius:20px;font-size:14px;color:#1a1a1a;font-family:inherit}.input-row input::placeholder{color:#bbb}.input-row input:focus{outline:none;border-color:#ea580c}
.input-row button{padding:10px 14px;border-radius:50%;border:none;font-size:16px;cursor:pointer;width:40px;height:40px;display:flex;align-items:center;justify-content:center}
.input-row button.blue{background:#2563eb;color:#fff}.input-row button.orange{background:#ea580c;color:#fff}
.input-row button:disabled{opacity:0.3;cursor:not-allowed}
.owner-cmds{display:none;padding:4px 12px 6px;gap:5px;flex-wrap:wrap;background:#fff}
.cmd-btn{font-size:11px;padding:5px 10px;background:#f5f5f0;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;font-family:monospace;font-weight:600}.cmd-btn:hover{border-color:#ea580c;color:#1a1a1a}
.owner-input{display:none}.home-bar{width:120px;height:4px;background:#ddd;border-radius:2px;margin:8px auto 10px}
.examples{margin-bottom:20px;padding:0 20px}.examples p{font-size:12px;color:#aaa;margin-bottom:6px;text-align:center}
.ex-row{display:flex;flex-wrap:wrap;gap:6px;justify-content:center}.ex{font-size:12px;padding:6px 10px;background:#fff;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,0.04)}.ex:hover{border-color:#2563eb;color:#1a1a1a}
.cta{text-align:center;margin:24px 0;padding:0 20px}.cta a{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px}

footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:4px}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:700px){.phones{flex-direction:column;align-items:center}.device{width:100%;max-width:360px}}
.howitworks{max-width:640px;margin:0 auto;padding:0 20px 28px}
.hiw-steps{display:flex;flex-direction:column;gap:14px}
.hiw-step{display:flex;align-items:flex-start;gap:14px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px}
.hiw-num{width:28px;height:28px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:13px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}
.hiw-step strong{font-size:14px;display:block;margin-bottom:2px}
.hiw-step p{font-size:13px;color:#888;margin:0;line-height:1.4}

</style></head><body>
""" + NAV_HTML + """
<div class="top">
<h1 style="max-width:800px;margin:0 auto 12px;font-size:clamp(28px,4vw,44px);line-height:1.15">Know when your business needs you.<br><em>AI handles the rest.</em></h1>
<p class="sub">Customers text. AI filters. You get alerted when something actually needs your attention.</p>
<p style="font-size:13px;color:#aaa;margin-bottom:8px"><strong style="color:#1a1a1a;font-weight:700">No app. No software. No setup. No training.</strong></p>
<p style="font-size:12px;font-weight:500;color:#bbb;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.06em">Try a scenario or type your own</p>
<div class="examples">
<div class="ex-row">
<div class="ex" onclick="tryEx(this)">Your bathroom is flooding!</div>
<div class="ex" onclick="tryEx(this)">I've been waiting 25 minutes, nobody's helped me</div>
<div class="ex" onclick="tryEx(this)">The front door is locked and there's a line outside</div>
<div class="ex" onclick="tryEx(this)">Carwash bay 2 won't take my card</div>
<div class="ex" onclick="tryEx(this)">Guy at the counter was really rude to me</div>
<div class="ex" onclick="tryEx(this)">Washer #3 is leaking water everywhere</div>
<div class="ex" onclick="tryEx(this)">Gas pump is showing an error</div>
<div class="ex" onclick="tryEx(this)">Arcade machine is jammed and eating coins</div>
<div class="ex" onclick="tryEx(this)">Parking gate is stuck closed</div>
</div>
<div style="margin-top:12px"><button onclick="resetDemo()" style="padding:6px 12px;background:#f0f0f0;color:#666;border:1px solid #e0e0dc;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer">Reset</button></div>
</div>
<div class="phones">
<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G &nbsp; 87%</span></div>
<div class="phone-label-bar customer">Customer</div>
<div class="msgs" id="m-cust"><div class="bubble system">Customer messages appear here</div></div>
<div class="input-area"><div class="input-row">
<input type="text" id="cust-input" placeholder="Type a message..." onkeydown="if(event.key==='Enter')sendDemo()">
<button class="blue" id="cust-btn" onclick="sendDemo()">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G &nbsp; 92%</span></div>
<div class="phone-label-bar owner">Owner</div>
<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 14px 4px;background:#fff8f5;border-bottom:1px solid #f0f0ec;font-size:11px;color:#aaa;gap:6px"><span style="font-weight:600;color:#888;white-space:nowrap">Alert level:</span><div style="display:flex;gap:4px"><button class="filter-btn active" id="filt-crit" onclick="setFilter('critical')" style="font-size:10px;padding:3px 10px;border-radius:4px">🔴 Critical only</button><button class="filter-btn" id="filt-all" onclick="setFilter('all')" style="font-size:10px;padding:3px 10px;border-radius:4px">📋 All messages</button></div></div>
<div class="msgs" id="m-owner"><div class="bubble system">Owner alerts appear here</div></div>
<div class="owner-cmds" id="owner-cmds">
<div class="cmd-btn" onclick="ownerCmd('DETAILS')">DETAILS</div>
<div class="cmd-btn" onclick="ownerCmd('THUMBSUP')">&#128077;</div>
<div class="cmd-btn" onclick="ownerCmd('OK')">OK</div>
<div class="cmd-btn" onclick="ownerCmd('REPLY')">REPLY</div>
</div>
<div class="input-area owner-input" id="owner-input"><div class="input-row">
<input type="text" id="owner-inp" placeholder="Type a command..." onkeydown="if(event.key==='Enter')ownerCmd(this.value)">
<button class="orange" onclick="ownerCmd(document.getElementById('owner-inp').value)">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
</div>


<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>

<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa">Instagram</a></footer>
<script>
let lastData=null,acked=false,replyMode=false,history=[],demoCount=0,maxDemo=10,filterMode='critical';
const mc=document.getElementById('m-cust'),mo=document.getElementById('m-owner');
function addB(c,cls,label,text,tier){const d=document.createElement('div');d.className='bubble '+cls;if(tier)d.setAttribute('data-tier',tier);let h='';if(label)h+='<div class="lbl">'+label+'</div>';h+=text;d.innerHTML=h;c.appendChild(d);c.scrollTop=c.scrollHeight;applyFilter();return d}
function tryEx(el){document.getElementById('cust-input').value=el.textContent;sendDemo()}
function showOwnerInput(){document.getElementById('owner-cmds').style.display='flex';document.getElementById('owner-input').style.display='block'}
function hideOwnerInput(){document.getElementById('owner-cmds').style.display='none';document.getElementById('owner-input').style.display='none'}
function resetDemo(){history=[];lastData=null;acked=false;replyMode=false;demoCount=0;mc.innerHTML='<div class="bubble system">Customer messages appear here</div>';mo.innerHTML='<div class="bubble system">Owner alerts appear here</div>';document.getElementById('cust-input').value='';document.getElementById('owner-inp').value='';hideOwnerInput();addB(mo,'resp','','Conversation reset. Ready for a new scenario.')}
function setFilter(mode){filterMode=mode;document.getElementById('filt-all').className='filter-btn'+(mode==='all'?' active':'');document.getElementById('filt-crit').className='filter-btn'+(mode==='critical'?' active':'');applyFilter()}
function applyFilter(){mo.querySelectorAll('.bubble[data-tier]').forEach(function(b){var t=parseInt(b.getAttribute('data-tier'));b.style.display=(filterMode==='all'||t<=2)?'':'none'})}

(function(){document.getElementById('filt-crit').classList.add('active')})();function ownerCmd(raw){const cmd=(raw||'').trim().toUpperCase();const inp=document.getElementById('owner-inp');inp.value='';if(!cmd)return;
if(replyMode){replyMode=false;addB(mo,'cmd','',raw.trim());addB(mo,'resp','','Reply sent to the customer.');addB(mc,'in','Reply from owner',raw.trim());inp.placeholder='Type a command...';return}
addB(mo,'cmd','',raw.trim());
if(!lastData){addB(mo,'resp','','No active alerts.');return}
if(cmd==='DETAILS'){const d=lastData;const now=new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});const ackLabel=acked?'\\u2705 Acknowledged':'\\u23f3 Pending';addB(mo,'resp','','Alert \\u2014 '+ackLabel+'\\nTime: '+now+'\\nCategory: '+d.category.replace('_',' ')+'\\nMessage: "'+d.original_message+'"\\nReply OK to close, REPLY to respond, SNOOZE to revisit in 1hr.');return}
if(cmd==='REPLY'){replyMode=true;addB(mo,'resp','','What would you like to say to the customer? Type your reply now.');inp.placeholder='Type your reply...';inp.focus();return}
if(['OK','GOT IT','DONE','ON IT','ACK','THUMBSUP'].includes(cmd)){if(acked){addB(mo,'resp','','Already acknowledged.')}else{acked=true;addB(mo,'resp','','\\u2705 Alert acknowledged.')}return}
addB(mo,'resp','','Try DETAILS, OK, or REPLY.')}
async function sendDemo(){const inp=document.getElementById('cust-input');const btn=document.getElementById('cust-btn');const text=inp.value.trim();if(!text)return;
if(demoCount>=maxDemo){addB(mc,'system','','Demo limit reached. <a href="/signup" style="color:#ea580c">Sign up</a> to get started!');return}
inp.value='';btn.disabled=true;demoCount++;acked=false;replyMode=false;
addB(mc,'out-blue','',text);addB(mo,'system','','<span class="spinner"></span> Processing...');
try{const r=await fetch('/demo/classify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,history:history})});const d=await r.json();d.original_message=text;lastData=d;mo.lastChild.remove();
history.push({customer:text,reply:d.auto_reply});if(history.length>10)history.shift();
await new Promise(r=>setTimeout(r,300));addB(mc,'in','Auto-reply',d.auto_reply);await new Promise(r=>setTimeout(r,400));
const tierCls='t'+d.tier;const tags='<div class="meta"><span class="tag '+tierCls+'">'+d.tier_label+'</span><span class="tag '+tierCls+'">'+d.category.replace('_',' ')+'</span></div>';
if(d.tier===1){addB(mo,'alert-red','Emergency','\\ud83d\\udea8 URGENT: '+d.summary+'\\nReply: DETAILS',1);showOwnerInput()}
else if(d.tier===2){addB(mo,'alert','Alert','\\u26a0\\ufe0f Issue reported: '+d.summary+'\\nReply OK to acknowledge',2);showOwnerInput()}
else if(d.tier===3){addB(mo,'feedback','Feedback','\\ud83d\\ude14 '+d.summary+tags,3);showOwnerInput()}
else{addB(mo,'info','Message','\\ud83d\\udcac '+d.summary+tags,4);showOwnerInput()}}
catch(e){mo.lastChild.remove();addB(mo,'system','','Demo error. Try again.')}btn.disabled=false;inp.focus()}
</script></body></html>"""

@app.get("/demo")
def demo_page(): _ensure_init(); return Response(content=DEMO_HTML, media_type="text/html")


# --- How It Works page ---
HOW_IT_WORKS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>How It Works \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.hero{text-align:center;padding:48px 24px 24px;max-width:680px;margin:0 auto}
h1{font-size:clamp(28px,5vw,40px);font-weight:700;line-height:1.15;margin-bottom:14px}
.sub{font-size:17px;color:#666;line-height:1.5}
section{max-width:720px;margin:0 auto;padding:32px 24px}
h2{font-size:22px;font-weight:700;margin-bottom:8px;text-align:center}
.section-sub{font-size:15px;color:#888;text-align:center;margin-bottom:28px}
.steps{display:flex;flex-direction:column;gap:14px}
.step{display:flex;align-items:flex-start;gap:16px;background:#fff;border:1px solid #e0e0dc;border-radius:12px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.step-num{width:34px;height:34px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:15px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}
.step strong{font-size:16px;display:block;margin-bottom:6px}
.step p{font-size:14px;color:#666;line-height:1.55;margin:0}
.places{background:#fff;border:1px solid #e0e0dc;border-radius:12px;padding:24px;margin-top:8px}
.places-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}
.place{background:#f8f8f6;border:1px solid #ececea;border-radius:8px;padding:10px 8px;font-size:13px;text-align:center;color:#333}
.note{font-size:13px;color:#888;margin-top:14px;text-align:center;font-style:italic}
.filter-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
.filter-col{background:#fff;border:1px solid #e0e0dc;border-radius:12px;padding:18px}
.filter-col h3{font-size:14px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.filter-col ul{list-style:none;padding:0;margin:0}
.filter-col li{font-size:13px;color:#555;padding:4px 0;line-height:1.4}
.in h3{color:#15803d}
.out h3{color:#888}
.commands{background:#1a1a1a;color:#fff;border-radius:12px;padding:24px;margin-top:8px}
.cmd{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #2a2a2a;font-size:14px}
.cmd:last-child{border-bottom:none}
.cmd code{background:#ea580c;color:#fff;padding:3px 10px;border-radius:4px;font-family:monospace;font-weight:700;font-size:13px;flex-shrink:0;min-width:70px;text-align:center}
.cmd span{color:#ccc;line-height:1.5}
.faq{margin-top:8px}
.q{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px;margin-bottom:10px}
.q strong{display:block;font-size:15px;margin-bottom:6px}
.q p{font-size:14px;color:#666;line-height:1.5;margin:0}
.cta{text-align:center;padding:24px 24px 16px}
.cta a{display:inline-block;padding:16px 36px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:17px}
.cta .fine{display:block;margin-top:10px;font-size:13px;color:#888}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:32px}
.sign-preview{max-width:280px;margin:20px auto 0;text-align:center}.sign-preview img{width:100%;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,0.08);border:1px solid #e0e0dc}@media(max-width:600px){.filter-grid{grid-template-columns:1fr}.places-grid{grid-template-columns:repeat(2,1fr)}.sign-preview{max-width:240px}}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<h1>Your customers already know what's broken.<br>Now you will too.</h1>
<p class="sub">A QR code on the wall. A text to you when it matters. That's the whole product.</p>
</div>

<section>
<h2>Three steps. No software.</h2>
<p class="section-sub">Setup takes 2 minutes. Then it just runs.</p>
<div class="steps">
<div class="step"><div class="step-num">1</div><div><strong>Get your hotline.</strong><p>Sign up and instantly receive your unique QR code plus a print-ready sign \u2014 delivered digitally, ready to use right away. Yours forever.</p></div></div>
<div class="step"><div class="step-num">2</div><div><strong>Put it anywhere customers look.</strong><p>The QR is yours to use anywhere. Bathroom mirror. Menu. Receipt. Front door. Wherever they might need you.</p></div></div>
<div class="step"><div class="step-num">3</div><div><strong>You get a text \u2014 only when it matters.</strong><p>AI filters every message. Emergencies and broken stuff reach you in seconds. Noise gets quietly logged. Reply OK or DETAILS by text.</p></div></div>
</div>
</section>

<section>
<h2>One QR. Many homes.</h2>
<p class="section-sub">It's not a sign. It's a hotline. Put it everywhere customers might need you.</p>
<div class="places">
<div class="places-grid">
<div class="place">Bathroom mirror</div>
<div class="place">Table tent</div>
<div class="place">Receipt</div>
<div class="place">Menu</div>
<div class="place">Front door</div>
<div class="place">Staff badge</div>
<div class="place">Window decal</div>
<div class="place">Drive-thru</div>
<div class="place">Locker room</div>
<div class="place">Hotel key card</div>
<div class="place">Invoice footer</div>
<div class="place">Anywhere else</div>
</div>
<p class="note">We give you a sign to start. The QR is yours to drop into any signage you want.</p>
</div>
<div class="sign-preview">
<img src="data:image/jpeg;base64,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i3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm55Xi3/npoP/AH6n/wAaPK8W/wDPTQf+/U/+NYf/AAqbRf8AoIa3/wCBh/wo/wCFTaL/ANBDW/8AwMP+FFq/8v8A5N/wAvhv5/8AyT/gm4YfFpGPN0FffyZzj8N1U7mGx0ORdZ8T60lxcQAmHzFEccJI58qIZJbtk5P0rP8A+FTaL/0ENb/8DD/hVrTvhh4ZsJxcPazX0y8hryUy4/DgfnS5az+yvnJtfdYOfDR+2/lFJ/ffQytIivPHviKDxHcQyW2j6ef+JdFKMNM2RmQj8P5Dsa9BpAAoCqAABgAdAKa8QkxkuMf3WI/lXVQo+zTu7t6tnFicR7WSsrRWiXZf1ux9FRC3UEHdLx6yH/Gpa2OcKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD/2Q==" alt="Hotline QR sign example" />
</div>
</section>

<section>
<h2>High signal. Low noise.</h2>
<p class="section-sub">AI decides what reaches your phone.</p>
<div class="filter-grid">
<div class="filter-col in"><h3>\u2705 You get</h3><ul>
<li>Emergencies (injury, fire, hazards)</li>
<li>Broken equipment or supplies</li>
<li>Angry customers about to leave</li>
<li>Anything that needs you now</li>
</ul></div>
<div class="filter-col out"><h3>\u2715 Filtered out</h3><ul>
<li>Routine compliments</li>
<li>Spam and gibberish</li>
<li>Vague complaints with no detail</li>
<li>Anything that can wait</li>
</ul></div>
</div>
</section>

<section>
<h2>Manage everything by text.</h2>
<p class="section-sub">No app. No dashboard. No login. Your phone is the dashboard.</p>
<div class="commands">
<div class="cmd"><code>DETAILS</code><span>See the full customer message and category</span></div>
<div class="cmd"><code>OK</code><span>Mark the issue handled — or just react 👍</span></div>
<div class="cmd"><code>LIST</code><span>See the last 5 flagged issues</span></div>
<div class="cmd"><code>HELP</code><span>See all commands</span></div>
</div>
</section>

<section>
<h2>Common questions</h2>
<div class="faq">
<div class="q"><strong>Will I get spammed?</strong><p>No. AI filters every message. Most never reach you. You only hear about things that need you.</p></div>
<div class="q"><strong>Do customers need an app?</strong><p>No. They scan or text. Works on any phone. No download, no account.</p></div>
<div class="q"><strong>What if the AI gets it wrong?</strong><p>Reply DETAILS to any alert to see the exact words the customer sent. You stay in control.</p></div>
<div class="q"><strong>How long does setup take?</strong><p>2 minutes. Sign up, print your sign, you're live.</p></div>
</div>
</section>

<div class="cta">
<a href="/signup">Get your hotline \u2192</a>
<span class="fine">No card. No app. Cancel by text.</span>
</div>

<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa">Instagram</a></footer>
</body></html>"""

@app.get("/how-it-works")
def how_it_works_page():
    _ensure_init()
    return Response(content=HOW_IT_WORKS_HTML, media_type="text/html")


# --- Industries page ---
INDUSTRIES_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Who We Support \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.hero{text-align:center;padding:40px 24px 32px;max-width:600px;margin:0 auto}
h1{font-size:clamp(24px,4vw,36px);font-weight:700;margin-bottom:12px}
.sub{font-size:16px;color:#888;margin-bottom:32px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;max-width:800px;margin:0 auto 48px;padding:0 24px}
.card{background:#fff;border:1px solid #e0e0dc;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.card-top{padding:18px 20px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.card-top h3{font-size:16px;font-weight:600;margin:0}
.card-top .icon{font-size:20px;margin-right:10px}
.card-top .arrow{font-size:14px;color:#bbb;transition:transform 0.2s}
.card.open .arrow{transform:rotate(90deg)}
.card-body{display:none;padding:0 20px 16px;font-size:13px;color:#666;line-height:1.5}
.card.open .card-body{display:block}
.tag-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.tag-sm{font-size:11px;padding:3px 8px;background:#f5f5f0;border-radius:4px;color:#888}
.cta{text-align:center;padding:0 24px 48px}
.cta a{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<h1>Know what's happening before it costs you</h1>
<p class="sub">Hotline alerts owners and senior management to the things that matter most: safety risks, operational failures, and the moments that make or break your reputation.</p>
</div>
<div class="grid">
<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128664;</span><h3 style="display:inline">Car Washes</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The pressure washer isn't working on bay 2."</em> A customer tries to pay, can't complete the transaction, and leaves. You're losing revenue every minute until you find out. Hotline makes sure you know about equipment failures, payment jams, and service issues before the next customer walks away.<div class="tag-row"><span class="tag-sm">equipment failures</span><span class="tag-sm">payment issues</span><span class="tag-sm">service disruptions</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#129499;</span><h3 style="display:inline">Laundromats</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"Machine #4 is leaking water all over the floor."</em> A broken dryer or washer is silent revenue loss—every 30 minutes without it, you're losing a customer transaction. Hotline tells you when it starts, not after you find standing water and potential liability issues.<div class="tag-row"><span class="tag-sm">equipment leaks</span><span class="tag-sm">safety hazards</span><span class="tag-sm">capacity loss</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127918;</span><h3 style="display:inline">Arcades & Gaming</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The pinball machine is stuck and won't take coins."</em> A broken cabinet is lost revenue, not just now but forever—that kid goes to the arcade down the street instead. Hotline alerts you to jams, payment failures, and malfunctions so you can fix them before your customers find a competitor.<div class="tag-row"><span class="tag-sm">payment jams</span><span class="tag-sm">machine failures</span><span class="tag-sm">revenue loss</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128472;</span><h3 style="display:inline">Parking Garages & Lots</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The gate is stuck closed and won't open."</em> A broken gate = zero revenue that hour and frustrated customers. Payment systems down, ticket machines jammed, access cards failing—you need to know instantly, not when you check the cameras tomorrow.<div class="tag-row"><span class="tag-sm">gate failures</span><span class="tag-sm">payment system down</span><span class="tag-sm">access issues</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#9981;</span><h3 style="display:inline">Gas Stations</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"Pump 3 is showing an error and won't accept my card."</em> A broken pump drives customers away mid-transaction. Payment readers fail, nozzles jam, systems go offline—each minute of downtime is lost gallons and frustrated drivers heading elsewhere.<div class="tag-row"><span class="tag-sm">pump failures</span><span class="tag-sm">payment reader issues</span><span class="tag-sm">system outages</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128273;</span><h3 style="display:inline">Car Rental Kiosks</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The kiosk won't read my license."</em> A down kiosk means customers can't rent, access vehicles, or complete transactions. License readers fail, touch screens freeze, payment systems timeout—your revenue stream stops instantly. Hotline gets you the alert before you miss a single rental.<div class="tag-row"><span class="tag-sm">kiosk outages</span><span class="tag-sm">reader failures</span><span class="tag-sm">payment downtime</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#9749;</span><h3 style="display:inline">Restaurants & Cafes</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"There's no one at the register and the bathroom is flooded."</em> You're across town. Without Hotline, this becomes a 1-star review. With it, you know in seconds—whether it's a no-show, an equipment failure, or an angry customer.<div class="tag-row"><span class="tag-sm">staffing issues</span><span class="tag-sm">food safety</span><span class="tag-sm">customer experience</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128722;</span><h3 style="display:inline">Retail Stores</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The self-checkout is down"</em> or <em>"Fitting room door is broken."</em> You hear about it when sales are already lost. Hotline connects you to what customers see the moment it matters.<div class="tag-row"><span class="tag-sm">equipment downtime</span><span class="tag-sm">customer friction</span><span class="tag-sm">safety issues</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127947;</span><h3 style="display:inline">Gyms & Fitness Studios</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The treadmill isn't working"</em> or <em>"Access card reader is down."</em> Members pay for working equipment. A broken machine or locked building means lost member trust and churn.<div class="tag-row"><span class="tag-sm">equipment failures</span><span class="tag-sm">access issues</span><span class="tag-sm">member satisfaction</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#9986;</span><h3 style="display:inline">Salons & Barbershops</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"A customer got a chemical burn"</em> or <em>"Appointment system crashed."</em> Safety issues and booking problems hit reputation and liability instantly. Hotline makes sure you know before damage spreads.<div class="tag-row"><span class="tag-sm">safety incidents</span><span class="tag-sm">system failures</span><span class="tag-sm">customer injury</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128295;</span><h3 style="display:inline">Auto Repair Shops</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"Your shop damaged my car"</em> or <em>"I've been waiting 4 hours."</em> Customers tell you how they feel the moment it happens. Hotline ensures you can respond to issues before they become bad reviews.<div class="tag-row"><span class="tag-sm">quality complaints</span><span class="tag-sm">wait time issues</span><span class="tag-sm">damage claims</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127976;</span><h3 style="display:inline">Hotels & Airbnbs</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The AC in room 205 stopped working and it's midnight."</em> A guest complaint is a potential bad review. Hotline gets you the alert while the guest is still there, not after they post about it online.<div class="tag-row"><span class="tag-sm">equipment failures</span><span class="tag-sm">guest complaints</span><span class="tag-sm">reputation risk</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127973;</span><h3 style="display:inline">Medical & Dental Offices</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"Your equipment isn't sterilized"</em> or <em>"No one's answering the phone."</em> Patient safety and trust are non-negotiable. Hotline keeps you alert to operational and safety issues in real-time.<div class="tag-row"><span class="tag-sm">safety protocols</span><span class="tag-sm">equipment issues</span><span class="tag-sm">staff gaps</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128187;</span><h3 style="display:inline">Coworking Spaces</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"WiFi is down"</em> or <em>"The bathroom is unusable."</em> Members are paying for a working environment. Know about disruptions before members lose their workspace and consider leaving.<div class="tag-row"><span class="tag-sm">connectivity issues</span><span class="tag-sm">facility problems</span><span class="tag-sm">member experience</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128230;</span><h3 style="display:inline">Storage Facilities</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The gate code isn't working"</em> or <em>"There's water coming into my unit."</em> Customers only visit occasionally — when something goes wrong, they need to reach you fast. Hotline puts a direct line on every unit door so you hear about gate failures, leaks, break-ins, and climate control issues before they become liability claims or lost renewals.<div class="tag-row"><span class="tag-sm">gate & access issues</span><span class="tag-sm">water intrusion</span><span class="tag-sm">climate control</span><span class="tag-sm">security concerns</span></div></div></div>
</div>

<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa">Instagram</a></footer>
</body></html>"""

@app.get("/industries")
def industries_page(): _ensure_init(); return Response(content=INDUSTRIES_HTML, media_type="text/html")


# --- Signup page ---
# ============================================================
# TWILIO COMPLIANCE NOTE — DO NOT REMOVE OR MODIFY opt-in block
# The SMS opt-in checkbox below (id="f-optin") is required for
# Twilio A2P 10DLC and toll-free verification (error 30445).
# It must remain: unchecked by default, required before submit,
# and include the exact disclosure text with STOP/HELP/rates.
# Removing or pre-checking this checkbox will cause Twilio
# campaign registration to fail. — Last reviewed: 2025
# ============================================================

# --- Resources pages ---
_ARTICLE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.wrap{max-width:660px;margin:0 auto;padding:32px 24px 80px}
.breadcrumb{font-size:12px;color:#aaa;margin-bottom:28px}
.breadcrumb a{color:#aaa}.breadcrumb a:hover{color:#ea580c}
header.ah{margin-bottom:36px;padding-bottom:24px;border-bottom:1px solid #e0e0dc}
header.ah h1{font-size:clamp(24px,4vw,38px);font-weight:700;line-height:1.15;margin-bottom:14px;letter-spacing:-0.02em}
.ameta{display:flex;gap:20px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:#aaa}
article h2{font-size:20px;font-weight:700;margin-top:36px;margin-bottom:12px;line-height:1.2}
article p{font-size:16px;line-height:1.7;margin-bottom:14px;color:#333}
article p.lead{font-size:18px;font-weight:500;color:#1a1a1a}
article strong{font-weight:700;color:#1a1a1a}
article ul{list-style:none;padding:0;margin:10px 0 18px}
article ul li{font-size:15px;line-height:1.6;padding-left:18px;margin-bottom:8px;position:relative;color:#333}
article ul li::before{content:'*';position:absolute;left:0;color:#ea580c;font-weight:700}
.pullquote{font-size:22px;font-weight:700;line-height:1.25;padding:18px 0 18px 18px;margin:24px 0;border-left:3px solid #ea580c;color:#1a1a1a}
.callout{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:18px 20px;margin:16px 0}
.callout-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#ea580c;margin-bottom:8px}
.callout p{font-size:15px;margin-bottom:0}
.placement-block{border-top:1px solid #e0e0dc;padding:18px 0}
.placement-block:first-child{border-top:none;padding-top:0}
.placement-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#aaa;margin-bottom:5px}
.placement-title{font-size:16px;font-weight:700;margin-bottom:10px}
.sample{background:#1a1a1a;color:#f8f8f6;padding:13px 16px;border-radius:8px;margin:8px 0;font-size:15px;font-style:italic;line-height:1.4}
.brand-block{background:#1a1a1a;color:#f8f8f6;padding:26px;border-radius:12px;margin:36px 0}
.brand-block h2{color:#fff;margin-top:0;font-size:18px;margin-bottom:10px}
.brand-block p{color:rgba(248,248,246,0.8);font-size:15px;margin-bottom:0;line-height:1.6}
.article-cta{margin-top:56px;padding-top:32px;border-top:1px solid #e0e0dc;text-align:center}
.article-cta h3{font-size:22px;font-weight:700;margin-bottom:18px}
.cta-btn{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px;transition:background 0.2s}
.cta-btn:hover{background:#dc2626;color:#fff}
.back-link{display:inline-block;margin-top:44px;font-size:13px;color:#aaa;font-weight:500}
.back-link:hover{color:#ea580c}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:40px}
"""

_ARTICLE_FOOT = """<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa">Instagram</a></footer>"""

_ARTICLE_CTA = """<div class="article-cta"><h3>Set up your Hotline today.</h3><a href="https://hotlinetxt.com/signup" class="cta-btn">Sign up &rarr;</a></div>"""

RESOURCES_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resources \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}
""" + NAV_CSS + """
.hero{text-align:center;padding:40px 24px 28px;max-width:600px;margin:0 auto}
h1{font-size:clamp(24px,4vw,36px);font-weight:700;margin-bottom:12px}
.sub{font-size:16px;color:#888}
.grid{max-width:760px;margin:0 auto;padding:24px 24px 60px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:#fff;border:1px solid #e0e0dc;border-radius:14px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,0.04);display:flex;flex-direction:column;gap:12px;transition:box-shadow 0.2s,border-color 0.2s;cursor:pointer}
.card:hover{box-shadow:0 6px 20px rgba(0,0,0,0.09);border-color:#ea580c}
.card-meta{display:flex;justify-content:space-between;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:#aaa}
.card h2{font-size:17px;font-weight:700;line-height:1.3;color:#1a1a1a}
.card p{font-size:13px;color:#888;line-height:1.5;flex:1}
.card .arrow{font-size:13px;font-weight:700;color:#ea580c;align-self:flex-end}
.faq-card{grid-column:1/-1;background:#1a1a1a;border-color:#1a1a1a;color:#f8f8f6}
.faq-card:hover{border-color:#ea580c}
.faq-card .card-meta{color:rgba(248,248,246,0.4)}
.faq-card h2{color:#f8f8f6;font-size:20px}
.faq-card p{color:rgba(248,248,246,0.7)}
.faq-card .arrow{color:#ea580c}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.12em;color:#aaa;margin-bottom:14px">Resources</div>
<h1>Run a tighter operation.</h1>
<p class="sub">Short reads on getting the most out of Hotline.</p>
</div>
<div class="grid">
<a href="/resources/faq" class="card faq-card">
<div class="card-meta"><span>FAQ</span><span>Common questions</span></div>
<h2>Everything you want to know before you sign up</h2>
<p>How Hotline works, what customers see, what owners see, pricing, privacy, and more. Start here.</p>
<span class="arrow">Read &rarr;</span>
</a>
<a href="/resources/why-you-need-a-hotline" class="card">
<div class="card-meta"><span>01 &mdash; Strategy</span><span>3 min read</span></div>
<h2>Why your business needs a direct customer line (and why Hotline is the easiest way to run one)</h2>
<p>Your staff won't always tell you what's wrong. Your customers will, if you give them a way to reach you.</p>
<span class="arrow">Read &rarr;</span>
</a>
<a href="/resources/where-to-put-your-qr" class="card">
<div class="card-meta"><span>02 &mdash; Setup</span><span>3 min read</span></div>
<h2>Where to put your QR code so customers actually use it</h2>
<p>Physical signs are just the start. The best placements are often digital, and most owners skip them entirely.</p>
<span class="arrow">Read &rarr;</span>
</a>
<a href="/resources/responding-to-alerts" class="card">
<div class="card-meta"><span>03 &mdash; Operations</span><span>3 min read</span></div>
<h2>How to respond to alerts without burning out</h2>
<p>Getting the alert is step one. Here's how to handle it fast without creating new problems for yourself.</p>
<span class="arrow">Read &rarr;</span>
</a>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""


RESOURCES_FAQ_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FAQ \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.wrap{max-width:700px;margin:0 auto;padding:32px 24px 80px}
.breadcrumb{font-size:12px;color:#aaa;margin-bottom:28px}
.breadcrumb a{color:#aaa}.breadcrumb a:hover{color:#ea580c}
header.ah{margin-bottom:40px;padding-bottom:24px;border-bottom:1px solid #e0e0dc}
header.ah h1{font-size:clamp(24px,4vw,38px);font-weight:700;line-height:1.15;margin-bottom:12px;letter-spacing:-0.02em}
header.ah p{font-size:16px;color:#888;line-height:1.6}
.section{margin-bottom:48px}
.section-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.14em;color:#ea580c;margin-bottom:20px;padding-bottom:10px;border-bottom:2px solid #ea580c;display:inline-block}
.faq-item{border-bottom:1px solid #e0e0dc;padding:0}
.faq-q{width:100%;background:none;border:none;text-align:left;padding:18px 0;font-family:inherit;font-size:16px;font-weight:600;color:#1a1a1a;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:16px;line-height:1.3}
.faq-q:hover{color:#ea580c}
.faq-icon{font-size:18px;color:#aaa;flex-shrink:0;transition:transform 0.2s;font-weight:400}
.faq-item.open .faq-icon{transform:rotate(45deg);color:#ea580c}
.faq-a{display:none;padding:0 0 18px;font-size:15px;line-height:1.7;color:#555}
.faq-item.open .faq-a{display:block}
.faq-a strong{color:#1a1a1a;font-weight:600}
.faq-a ul{list-style:none;padding:0;margin:10px 0}
.faq-a ul li{padding-left:16px;position:relative;margin-bottom:6px}
.faq-a ul li::before{content:'*';position:absolute;left:0;color:#ea580c;font-weight:700}
.privacy-split{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:4px}
.privacy-block{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px}
.privacy-block-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#aaa;margin-bottom:10px}
.privacy-block p,.privacy-block li{font-size:14px;color:#555;line-height:1.6}
.privacy-block ul{list-style:none;padding:0;margin:0}
.privacy-block ul li{padding-left:14px;position:relative;margin-bottom:5px}
.privacy-block ul li::before{content:'*';position:absolute;left:0;color:#ea580c;font-weight:700}
@media(max-width:560px){.privacy-split{grid-template-columns:1fr}}
.article-cta{margin-top:56px;padding-top:32px;border-top:1px solid #e0e0dc;text-align:center}
.article-cta h3{font-size:22px;font-weight:700;margin-bottom:18px}
.cta-btn{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px;transition:background 0.2s}
.cta-btn:hover{background:#dc2626;color:#fff}
.back-link{display:inline-block;margin-top:44px;font-size:13px;color:#aaa;font-weight:500}
.back-link:hover{color:#ea580c}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:40px}
</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<div class="breadcrumb"><a href="/resources">&larr; Resources</a> &nbsp;/ FAQ</div>
<header class="ah">
<h1>Frequently asked questions</h1>
<p>How Hotline works, what customers and owners see, pricing, and privacy. If something isn't covered here, email us at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>
</header>

<div class="section">
<div class="section-label">Getting started</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What is Hotline? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Hotline is an SMS-based alert system for small business owners. Customers text a number or scan a QR code to report issues. Every message is read, classified by urgency, and you get a text alert when something actually needs your attention. You manage everything by text. No app, no dashboard.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">How does setup work? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Sign up at hotlinetxt.com/signup. You'll get a print-ready sign PDF and a plain QR image texted to you within minutes. Post your sign, and the service starts working. The whole process takes under five minutes.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Do I need an app? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Everything runs through SMS. Customers text in, the message is processed and you get a text alert. You reply by text. Nothing to install on your end or theirs.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What phone number do customers text? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Customers text a dedicated Hotline number. Your QR code and sign include that number pre-formatted, so customers just scan and send. The number is shared infrastructure, not your personal cell.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">How it works</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">How does Hotline decide what to alert me about? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Every incoming message is classified by an AI into one of four tiers based on urgency. You only get alerted for Tier 1 (emergencies) and Tier 2 (operational issues). Lower-tier messages are logged but don't interrupt you unless you've turned on broader alerts.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What are the tiers? <span class="faq-icon">+</span></button>
<div class="faq-a">
<ul>
<li><strong>Tier 1 - Emergency:</strong> Fire, flooding, injury, safety hazard. Always gets through immediately.</li>
<li><strong>Tier 2 - Operational:</strong> Broken equipment, no staff on floor, card reader down, bathroom issues. Alerted if confidence is high.</li>
<li><strong>Tier 3 - Reputation:</strong> Unhappy customer, rude staff complaint, general frustration. Logged. Alerted only if you've opted into broader alerts.</li>
<li><strong>Tier 4 - Routine:</strong> Compliments, questions, neutral messages. Logged silently.</li>
</ul>
</div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Will I get spammed with complaints? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Owners who use Hotline are usually surprised by how quiet it is. Most days you'll get nothing. When something comes in, it's almost always real and actionable. There is also built-in rate limiting so a single frustrated customer can't flood you with alerts.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What does the customer see when they text in? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>They get an automatic reply confirming their message was received. The tone varies by tier. An emergency gets a response telling them to call 911. A complaint gets an empathetic acknowledgment. A compliment gets a warm thank you. Customers never see your personal number or any of your internal alert traffic.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">Managing alerts</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What commands can I text back? <span class="faq-icon">+</span></button>
<div class="faq-a">
<ul>
<li><strong>OK</strong> - Acknowledge and close an alert</li>
<li><strong>DETAILS</strong> - Get the full customer message and timestamp</li>
<li><strong>REPLY</strong> - Send a private reply directly to the customer</li>
<li><strong>LIST</strong> - See the last 5 flagged issues</li>
<li><strong>SNOOZE</strong> - Remind yourself about an alert in 1 hour</li>
<li><strong>QUIET 2H</strong> - Silence non-emergency alerts for a set time</li>
<li><strong>PAUSE / RESUME</strong> - Stop or restart all non-emergency alerts</li>
<li><strong>STATUS</strong> - See your current alert settings</li>
<li><strong>HELP</strong> - Full command list</li>
</ul>
</div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I add a second phone number for a manager or partner? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. You can add a second alert number during signup or ask us to add one after. Both numbers get the same alerts and can use the same commands.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I pause alerts when I'm off the clock? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. Text QUIET 2H (or any number of hours) to silence non-emergency alerts for that window. Text PAUSE to stop them indefinitely until you text RESUME. Tier 1 emergencies always come through regardless.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I reply directly to a customer? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. Text REPLY after receiving an alert, type your message, and your reply goes to the customer from the Hotline number. The customer does not see your personal cell number. You can have a back-and-forth if needed.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">Pricing and trial</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">How much does Hotline cost? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>$19.99 per month after your free trial. No setup fees, no contracts, cancel anytime by texting or emailing us.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Is there a free trial? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes, 14 days. No credit card required to start. You get full access to everything during the trial.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What happens when my trial ends? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>You'll get a text reminder the day before it expires. If you don't subscribe, alerts pause. Customer messages still come in and are logged, but you stop receiving notifications until you reactivate. Nothing is deleted.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">Technical</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Does Hotline work for multiple locations? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Each location needs its own Hotline account and QR code so messages route to the right place. Contact us at Connect@HotlineTXT.com if you're setting up multiple locations and we'll get you sorted.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What if a customer texts without scanning the QR? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>If a customer texts the Hotline number directly without a QR code scan, the message includes a business code that routes it correctly. If there's no code and no recent session, they'll get a prompt to scan the QR. Messages without a valid business code aren't forwarded to any owner.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">Privacy</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Will the business know it was me who texted? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Every message goes through the Hotline number, not your personal phone. The business owner sees the message content and when it came in. They do not see your phone number, your name, or any identifying information. As far as the owner knows, an anonymous customer sent a message through Hotline.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can the business see my personal phone number? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Your message travels through the Hotline system number, not directly from your phone to theirs. The owner's alert shows the message text and timestamp only. Your personal number is never displayed to the business, not in the alert, not in any reply thread, not anywhere in their interface.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Does the business owner have my contact info after I text? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. The owner has no way to contact you outside of Hotline unless you choose to share your information in the message itself. If the owner replies using the REPLY command, that message comes back to you through the Hotline number, keeping both sides anonymous throughout the conversation.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Who else can see my message besides the owner? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Any alert recipients the owner has added, such as a manager or business partner, will receive the same alert text. None of them see your phone number. Messages are stored in Hotline's system for logging. They are not shared with third parties or used for marketing.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Do customers ever see my personal cell number? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Never. All customer-facing messages, including auto-replies and any replies you send using the REPLY command, go out from the Hotline number. Customers see Hotline as the sender at all times. Your personal cell is not involved in any part of the customer-facing flow.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What number do my alerts come from? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Your alerts come from the Hotline system number. Save it as "Hotline Alerts" in your contacts so you recognize it. That same number is the one customers text in to, and the one your replies go out from. Everything runs through one shared number, which is what keeps both sides private.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can customers reply back to me directly? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>If a customer replies to their auto-response, that message routes back to you through Hotline. It does not create a direct SMS thread between their phone and yours. The conversation stays inside the Hotline system on both ends. Neither party ever has direct access to the other's personal number.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Is my business data shared with anyone? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Your business name, phone number, and incoming message data are used only to operate the service. Owner data is never sold or shared with advertisers or third parties.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">How do I delete my account? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Email Connect@HotlineTXT.com and we'll delete your account and associated data. You can also just stop using the service and let your trial or subscription lapse. Nothing is retained after deletion.</p></div>
</div>

</div>

<div class="article-cta"><h3>Ready to get started?</h3><a href="https://hotlinetxt.com/signup" class="cta-btn">Sign up free &rarr;</a></div>
<a href="/resources" class="back-link">&larr; Back to resources</a>
</div>
""" + _ARTICLE_FOOT + """
</body>
<script>
function toggle(btn){
  var item=btn.parentElement;
  var isOpen=item.classList.contains('open');
  document.querySelectorAll('.faq-item.open').forEach(function(el){el.classList.remove('open')});
  if(!isOpen)item.classList.add('open');
}
</script>
</html>"""


RESOURCES_ARTICLE_1_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Why your business needs a direct customer line \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>""" + _ARTICLE_CSS + """</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<div class="breadcrumb"><a href="/resources">&larr; Resources</a> &nbsp;/ 01 &mdash; Strategy</div>
<header class="ah">
<h1>Why your business needs a direct customer line (and why Hotline is the easiest way to run one)</h1>
<div class="ameta"><span>Strategy</span><span>3 min read</span></div>
</header>
<article>

<p class="lead">Your staff won't always tell you what's wrong. Your customers will, if you give them a channel to do it.</p>

<p>Think about the last time something went sideways in your business and you found out too late. The bathroom that went hours without being cleaned. The staff member who called out and nobody covered the floor. The card reader that stopped working during the dinner rush.</p>

<p>Someone in your building knew. They just didn't tell you. Or they told a coworker. Or they pulled out their phone and left a one-star review instead.</p>

<h2>The gap between what happens and what you know</h2>

<p>Every business has this gap. Problems happen at the floor level. Owners operate above it. The information that travels between the two gets filtered by time, by staff who don't want to deliver bad news, and by systems that only catch things after the fact.</p>

<p>That gap closes when customers have a direct line to you, in the moment, while the problem is still fixable.</p>

<p>Not a survey. Not a comment card. A text message that reaches you in real time.</p>

<h2>Why customers actually use it</h2>

<p>Most unhappy customers don't complain to your face. It feels confrontational. They'd rather just leave and vent somewhere else later.</p>

<p>Texting is different. It's low friction, low stakes, and anonymous enough that people actually do it. Give someone a QR code and a reason to scan it, and you'll hear things you'd never hear otherwise.</p>

<p>The business that knows first is the business that can fix it first.</p>

<h2>What happens with those messages</h2>

<p>Messages aren't just collected. Each one is read, classified by urgency, and you only get bothered when something actually needs your attention.</p>

<p>A customer complaining that the music is too loud? that one gets logged quietly. A customer texting that your front door is locked and there's a line outside? It texts you immediately.</p>

<p>You set the threshold. You get the signal. The noise stays out of your way.</p>

<div class="brand-block">
<h2>The fast version</h2>
<p>Sign up, get a QR code, post it. Customers scan and text. Every message gets read, noise is filtered out, and you only get alerted when something needs your attention. Respond by text. Done.</p>
</div>

<h2>The risk if you skip it</h2>

<p>Without it, problems compound. One bad shift becomes a pattern. One broken machine sits broken for a week because nobody flagged it. One frustrated customer turns into ten reviews you never saw coming.</p>

<p>You can't fix what you don't know about. you will know.</p>

""" + _ARTICLE_CTA + """
<a href="/resources" class="back-link">&larr; Back to resources</a>
</article>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""


RESOURCES_ARTICLE_2_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Where to put your QR code \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>""" + _ARTICLE_CSS + """</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<div class="breadcrumb"><a href="/resources">&larr; Resources</a> &nbsp;/ 02 &mdash; Setup</div>
<header class="ah">
<h1>Where to put your QR code so customers actually use it</h1>
<div class="ameta"><span>Setup</span><span>3 min read</span></div>
</header>
<article>

<p class="lead">The QR code only works if a customer sees it at the right moment. That moment is when the problem is in front of them, not after they've already walked out.</p>

<p>Most owners post a sign near the entrance and call it done. That's the least effective spot. By the time someone's at the exit, the problem is behind them. They're already deciding whether to leave a review.</p>

<p>Put it at the problem, not at the door.</p>

<h2>Physical placements that work</h2>

<p>Think about where things break down in your business. Then put it there.</p>

<ul>
<li>Inside bathroom stalls, at eye level</li>
<li>On equipment that tends to break or jam</li>
<li>Tables and tent cards in restaurants</li>
<li>Near self-serve stations and kiosks</li>
<li>Fitting rooms</li>
<li>Hotel rooms and short-term rentals, near the TV or on the welcome card</li>
<li>Locker rooms and shared facilities</li>
</ul>

<p>Waterproof sticker stock for bathrooms and wet areas. Minimum 1.5 inches. Bigger in low light.</p>

<h2>Digital placements (most owners skip these)</h2>

<p>Physical signs catch customers mid-problem. Digital placements catch them after, when they're about to write a review. You want both.</p>

<ul>
<li>Wi-Fi login page, captive portal screen</li>
<li>Order confirmation emails and SMS</li>
<li>Digital and paper receipts</li>
<li>Booking confirmation pages</li>
<li>Website footer</li>
<li>Auto-reply on your main business phone number</li>
<li>Google Business profile</li>
<li>Instagram bio</li>
<li>Loyalty program welcome message</li>
<li>To-go bags and packaging</li>
</ul>

<p>The Wi-Fi login page is underrated. Every customer who connects has their phone in their hand and is already looking at a screen. That's a good time to remind them you're reachable.</p>

<h2>What to write next to it</h2>

<p>Keep it to one line. Tell them what happens when they scan.</p>

<div class="sample">"Something wrong? Text us. Owner reads every message."</div>
<div class="sample">"Issue with your visit? Let us know before you leave."</div>
<div class="sample">"Staff not around? Something broken? Scan to text us."</div>

<p>Avoid "feedback survey" and "rate your experience." Those sound like homework. Nobody scans homework.</p>

<h2>The pattern to follow</h2>

<p>Physical placement at the place where problems happen. Digital placement everywhere customers go after they leave. Both feed Hotline the same way. One catches the issue live. The other catches the customer before they write about it publicly.</p>

<p>Start with two or three spots and add more as you figure out where complaints tend to originate. the patterns become clear over time.</p>

""" + _ARTICLE_CTA + """
<a href="/resources" class="back-link">&larr; Back to resources</a>
</article>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""


RESOURCES_ARTICLE_3_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>How to respond to alerts without burning out \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>""" + _ARTICLE_CSS + """</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<div class="breadcrumb"><a href="/resources">&larr; Resources</a> &nbsp;/ 03 &mdash; Operations</div>
<header class="ah">
<h1>How to respond to alerts without burning out</h1>
<div class="ameta"><span>Operations</span><span>3 min read</span></div>
</header>
<article>

<p class="lead">Every single message that comes in gets handled. You only hear about the ones that actually need you.</p>

<p>That distinction matters. Most owners brace for a flood of notifications and then realize the opposite is true. Every incoming message is read, each customer gets an automatic response with the right tone, and everything is filtered before it reaches you. Compliments, questions, minor complaints, spam, all of it gets processed without you lifting a finger.</p>

<p>What makes it through to your phone is a short list: real operational problems and emergencies. That's the job you actually need to do.</p>

<h2>What the AI handles so you don't have to</h2>

<p>Every customer who texts in gets an immediate auto-reply. That reply is written based on the type of message. An emergency gets a response telling the customer to call 911. A complaint gets an empathetic acknowledgment. A compliment gets a warm thank you. A question gets a prompt to wait for a follow-up.</p>

<p>You never write those responses. Those go out automatically. By the time you see an alert, the customer has already heard back from someone.</p>

<div class="callout">
<div class="callout-label">What this means in practice</div>
<p>If 20 customers text in on a busy Saturday, 20 responses go out automatically. You might get one alert. Maybe none. The rest is handled.</p>
</div>

<h2>When you do get an alert, speed matters</h2>

<p>The alerts that reach you are real. Equipment down, no staff on the floor, a safety issue. These need a human response, and fast.</p>

<p>The customer has already received an automated acknowledgment from Hotline. Your job now is to actually fix the thing or get someone who can.</p>

<ul>
<li>Read the alert and decide: can you handle it, or do you need to call someone?</li>
<li>If you can act, act. Then text OK to Hotline to close the alert.</li>
<li>If you need to delegate, forward the summary to whoever is on the floor.</li>
<li>Use REPLY if the customer needs to hear directly from you.</li>
</ul>

<p>You don't need to be in front of a computer. The whole loop happens in your text messages.</p>

<h2>Don't overpromise in your replies</h2>

<p>When you reply to a customer directly, keep it simple. Acknowledge that you've seen it, confirm someone is on it. That's enough.</p>

<p>"We're looking into it" is clean. "We'll comp your next visit" said in a moment of stress is a commitment you now have to track and honor. Keep replies short and factual until you know what you're dealing with.</p>

<div class="callout">
<div class="callout-label">Safe reply template</div>
<p>Acknowledge. Confirm you've seen it. Tell them someone is on it. Stop there.</p>
</div>

<h2>Use the pattern to fix the root cause</h2>

<p>If the same issue keeps coming through, the pattern is telling you something. Three alerts about the same machine, the same bathroom, the same shift gap? That's not noise. That's a pattern.</p>

<p>Text LIST to see recent flagged alerts together. The fix usually becomes obvious when you see them grouped.</p>

<h2>Protect your own time</h2>

<p>Text QUIET 2H to silence non-emergency alerts for a set window. Text PAUSE to stop them until you're ready. Tier 1 emergencies always get through regardless of your settings.</p>

<p>The whole system is built to stay out of your way. The AI runs constantly in the background so you don't have to. When it needs you, it will find you.</p>

""" + _ARTICLE_CTA + """
<a href="/resources" class="back-link">&larr; Back to resources</a>
</article>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""


@app.get("/resources")
def resources_page(): _ensure_init(); return Response(content=RESOURCES_HTML, media_type="text/html")

@app.get("/resources/faq")
def resources_faq(): _ensure_init(); return Response(content=RESOURCES_FAQ_HTML, media_type="text/html")

@app.get("/resources/why-you-need-a-hotline")
def resources_article_1(): _ensure_init(); return Response(content=RESOURCES_ARTICLE_1_HTML, media_type="text/html")

@app.get("/resources/where-to-put-your-qr")
def resources_article_2(): _ensure_init(); return Response(content=RESOURCES_ARTICLE_2_HTML, media_type="text/html")

@app.get("/resources/responding-to-alerts")
def resources_article_3(): _ensure_init(); return Response(content=RESOURCES_ARTICLE_3_HTML, media_type="text/html")


# --- Signup page ---
# ============================================================
# TWILIO COMPLIANCE NOTE — DO NOT REMOVE OR MODIFY opt-in block
# The SMS opt-in checkbox below (id="f-optin") is required for
# Twilio A2P 10DLC and toll-free verification (error 30445).
# It must remain: unchecked by default, required before submit,
# and include the exact disclosure text with STOP/HELP/rates.
# Removing or pre-checking this checkbox will cause Twilio
# campaign registration to fail. — Last reviewed: 2025
# ============================================================
SIGNUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign Up \u2014 Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.wrap{max-width:480px;margin:0 auto;padding:24px}
h1{font-size:24px;font-weight:700;margin-bottom:8px}
.sub{font-size:15px;color:#888;margin-bottom:24px}
.card{background:#fff;border:1px solid #e0e0dc;border-radius:14px;padding:28px;box-shadow:0 4px 20px rgba(0,0,0,0.04)}
.trial{background:#fff7ed;border:1px solid #fed7aa;color:#c2410c;padding:10px 16px;border-radius:8px;font-size:14px;font-weight:500;margin-bottom:20px;text-align:center}
label{display:block;font-size:13px;font-weight:500;color:#888;margin-bottom:4px;margin-top:14px}label:first-of-type{margin-top:0}
input[type=text],input[type=tel],input[type=email],input[type=url]{width:100%;padding:12px 14px;background:#fafaf8;border:1px solid #e0e0dc;border-radius:8px;font-size:16px;color:#1a1a1a;font-family:inherit}input::placeholder{color:#bbb}input:focus{outline:none;border-color:#ea580c}
.btn{width:100%;padding:14px;background:#ea580c;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:700;cursor:pointer;margin-top:20px;font-family:inherit}.btn:hover{background:#dc2626}.btn:disabled{opacity:0.4}
.result{padding:14px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;line-height:1.5;display:none}.ok{background:#f0fdf4;color:#166534;border:1px solid #bbf7d0}.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.spinner{display:inline-block;width:16px;height:16px;border:2.5px solid #fff;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:6px}@keyframes spin{to{transform:rotate(360deg)}}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:32px 0 0}
.step{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.step-num{width:28px;height:28px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:13px;display:inline-flex;align-items:center;justify-content:center;margin-bottom:8px}
.step h3{font-size:14px;font-weight:600;margin-bottom:3px}.step p{font-size:12px;color:#888;line-height:1.4}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:40px}
/* --- TWILIO COMPLIANCE: opt-in disclosure styles --- DO NOT REMOVE --- */
.optin-wrap{display:flex;align-items:flex-start;gap:10px;margin-top:18px;padding:14px 16px;background:#f8f8f6;border:1px solid #e0e0dc;border-radius:8px}
.optin-wrap input[type=checkbox]{width:18px;height:18px;min-width:18px;margin-top:2px;accent-color:#ea580c;cursor:pointer}
.optin-wrap label{font-size:12px;color:#555;line-height:1.55;margin:0;font-weight:400}
.optin-wrap label a{color:#ea580c;text-decoration:underline}
.optin-err{display:none;color:#991b1b;font-size:12px;margin-top:6px}
/* --- END TWILIO COMPLIANCE STYLES --- */
@media(max-width:500px){.steps{grid-template-columns:1fr}}
</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<h1>Get your QR code</h1>
<p class="sub">No app. No software. No training required. Sign up in 30 seconds and get your print-ready sign instantly.</p>
<div class="card">
<div class="trial">14-day free trial &middot; No credit card required</div>
<div style="text-align:center;font-size:13px;color:#888;margin:-8px 0 16px">Then $19.99/month. Cancel anytime.</div>
<div class="result" id="result"></div>
<label>Business name</label><input type="text" id="f-name" placeholder="Joe's Coffee">
<label>Your cell phone</label><input type="tel" id="f-phone" placeholder="(727) 555-1234">
<label>Partner or manager phone (optional)</label><input type="tel" id="f-phone2" placeholder="(727) 555-5678">
<label>Email (for digest reports)</label><input type="email" id="f-email" placeholder="you@example.com">
<label>Business website (optional)</label><input type="url" id="f-url" placeholder="https://joescoffee.com">
<label>Business zip code</label><input type="text" id="f-zip" placeholder="78745" maxlength="5" pattern="[0-9]{5}" inputmode="numeric" required>


<!-- ============================================================
     TWILIO COMPLIANCE — SMS OPT-IN DISCLOSURE — DO NOT REMOVE
     Required for A2P 10DLC and toll-free verification (30445).
     Checkbox must be: unchecked by default, required to submit.
     Disclosure text, STOP/HELP instructions, and policy links
     must remain intact and unmodified. — Last reviewed: 2025
     ============================================================ -->
<div class="optin-wrap">
  <input type="checkbox" id="f-optin">
  <label for="f-optin">By checking this box, you agree to receive recurring SMS alerts from Hotline (the Hotline business alert service). Msg &amp; data rates may apply. Message frequency varies. Reply <strong>STOP</strong> to cancel, <strong>HELP</strong> for help. View our <a href="/terms" target="_blank">Terms of Service</a> and <a href="/privacy" target="_blank">Privacy Policy</a>.</label>
</div>
<div class="optin-err" id="optin-err">&#9888; You must agree to receive SMS messages to continue.</div>
<!-- ============================================================
     END TWILIO COMPLIANCE BLOCK
     ============================================================ -->

<button class="btn" id="f-btn" onclick="signup()">Get my QR code &rarr;</button>
</div>
<div class="steps">
<div class="step"><div class="step-num">1</div><h3>Sign up</h3><p>Get your QR code and sign in seconds</p></div>
<div class="step"><div class="step-num">2</div><h3>Display it</h3><p>Print your sign and post it in your business</p></div>
<div class="step"><div class="step-num">3</div><h3>Get alerts</h3><p>Customers scan, AI filters, you get alerted</p></div>
</div>
</div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa">Instagram</a></footer>
<script>
async function signup(){
  const name=document.getElementById('f-name').value.trim();
  let phone=document.getElementById('f-phone').value.trim().replace(/[\\s\\-\\(\\)]/g,'');
  let phone2=document.getElementById('f-phone2').value.trim().replace(/[\\s\\-\\(\\)]/g,'');
  const email=document.getElementById('f-email').value.trim();
  const url=document.getElementById('f-url').value.trim();
  const zip=document.getElementById('f-zip').value.trim();
  const res=document.getElementById('result');
  const btn=document.getElementById('f-btn');

  // --- TWILIO COMPLIANCE: validate opt-in checkbox --- DO NOT REMOVE ---
  const optinChecked=document.getElementById('f-optin').checked;
  const optinErr=document.getElementById('optin-err');
  if(!optinChecked){optinErr.style.display='block';optinErr.scrollIntoView({behavior:'smooth',block:'nearest'});return;}
  optinErr.style.display='none';
  // --- END TWILIO COMPLIANCE VALIDATION ---

  if(!phone.startsWith('+')){if(phone.startsWith('1')&&phone.length===11)phone='+'+phone;else if(phone.length===10)phone='+1'+phone;else{res.className='result err';res.style.display='block';res.textContent='Please enter a valid US phone number.';return}}
  if(phone2&&!phone2.startsWith('+')){if(phone2.startsWith('1')&&phone2.length===11)phone2='+'+phone2;else if(phone2.length===10)phone2='+1'+phone2}
  if(!name){res.className='result err';res.style.display='block';res.textContent='Please enter your business name.';return}
  if(!zip||!/^\d{5}$/.test(zip)){res.className='result err';res.style.display='block';res.textContent='Please enter a valid 5-digit zip code.';return}
  btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Setting up...';res.style.display='none';
  try{const r=await fetch('/signup/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,phone2,email,website_url:url,zip})});const d=await r.json();
  if(d.success){
    if(d.waitlisted){res.className='result ok';res.innerHTML="<strong>You're on the list!</strong><br><br>We'll text you as soon as your account is ready.";}
    else{res.className='result ok';res.innerHTML='<strong>You are live!</strong><br><br>Check your texts for your sign PDF and QR code image.<br><br>Code: <strong>'+d.business_code+'</strong><br><a href="'+d.sign_url+'" target="_blank" style="color:#ea580c">Download your sign →</a>';}
    res.style.display='block';btn.textContent='Done!'}
  else{res.className='result err';res.textContent=d.error||'Something went wrong.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my number &rarr;'}}
  catch(e){res.className='result err';res.textContent='Connection error.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my number &rarr;'}
}
</script></body></html>"""

@app.get("/signup")
def signup_page(): _ensure_init(); return Response(content=SIGNUP_HTML, media_type="text/html")


# --- Privacy Policy page ---
PRIVACY_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy &mdash; Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.wrap{max-width:720px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:28px;font-weight:700;margin-bottom:6px}
.meta{font-size:13px;color:#aaa;margin-bottom:32px}
h2{font-size:17px;font-weight:700;margin:28px 0 8px}
p,li{font-size:15px;line-height:1.7;color:#333}
ul{padding-left:20px;margin-top:6px}
ul li{margin-bottom:4px}
.highlight{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:16px 20px;margin:24px 0}
.highlight p{color:#7c2d12;font-weight:500}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:40px}
footer a{color:#aaa}
</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<h1>Privacy Policy</h1>
<p class="meta">Effective date: January 1, 2025 &nbsp;&middot;&nbsp; HotlineTXT.com</p>

<div class="highlight"><p>&#128241; Hotline is an SMS-based customer feedback system. Customers text a business number and business owners receive alerts. This policy explains how we handle that data.</p></div>

<h2>1. Who We Are</h2>
<p>Hotline is operated by HotlineTXT.com (&ldquo;we,&rdquo; &ldquo;our,&rdquo; or &ldquo;us&rdquo;). We provide SMS-based customer alerting services to small businesses. For questions, contact us at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>

<h2>2. Information We Collect</h2>
<p>We collect the following information when you use Hotline:</p>
<ul>
<li><strong>Customer SMS messages:</strong> The text content of messages sent to a Hotline business number, along with the sender&rsquo;s phone number and timestamp.</li>
<li><strong>Business owner information:</strong> Business name, owner phone number, optional email address, and optional website URL provided during signup.</li>
<li><strong>Usage data:</strong> Message tiers, categories, sentiment classifications, and acknowledgment records generated by our AI system.</li>
</ul>

<h2>3. How We Use Your Information</h2>
<p>We use collected information solely to operate the Hotline service:</p>
<ul>
<li>Classify and route customer messages to business owners via SMS</li>
<li>Send alert notifications to registered business owner phone numbers</li>
<li>Generate weekly digest summaries for business owners (if opted in)</li>
<li>Maintain message logs accessible to the business owner via SMS commands</li>
</ul>
<p>We do <strong>not</strong> sell, rent, or share your personal information with third parties for marketing purposes.</p>

<h2>4. SMS Messaging and Opt-In</h2>
<p><strong>Business owners:</strong> By signing up for Hotline, you consent to receive SMS alerts and notifications from your assigned Hotline number. You may opt out at any time by texting <strong>STOP</strong> to your Hotline number. Standard message and data rates from your carrier may apply.</p>
<p><strong>Customers texting a business:</strong> When you text a Hotline-powered business number, your message and phone number are stored and forwarded to the business owner. You are not opted in to any marketing list. The business may reply to your message directly via SMS.</p>

<h2>5. Data Retention</h2>
<p>Customer messages and associated data are stored for up to 90 days by default. Business owner accounts and associated message history are retained for the duration of the account. You may request deletion by contacting <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>

<h2>6. Third-Party Services</h2>
<p>Hotline uses the following third-party services to operate:</p>
<ul>
<li><strong>Twilio:</strong> SMS sending and receiving. Twilio handles phone number provisioning and message delivery. See <a href="https://www.twilio.com/legal/privacy" target="_blank">Twilio&rsquo;s Privacy Policy</a>.</li>
<li><strong>Anthropic:</strong> AI message classification. Customer message text is sent to Anthropic&rsquo;s API for analysis. See <a href="https://www.anthropic.com/privacy" target="_blank">Anthropic&rsquo;s Privacy Policy</a>.</li>
</ul>

<h2>7. Security</h2>
<p>We use industry-standard security practices to protect your data. However, no method of transmission over the internet or electronic storage is 100% secure. We encourage you to contact us immediately at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> if you suspect any unauthorized access.</p>

<h2>8. Children&rsquo;s Privacy</h2>
<p>Hotline is not directed at children under 13. We do not knowingly collect personal information from children under 13. If you believe a child has provided us with personal information, please contact us.</p>

<h2>9. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time. We will notify registered business owners of material changes via SMS or email. Continued use of Hotline after changes constitutes acceptance of the updated policy.</p>

<h2>10. Contact</h2>
<p>For privacy questions, data deletion requests, or to opt out of SMS communications, contact:</p>
<p style="margin-top:8px"><strong>Email:</strong> <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a><br>
<strong>Website:</strong> <a href="https://HotlineTXT.com">HotlineTXT.com</a></p>
</div>
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener">Instagram</a></footer>
</body></html>"""

@app.get("/privacy")
def privacy_page(): _ensure_init(); return Response(content=PRIVACY_HTML, media_type="text/html")


# --- Terms of Service page ---
TERMS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terms of Service &mdash; Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.wrap{max-width:720px;margin:0 auto;padding:32px 24px 64px}
h1{font-size:28px;font-weight:700;margin-bottom:6px}
.meta{font-size:13px;color:#aaa;margin-bottom:32px}
h2{font-size:17px;font-weight:700;margin:28px 0 8px}
p,li{font-size:15px;line-height:1.7;color:#333}
ul{padding-left:20px;margin-top:6px}
ul li{margin-bottom:4px}
.highlight{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:16px 20px;margin:24px 0}
.highlight p{color:#7c2d12;font-weight:500}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc;margin-top:40px}
footer a{color:#aaa}
</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<h1>Terms of Service</h1>
<p class="meta">Effective date: January 1, 2025 &nbsp;&middot;&nbsp; HotlineTXT.com</p>

<div class="highlight"><p>&#128241; By using Hotline, you agree to these terms. Hotline provides SMS-based customer alerting for small businesses. Please read these terms carefully.</p></div>

<h2>1. Acceptance of Terms</h2>
<p>By signing up for or using Hotline (&ldquo;the Service&rdquo;) operated by HotlineTXT.com, you agree to be bound by these Terms of Service. If you do not agree, do not use the Service.</p>

<h2>2. Description of Service</h2>
<p>Hotline is an SMS-based system that allows customers to send text messages to a business phone number. The Service uses AI to classify incoming messages and notifies registered business owners of important issues via SMS. Business owners interact with the Service entirely via SMS commands.</p>

<h2>3. SMS Messaging &mdash; Opt-In and Opt-Out</h2>
<p><strong>Business owners:</strong> By completing signup and providing your phone number, you expressly consent to receive SMS messages from Hotline, including:</p>
<ul>
<li>Alert notifications when customers send flagged messages</li>
<li>Weekly digest summaries (if enabled)</li>
<li>Onboarding and setup messages</li>
</ul>
<p>Message frequency varies based on customer activity. Standard message and data rates may apply.</p>
<p>To opt out of SMS alerts at any time, text <strong>STOP</strong> to your assigned Hotline number. You will receive one confirmation message and no further messages will be sent. Text <strong>HELP</strong> for assistance or contact <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>
<p><strong>Customers:</strong> Customers who text a Hotline business number are not opting in to any marketing messages. Their messages are forwarded to the relevant business owner only.</p>

<h2>4. Permitted Use</h2>
<p>You may use Hotline only for lawful business purposes. You agree not to:</p>
<ul>
<li>Use Hotline to send spam, unsolicited messages, or harassing communications</li>
<li>Impersonate any person or business</li>
<li>Use the Service in violation of any applicable law or regulation</li>
<li>Attempt to circumvent any security or rate-limiting measures</li>
<li>Use Hotline for any purpose that violates Twilio&rsquo;s Acceptable Use Policy</li>
</ul>

<h2>5. Account Responsibilities</h2>
<p>You are responsible for keeping your phone number and account information current and accurate. You are responsible for all activity associated with your Hotline account. Notify us immediately at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> if you suspect unauthorized use.</p>

<h2>6. Pricing and Billing</h2>
<p>Hotline offers a 14-day free trial with no credit card required. After the trial period, continued use of the Service is subject to the then-current pricing listed on <a href="https://HotlineTXT.com">HotlineTXT.com</a>. We reserve the right to change pricing with reasonable notice.</p>

<h2>7. Termination</h2>
<p>Either party may terminate the Service at any time. We reserve the right to suspend or terminate accounts that violate these Terms. Upon termination, your data may be deleted after 90 days.</p>

<h2>8. Disclaimer of Warranties</h2>
<p>Hotline is provided &ldquo;as is&rdquo; without warranty of any kind. We do not guarantee that the Service will be uninterrupted, error-free, or that alerts will be delivered within any specific timeframe. AI message classification is probabilistic and may not be 100% accurate.</p>

<h2>9. Limitation of Liability</h2>
<p>To the maximum extent permitted by law, HotlineTXT.com shall not be liable for any indirect, incidental, special, or consequential damages arising from your use of the Service, including any missed or delayed alerts.</p>

<h2>10. Governing Law</h2>
<p>These Terms are governed by the laws of the United States. Any disputes shall be resolved through binding arbitration or in the courts of applicable jurisdiction.</p>

<h2>11. Changes to Terms</h2>
<p>We may update these Terms from time to time. We will notify registered users of material changes via SMS or email. Continued use after changes constitutes acceptance.</p>

<h2>12. Contact</h2>
<p>For questions about these Terms, contact:</p>
<p style="margin-top:8px"><strong>Email:</strong> <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a><br>
<strong>Website:</strong> <a href="https://HotlineTXT.com">HotlineTXT.com</a></p>
</div>
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener">Instagram</a></footer>
</body></html>"""

@app.get("/terms")
def terms_page(): _ensure_init(); return Response(content=TERMS_HTML, media_type="text/html")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe sends events here after payment succeeds/fails."""
    _ensure_init()
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    # Verify signature
    if STRIPE_WEBHOOK_SECRET:
        try:
            # Manual HMAC verification (no stripe SDK required)
            import hmac as _hmac, hashlib as _hashlib
            parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
            ts = parts.get("t", "")
            v1 = parts.get("v1", "")
            signed = f"{ts}.{payload.decode()}"
            expected = _hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed.encode(), _hashlib.sha256).hexdigest()
            if not _hmac.compare_digest(expected, v1):
                logger.warning("[STRIPE] Invalid webhook signature")
                return JSONResponse({"error": "Invalid signature"}, status_code=400)
        except Exception as e:
            logger.error(f"[STRIPE] Signature check failed: {e}")
            return JSONResponse({"error": "Signature error"}, status_code=400)

    try:
        event = json.loads(payload)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})
    customer_id = data.get("customer", "")
    logger.info(f"[STRIPE] Event: {event_type} customer={customer_id}")

    biz = get_business_by_stripe_customer(customer_id) if customer_id else None

    if event_type == "invoice.payment_succeeded":
        if biz:
            set_sub_status(biz["id"], "active")
            logger.info(f"[STRIPE] Payment succeeded — {biz['id']} set active")
            # Notify owner if they were previously blocked
            if (biz.get("sub_status") or "trialing") in ("expired", "past_due"):
                for p in get_alert_phones(biz):
                    send_sms(p, "\u2705 Payment received. Hotline alerts are active again.")

    elif event_type == "invoice.payment_failed":
        if biz:
            set_sub_status(biz["id"], "past_due")
            logger.info(f"[STRIPE] Payment failed — {biz['id']} set past_due")
            PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "")
            link_part = f"\nUpdate payment: {PAYMENT_LINK}" if PAYMENT_LINK else ""
            for p in get_alert_phones(biz):
                send_sms(p, f"\u26a0\ufe0f Hotline payment failed. Alerts may stop soon.{link_part}")

    elif event_type == "customer.subscription.updated":
        status_map = {"active": "active", "past_due": "past_due", "canceled": "canceled",
                      "unpaid": "past_due", "trialing": "trialing"}
        stripe_status = data.get("status", "")
        mapped = status_map.get(stripe_status, stripe_status)
        if biz and mapped:
            set_sub_status(biz["id"], mapped)
            logger.info(f"[STRIPE] Sub updated — {biz['id']} => {mapped}")

    elif event_type == "customer.subscription.deleted":
        if biz:
            set_sub_status(biz["id"], "canceled")
            logger.info(f"[STRIPE] Sub canceled — {biz['id']}")
            for p in get_alert_phones(biz):
                send_sms(p, "\u26d4 Hotline subscription canceled. Alerts are paused.")

    return JSONResponse({"received": True})


@app.post("/cron/trial-warnings")
def cron_trial_warnings():
    """Call daily via Vercel cron or external scheduler."""
    _ensure_init()
    n = send_trial_warnings()
    return {"warnings_sent": n}


@app.post("/signup/create")
async def signup_create(request_data:dict=None):
    _ensure_init()
    if not request_data: return {"error":"Missing data"}
    name = (request_data.get("name") or "").strip()
    phone = (request_data.get("phone") or "").strip()
    phone2 = (request_data.get("phone2") or "").strip()
    email = (request_data.get("email") or "").strip()
    website_url = (request_data.get("website_url") or "").strip()
    zip_code = (request_data.get("zip") or "").strip()
    if not name: return {"error":"Business name required"}
    if not phone or not phone.startswith("+"): return {"error":"Valid phone with country code required"}

    base = os.getenv("BASE_URL", "https://hotlinetxt.com")

    # Build business ID
    biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    with get_db() as c:
        if _fetchone(c,_q("SELECT id FROM businesses WHERE id=?"), (biz_id,)):
            biz_id = biz_id[:25]+"-"+datetime.now(timezone.utc).strftime("%H%M%S")

    extra = phone2 if phone2 and phone2.startswith("+") else ""
    business_code = create_business(biz_id, name, phone, "", extra_phones=extra, email=email, website_url=website_url, zip_code=zip_code)
    if not business_code:
        # Possibly duplicate — save to waitlist
        logger.warning(f"create_business failed for {name} ({phone}) — saving to waitlist")
        save_pending_signup(name, phone, phone2, email, website_url)
        ts = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")
        email_html = f"""<div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px">
          <h2 style="color:#ea580c;margin:0 0 16px">New Waitlist Signup</h2>
          <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr><td style="padding:8px 0;color:#888;width:120px">Name</td><td style="padding:8px 0;font-weight:600">{name}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Phone</td><td style="padding:8px 0;font-family:monospace">{phone}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Email</td><td style="padding:8px 0">{email or "—"}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Website</td><td style="padding:8px 0">{website_url or "—"}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Time</td><td style="padding:8px 0">{ts}</td></tr>
          </table>
          <p style="margin:24px 0 0;font-size:13px;color:#aaa">View at <a href="https://hotlinetxt.com/admin" style="color:#ea580c">hotlinetxt.com/admin</a></p>
        </div>"""
        send_email("Connect@HotlineTXT.com", f"New waitlist signup: {name}", email_html)
        return {"success":True,"waitlisted":True,"name":name,"owner_phone":phone}

    # Send welcome + asset links
    welcome = WELCOME_MSG.format(name=name)
    send_sms(phone, welcome)
    if extra: send_sms(extra, welcome)

    pref_prompt = (
        "One quick setup \u2014 what alerts do you want?\n\n"
        "Reply TIER2 \u2014 Critical only (equipment failures, no staff, safety issues)\n"
        "Reply TIER3 \u2014 Everything including complaints & feedback\n\n"
        "You can change this anytime by texting ALERTS."
    )
    send_sms(phone, pref_prompt)
    if extra: send_sms(extra, pref_prompt)

    asset_msg = (
        f"Your Hotline assets for {name}:\n"
        f"Print-ready sign: {base}/signs/{business_code}.pdf\n"
        f"Plain QR image (custom signage): {base}/qr/{business_code}.png"
    )
    send_sms(phone, asset_msg)
    if extra: send_sms(extra, asset_msg)

    logger.info(f"Signup: {name} ({biz_id}) code={business_code}")

    # Notify admin
    ts = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")
    email_html = f"""<div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px">
      <h2 style="color:#ea580c;margin:0 0 16px">New Hotline Signup</h2>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr><td style="padding:8px 0;color:#888;width:140px">Business</td><td style="padding:8px 0;font-weight:600">{name}</td></tr>
        <tr><td style="padding:8px 0;color:#888">Phone</td><td style="padding:8px 0;font-family:monospace">{phone}</td></tr>
        <tr><td style="padding:8px 0;color:#888">Business Code</td><td style="padding:8px 0;font-family:monospace;font-weight:700;color:#ea580c">{business_code}</td></tr>
        <tr><td style="padding:8px 0;color:#888">Email</td><td style="padding:8px 0">{email or "—"}</td></tr>
        <tr><td style="padding:8px 0;color:#888">Website</td><td style="padding:8px 0">{website_url or "—"}</td></tr>
        <tr><td style="padding:8px 0;color:#888">Time</td><td style="padding:8px 0">{ts}</td></tr>
      </table>
      <p style="margin:16px 0 0;font-size:13px"><a href="{base}/signs/{business_code}.pdf" style="color:#ea580c">Sign PDF</a> &nbsp;|&nbsp; <a href="{base}/qr/{business_code}.png" style="color:#ea580c">QR PNG</a></p>
    </div>"""
    send_email("Connect@HotlineTXT.com", f"New signup: {name} ({business_code})", email_html)

    return {"success":True,"business_id":biz_id,"name":name,"owner_phone":phone,"business_code":business_code,
            "sign_url":f"{base}/signs/{business_code}.pdf","qr_url":f"{base}/qr/{business_code}.png"}
