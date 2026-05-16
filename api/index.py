"""
Hotline — SMS Alert System. Single-file Vercel deployment.
"""
import os, re, json, logging, io
from datetime import datetime, timezone, timedelta
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
        "CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_phone)",
        "CREATE INDEX IF NOT EXISTS idx_msg_biz ON messages(business_id, tier, acknowledged)",
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
                         ("stripe_sub_id","\'\'")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE businesses ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
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

def create_business(biz_id, name, owner_phone, twilio_number="", extra_phones="", email="", website_url="", business_code=""):
    now = datetime.now(timezone.utc).isoformat()
    all_phones = ",".join([owner_phone] + [p.strip() for p in extra_phones.split(",") if p.strip()]) if extra_phones else owner_phone
    website_info = scrape_website_info(website_url) if website_url else ""
    if not business_code:
        business_code = _gen_business_code()
    trial_end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    try:
        with get_db() as c:
            _execute(c, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,email,website_url,website_info,twilio_number,business_code,trial_ends_at,sub_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
                     (biz_id, name, owner_phone, all_phones, email or "", website_url or "", website_info, twilio_number or "", business_code, trial_end, "trialing", now))
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
    with get_db() as c: return _fetchall(c, _q("SELECT * FROM messages WHERE business_id=? ORDER BY created_at DESC LIMIT ?"), (bid, limit))

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
- Tier 1: Urgent, direct. ALWAYS tell customer to call 911. NEVER say "we've contacted emergency services." You haven't.
- Tier 2: Professional, serious. Confirm issue type, say management notified. No exclamation marks. NEVER promise specific action ("we'll fix it", "we'll change that").
- Tier 3: Empathetic. Acknowledge frustration. Invite more details — gives customer an outlet, prevents public reviews. No exclamation marks.
- Tier 4 positive: Warm, friendly, use exclamation marks. Genuine appreciation.
- Tier 4 inquiry: NEVER answer factual questions about the business. Not hours, not address, not menu, not prices, not directions. Always say forwarded to management.

HARD RULES:
- NEVER fabricate business information.
- NEVER promise action will be taken. Business decides. You acknowledge and forward.
- NEVER claim to have contacted emergency services.
- NEVER ask follow-up questions for Tier 1 or 2. Just acknowledge and notify.
- For Tier 3 only, you MAY gently invite more detail.
- Keep auto_reply under 160 characters.
- Vary responses naturally. Don't repeat same template.

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

def _fmt_ts(iso):
    try: return datetime.fromisoformat(iso).strftime("%b %d %I:%M%p").replace(" 0"," ")
    except: return iso[:16]

def _fmt_phone_short(phone):
    d = _normalize_phone(phone).replace("+","")
    if len(d)==11 and d[0]=="1": d=d[1:]
    if len(d)==10: return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return phone

def handle_owner_command(text, business, sender_phone=""):
    bid = business["id"]
    raw = text.strip()
    cmd = raw.upper()

    # ── Reply mode (persisted in DB, survives restarts) ───────────────────────
    reply_mid = get_reply_mode(bid)
    if reply_mid:
        if cmd in {"CANCEL","NEVERMIND","STOP"}:
            clear_reply_mode(bid)
            return "Reply cancelled."
        clear_reply_mode(bid)
        msg = get_message_by_id(reply_mid)
        if msg:
            send_sms(msg["from_number"], raw)
            return f"Reply sent to customer."
        return "Could not find the original message."

    # ── OK #N — acknowledge a specific alert by ID ────────────────────────────
    is_ok_n = re.match(r"^OK\s+#?(\d+)$", cmd)
    if is_ok_n:
        target_id = int(is_ok_n.group(1))
        msg = get_message_by_id(target_id)
        if not msg or msg["business_id"] != bid: return f"Alert #{target_id} not found."
        if msg["acknowledged"]: return f"\u2705 Alert #{target_id} already acknowledged."
        mark_acknowledged(target_id)
        set_context(bid, target_id)
        others = [p for p in get_alert_phones(business) if _normalize_phone(p)[-10:] != _normalize_phone(sender_phone)[-10:]]
        short = _fmt_phone_short(sender_phone) if sender_phone else "someone"
        for p in others: send_sms(p, f"\u2705 Alert #{target_id} acknowledged by {short}.")
        return f"\u2705 Alert #{target_id} acknowledged.\n\"{msg['message_text'][:80]}\""

    # ── OK / ACK — with multi-alert disambiguation ────────────────────────────
    ack_words = {"OK","GOT IT","DONE","ON IT","ACK","YES"}
    is_thumbs = "\U0001f44d" in raw
    is_reaction_ack = any(cmd.startswith(w) for w in ["LIKED","LOVED","THUMBED UP"])
    if cmd in ack_words or is_thumbs or is_reaction_ack:
        all_flagged = get_recent_flagged(bid, 20)
        unacked = [m for m in all_flagged if not m["acknowledged"]]

        if not unacked: return "No unacknowledged alerts."

        if len(unacked) > 1:
            lines = [f"{len(unacked)} open alerts. Reply OK #N to acknowledge one:\n"]
            for m in unacked[:5]:
                lines.append(f"  #{m['id']} \u2014 {m['summary']} ({_fmt_ts(m['created_at'])})")
            lines.append("\nExample: OK 2")
            return "\n".join(lines)

        # Exactly one — ack it and echo back what it was
        msg = unacked[0]
        mark_acknowledged(msg["id"])
        set_context(bid, msg["id"])
        others = [p for p in get_alert_phones(business) if _normalize_phone(p)[-10:] != _normalize_phone(sender_phone)[-10:]]
        short = _fmt_phone_short(sender_phone) if sender_phone else "someone"
        for p in others: send_sms(p, f"\u2705 Alert #{msg['id']} acknowledged by {short}.")
        return f"\u2705 Alert #{msg['id']} acknowledged.\n\"{msg['message_text'][:80]}\""

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

    if cmd == "HELP":
        return ("Commands:\nDETAILS \u2014 View latest alert\nOK \u2014 Close/acknowledge alert\n"
                "OK 2 \u2014 Acknowledge specific alert\nREPLY \u2014 Reply to customer (privately)\n"
                "SNOOZE \u2014 Revisit latest alert in 1hr\nSNOOZE 2H \u2014 Revisit in X hours\n"
                "LIST \u2014 Flagged issues\nLIST ALL \u2014 All messages\n"
                "STATUS \u2014 Alert status + preference\n"
                "ALERTS \u2014 View/change alert level\n"
                "TIER2 \u2014 Critical issues only\nTIER3 \u2014 Add reputation alerts\n"
                "QUIET 2H \u2014 Silence alerts for X hours\nPAUSE / RESUME\n"
                "DIGEST DAILY / WEEKLY\nBILLING \u2014 Subscription status\nHELP \u2014 This message")

    if cmd == "REPLY":
        ctx_id = get_context(bid)
        msg = get_message_by_id(ctx_id) if ctx_id else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg:
            recent = get_recent_flagged(bid, 1)
            msg = recent[0] if recent else None
        if not msg: return "No messages to reply to."
        set_reply_mode(bid, msg["id"])
        return f"Replying to customer re: \"{msg['message_text'][:60]}\"\nType your reply now, or CANCEL."

    if cmd == "DETAILS":
        ctx_id = get_context(bid)
        msg = get_message_by_id(ctx_id) if ctx_id else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg:
            recent = get_recent_flagged(bid, 1)
            msg = recent[0] if recent else None
        if not msg: return "No alerts on record."
        set_context(bid, msg["id"])
        ack = "\u2705 Acknowledged" if msg["acknowledged"] else "\u23f3 Pending"
        return (f"Alert #{msg['id']} \u2014 {ack}\nTime: {_fmt_ts(msg['created_at'])}\n"
                f"Category: {msg['category']}\n"
                f"Message: \"{msg['message_text']}\"\nReply OK to close, REPLY to respond, SNOOZE to revisit in 1hr.")

    if cmd == "LIST ALL":
        msgs = get_recent_all(bid, 5)
        if not msgs: return "No messages yet."
        icons = {1:"\U0001f6a8",2:"\u26a0\ufe0f",3:"\U0001f614",4:"\U0001f4ac"}
        lines = ["Last 5 messages:\n"]
        for m in msgs:
            ack_mark = " \u2705" if m["acknowledged"] else ""
            lines.append(f"{icons.get(m['tier'],chr(0x1f4ac))} #{m['id']} \u2014 {m['summary']} ({_fmt_ts(m['created_at'])}){ack_mark}")
        return "\n".join(lines)

    if cmd == "LIST":
        msgs = get_recent_flagged(bid, 5)
        if not msgs: return "No flagged issues."
        lines = ["Last 5 flagged:\n"]
        for m in msgs:
            icon = "\u2705" if m["acknowledged"] else "\u26a0\ufe0f"
            lines.append(f"{icon} #{m['id']} \u2014 {m['summary']} ({_fmt_ts(m['created_at'])})")
        lines.append("\nReply OK #N to acknowledge. DETAILS for full message.")
        return "\n".join(lines)

    if cmd == "STATUS":
        name = business.get("name") or bid
        if business.get("paused"): return f"\U0001f4f4 Alerts PAUSED for {name}.\nReply RESUME to turn back on."
        mu = business.get("muted_until")
        if mu:
            try:
                until = datetime.fromisoformat(mu)
                if until > datetime.now(timezone.utc):
                    mins = int((until - datetime.now(timezone.utc)).total_seconds()/60)
                    return f"\U0001f507 Muted for {mins//60}h {mins%60}m.\nReply RESUME to unmute."
            except: pass
        unacked = sum(1 for m in get_recent_flagged(bid, 20) if not m["acknowledged"])
        t3 = "on" if business.get("alert_tier3") else "off"
        unacked_str = f"\n{unacked} unacknowledged alert(s) \u2014 reply LIST" if unacked else ""
        t3_label = "Tier 2 + Tier 3 (all)" if business.get("alert_tier3") else "Tier 2 critical only"
        return f"\U0001f514 Alerts ON for {name}.\nAlert level: {t3_label}{unacked_str}\nReply ALERTS to change."

    if cmd == "ALERTS":
        t3 = "on (Tier 2 + Tier 3)" if business.get("alert_tier3") else "off (Tier 2 critical only)"
        return (f"\U0001f514 Alert level: {t3}\n\n"
                "Reply TIER2 \u2014 Critical issues only\n"
                "Reply TIER3 \u2014 Also get reputation/feedback alerts")
    if cmd in ("TIER2", "ALERTS CRITICAL"): set_alert_tier3(bid, False); return "\U0001f534 Critical only. You'll get Tier 2 (operations, equipment, staffing) and emergencies.\nReply TIER3 to also get reputation alerts."
    if cmd in ("TIER3", "ALERTS ALL"): set_alert_tier3(bid, True); return "\U0001f7e1 All alerts on. You'll now also get Tier 3 reputation/feedback messages.\nReply TIER2 to go back to critical only."

    if cmd == "PAUSE": set_paused(bid, True); return "\U0001f4f4 Alerts PAUSED. Reply RESUME to turn back on."
    if cmd == "RESUME": set_paused(bid, False); set_muted_until(bid, None); return "\U0001f514 Alerts resumed."
    if cmd == "DIGEST DAILY": set_digest_freq(bid, "daily"); return "\U0001f4e7 Digest set to daily."
    if cmd == "DIGEST WEEKLY": set_digest_freq(bid, "weekly"); return "\U0001f4e7 Digest set to weekly."

    if cmd.startswith("SNOOZE"):
        ctx_id = get_context(bid)
        msg = get_message_by_id(ctx_id) if ctx_id else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg: return "No active alert to snooze."
        m = re.match(r"SNOOZE\s+(\d+)\s*(H|HR|HRS|HOUR|HOURS|M|MIN|MINS|MINUTE|MINUTES)?", cmd)
        if m:
            amt = int(m.group(1)); unit = (m.group(2) or "H")[0]
            if unit == "M": delta = timedelta(minutes=max(1,min(1440,amt))); label = f"{amt}m"
            else: delta = timedelta(hours=max(1,min(72,amt))); label = f"{amt}h"
        else:
            delta = timedelta(hours=1); label = "1hr"
        snooze_until = datetime.now(timezone.utc) + delta
        set_context(bid, msg["id"])
        return f"\u23f0 Snoozed. Alert #{msg['id']} will remind you in {label}.\n\"{msg['message_text'][:60]}\""

    if cmd.startswith("QUIET"):
        m = re.match(r"QUIET\s+(\d+)\s*(H|HR|HRS|HOUR|HOURS|M|MIN|MINS|MINUTE|MINUTES)?", cmd)
        if not m: set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=1)); return "\U0001f507 Quiet for 1hr. Reply RESUME to unmute."
        amt = int(m.group(1)); unit = (m.group(2) or "H")[0]
        if unit=="M": amt=max(1,min(1440,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(minutes=amt)); return f"\U0001f507 Quiet for {amt}m. Reply RESUME to unmute."
        else: amt=max(1,min(72,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=amt)); return f"\U0001f507 Quiet for {amt}h. Reply RESUME to unmute."

    if cmd.startswith("MUTE"):
        m = re.match(r"MUTE\s+(\d+)\s*(H|HR|HRS|HOUR|HOURS|M|MIN|MINS|MINUTE|MINUTES)?", cmd)
        if not m: set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=1)); return "\U0001f507 Quiet for 1hr. Reply RESUME to unmute. (Tip: use QUIET 2H to set duration)"
        amt = int(m.group(1)); unit = (m.group(2) or "H")[0]
        if unit=="M": amt=max(1,min(1440,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(minutes=amt)); return f"\U0001f507 Quiet for {amt}m. Reply RESUME to unmute."
        else: amt=max(1,min(72,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=amt)); return f"\U0001f507 Quiet for {amt}h. Reply RESUME to unmute."

    if any(cmd.startswith(w) for w in ["EMPHASIZED","QUESTIONED","LAUGHED AT","DISLIKED"]): return ""
    return f"Unknown: \"{raw[:20]}\"\nReply HELP for commands."



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

    # --- Pending signups table ---
    pending = get_pending_signups()
    pending_rows = ""
    for p in pending:
        ts = p["created_at"][:16].replace("T"," ") + " UTC"
        pending_rows += f'<tr><td style="padding:12px 16px;font-weight:600">{p["name"]}</td><td style="padding:12px 16px;font-family:monospace;font-size:13px">{p["owner_phone"]}</td><td style="padding:12px 16px;font-size:13px;color:#555">{p["email"] or "—"}</td><td style="padding:12px 16px;font-size:13px;font-family:monospace;color:#ea580c;font-weight:600">—</td><td style="padding:12px 16px;font-size:12px;color:#999">{ts}</td></tr>'
    pending_count = len(pending)
    pending_badge = f' <span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;vertical-align:middle">{pending_count}</span>' if pending_count else ""
    if not pending_rows:
        pending_rows = '<tr><td colspan="5" style="padding:24px;text-align:center;color:#999">No pending signups.</td></tr>'

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
        rows += (
            f'<tr id="row-{bid}">'
            f'<td style="padding:12px 16px;font-weight:600">{b["name"]}<br><span style="font-size:11px;color:#aaa">{b.get("owner_phone","")}</span></td>'
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

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline Admin</title></head>
<body style="font-family:system-ui;margin:0;padding:24px;background:#f8f8f6">
<div style="max-width:960px;margin:0 auto">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:28px">
    <h1 style="font-size:24px;margin:0">Hotline Admin</h1>
    <a href="/admin/logout" style="font-size:13px;color:#888;text-decoration:none">Sign out</a>
  </div>

  <div id="toast" style="display:none;background:#166534;color:#fff;font-size:13px;padding:8px 14px;border-radius:6px;margin-bottom:16px"></div>

  <h2 style="font-size:16px;font-weight:700;margin:0 0 12px">Pending Signups{pending_badge}</h2>
  <p style="font-size:13px;color:#888;margin:0 0 12px">These leads signed up while Twilio provisioning was unavailable. Provision them manually once the service is live.</p>
  <div style="background:#fff;border:1px solid #e0e0dc;border-radius:10px;overflow-x:auto;margin-bottom:36px">
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <thead><tr style="background:#f5f5f0;border-bottom:1px solid #e0e0dc">
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Business</th>
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Phone</th>
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Email</th>
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Biz Code</th>
        <th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Signed Up</th>
      </tr></thead>
      <tbody>{pending_rows}</tbody>
    </table>
  </div>

  <h2 style="font-size:16px;font-weight:700;margin:0 0 12px">Active Businesses</h2>
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
<!-- Billing Modal -->
<div id="billing-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;align-items:center;justify-content:center">
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
document.getElementById("billing-modal").addEventListener("click",function(e){{if(e.target===this)closeBilling();}});
</script>
</body></html>'''
    return Response(content=html, media_type="text/html")


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


# ── Customer session cache ────────────────────────────────────────────────────
# Maps customer phone → (business_id, expiry_timestamp)
# Allows follow-up messages without a BC#### code to route correctly
_customer_sessions: dict = {}
SESSION_TTL_MINUTES = 30

def _set_customer_session(phone: str, business_id: str):
    expiry = datetime.now(timezone.utc) + timedelta(minutes=SESSION_TTL_MINUTES)
    _customer_sessions[phone] = (business_id, expiry)

def _get_customer_session(phone: str):
    """Return business_id if session exists and hasn't expired, else None."""
    entry = _customer_sessions.get(phone)
    if not entry:
        return None
    business_id, expiry = entry
    if datetime.now(timezone.utc) > expiry:
        del _customer_sessions[phone]
        return None
    return business_id

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
    """Classify + alert for a customer message. Returns TwiML auto-reply."""
    website_info = biz.get("website_info", "")
    c = classify_message(body, website_info=website_info)
    msg_id = store_message(biz["id"], sender, body, c)
    tier, conf, summary = c["tier"], c["confidence"], c.get("summary", "Issue reported")
    cat = c.get("category", "other")

    alert_phones = get_alert_phones(biz)
    should_alert_t3 = biz.get("alert_tier3") and tier == 3 and conf > 0.5
    should_alert = tier == 1 or (tier == 2 and conf > 0.7) or should_alert_t3
    silenced = is_alerts_silenced(biz)
    recent_count = get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW)

    logger.info(f"[CLASSIFY] biz={biz['id']} tier={tier} conf={conf:.2f} cat={cat} summary={summary!r}")

    _trial_ok = can_send_alerts(biz)
    if not _trial_ok and tier != 1:
        logger.info(f"[TRIAL BLOCKED] Alert suppressed for {biz['id']} — trial expired or unpaid")
    elif alert_phones and should_alert and not (silenced and tier != 1):
        if recent_count < RATE_LIMIT_MAX:
            if tier == 1:
                alert = "\U0001f6a8 URGENT: Possible emergency reported\nReply DETAILS for full message."
            elif cat == "inquiry":
                alert = f"\u2753 Customer question: {summary}\nReply REPLY to respond or OK to close."
            else:
                tier_label = "⚠️ Issue" if tier == 2 else "💬 Feedback"
                alert = f"{tier_label}: {summary}\nReply OK to close · REPLY to respond · SNOOZE to revisit in 1hr"
            for p in alert_phones:
                ok = send_sms(p, alert)   # uses shared number via _twilio_from
                logger.info(f"[ALERT SENT] to={p} ok={ok} msg={alert!r}")
            mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")
            set_context(biz["id"], msg_id)
        else:
            logger.warning(f"[RATE LIMITED] {biz['id']} hit {recent_count} alerts in {RATE_LIMIT_WINDOW}min window")

    return c.get("auto_reply") or "Thanks for reaching out. We've received your message."


@app.post("/sms/incoming")
async def incoming_sms(From:str=Form(...), Body:str=Form(...), To:str=Form("")):
    _ensure_init()
    sender, body = From.strip(), Body.strip()
    logger.info(f"[INCOMING] From={sender} Body={body[:80]!r}")

    # 1. Check if sender is a registered owner/alert-phone
    owner_biz = get_business_by_owner(sender)
    if owner_biz:
        logger.info(f"[OWNER CMD] biz={owner_biz['id']} cmd={body!r}")
        resp = handle_owner_command(body, owner_biz, sender_phone=sender)
        if not resp: return _twiml("")
        return _twiml(resp)

    # 2. Try to parse a BC#### code from the message body
    code = _parse_business_code_from_body(body)
    if code:
        biz = get_business_by_code(code)
        if biz:
            clean_body = _scrub_hotline_header(body)
            if not clean_body:
                # Customer scanned and hit send without typing — prompt them
                # Still set the session so their follow-up routes correctly
                _set_customer_session(sender, biz["id"])
                logger.info(f"[BLANK MSG] {sender} → {biz['id']} — session set, awaiting message")
                return _twiml("Got it! Now just describe what's wrong and send it to us.")
            _set_customer_session(sender, biz["id"])
            auto_reply = _process_customer_message(biz, sender, clean_body)
            return _twiml(auto_reply)
        else:
            logger.warning(f"[NO BIZ] Received code {code!r} but no matching business")
            return _twiml("Thanks for reaching out. We couldn't find that business code.")

    # 3. No BC#### code — check if sender has an active session from a recent scan
    session_biz_id = _get_customer_session(sender)
    if session_biz_id:
        with get_db() as conn:
            biz = _fetchone(conn, _q("SELECT * FROM businesses WHERE id=?"), (session_biz_id,))
        if biz:
            logger.info(f"[SESSION] {sender} follow-up → {session_biz_id}")
            auto_reply = _process_customer_message(biz, sender, body)
            return _twiml(auto_reply)

    # 4. No code, no session — generic fallback
    logger.info(f"[NO CODE] No BC code or session found for {sender}")
    return _twiml("Thanks for reaching out. To contact a business, please scan their QR code.")

def _twiml(msg):
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Message>'+msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")+'</Message></Response>', media_type="application/xml")


# --- Shared nav + styles ---
NAV_CSS = """
.nav{display:flex;justify-content:center;align-items:center;padding:12px 24px;max-width:960px;margin:0 auto;position:relative}
.nav .logo{font-size:13px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#ea580c;text-decoration:none;position:absolute;left:24px}
.nav-links{display:flex;gap:20px;align-items:center;margin:0 auto}
.nav-links a{font-size:14px;color:#666;text-decoration:none;font-weight:500}
.nav-links a:hover{color:#1a1a1a}
.nav-links .signup-btn{background:#ea580c;color:#fff;padding:8px 16px;border-radius:6px;font-weight:600}
.nav-links .signup-btn:hover{background:#dc2626;color:#fff}
.hamburger{display:none;cursor:pointer;font-size:22px;color:#666;position:absolute;right:24px}
@media(max-width:600px){.nav{justify-content:space-between;padding:12px 16px}.nav .logo{position:static;font-size:12px}.nav-links{display:none;position:absolute;top:48px;right:16px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:12px;flex-direction:column;gap:10px;box-shadow:0 4px 12px rgba(0,0,0,0.08);z-index:10}.nav-links.open{display:flex}.hamburger{display:block;position:static}}
"""

NAV_HTML = """<nav class="nav"><a href="/" class="logo"><span>H</span> HOTLINE</a>
<div class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')">&#9776;</div>
<div class="nav-links"><a href="/">Demo</a><a href="/how-it-works">How It Works</a><a href="/industries">Who We Support</a><a href="/signup" class="signup-btn">Sign Up</a></div></nav>"""


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
- Tier 1: Urgent. ALWAYS tell customer to call 911. NEVER say "we've contacted emergency services."
- Tier 2: Professional, serious. Confirm issue, say management notified. No exclamation marks. NEVER promise action.
- Tier 3: Empathetic. Acknowledge frustration. Invite more details. No exclamation marks.
- Tier 4 positive: Warm, friendly, exclamation marks.
- Tier 4 inquiry: NEVER answer business questions (hours, menu, prices, directions). Forward to management.

HARD RULES:
- NEVER fabricate business information.
- NEVER promise action will be taken.
- NEVER claim to have contacted emergency services.
- NEVER ask follow-up questions for Tier 1 or 2.
- Keep auto_reply under 160 characters.
- Vary responses. Don't repeat templates.

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
<h1 style="max-width:700px;margin:0 auto 12px">Know when your business needs you.<br><em>AI handles the rest.</em></h1>
<p class="sub">Customers text. AI filters. You get alerted when something actually needs your attention.</p>
<p style="font-size:13px;color:#aaa;margin-bottom:8px"><strong style="color:#1a1a1a;font-weight:700">No app. No software. No setup. No training.</strong></p>
<div class="examples"><p style="font-size:12px;font-weight:500;color:#bbb;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.06em">Try a scenario or type your own</p><div class="ex-row">
<div class="ex" onclick="tryEx(this)">Your bathroom is flooding!</div>
<div class="ex" onclick="tryEx(this)">I've been waiting 25 minutes, nobody's helped me</div>
<div class="ex" onclick="tryEx(this)">The front door is locked and there's a line outside</div>
<div class="ex" onclick="tryEx(this)">Carwash bay 2 won't take my card</div>
<div class="ex" onclick="tryEx(this)">Guy at the counter was really rude to me</div>
<div class="ex" onclick="tryEx(this)">Washer #3 is leaking water everywhere</div>
<div class="ex" onclick="tryEx(this)">Gas pump is showing an error</div>
<div class="ex" onclick="tryEx(this)">Arcade machine is jammed and eating coins</div>
<div class="ex" onclick="tryEx(this)">Parking gate is stuck closed</div>
</div></div>
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
<div class="cmd-btn" onclick="resetDemo()" style="background:#f0f0f0;color:#666">Reset</div>
</div>
<div class="input-area owner-input" id="owner-input"><div class="input-row">
<input type="text" id="owner-inp" placeholder="Type a command..." onkeydown="if(event.key==='Enter')ownerCmd(this.value)">
<button class="orange" onclick="ownerCmd(document.getElementById('owner-inp').value)">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
</div>


<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>

<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
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
.hero{text-align:center;padding:40px 24px 32px;max-width:600px;margin:0 auto}
h1{font-size:clamp(24px,4vw,36px);font-weight:700;margin-bottom:12px}
.sub{font-size:16px;color:#888;margin-bottom:32px}
.steps{max-width:520px;margin:0 auto 40px;padding:0 24px;display:flex;flex-direction:column;gap:16px}
.step{display:flex;align-items:flex-start;gap:16px;background:#fff;border:1px solid #e0e0dc;border-radius:12px;padding:20px 22px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.step-num{width:32px;height:32px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:14px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}
.step strong{font-size:15px;display:block;margin-bottom:4px}
.step p{font-size:13px;color:#888;margin:0;line-height:1.5}
.sign-img{text-align:center;padding:0 24px 32px;max-width:420px;margin:0 auto}
.sign-img img{width:100%;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.10)}
.sign-img p{font-size:12px;color:#aaa;margin-top:10px}
.cta{text-align:center;padding:0 24px 48px}
.cta a{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<h1>Three steps. No software.</h1>
<p class="sub">Hotline works through SMS \u2014 no app to install, no dashboard to learn, no training for your team.</p>
</div>
<div class="steps">
<div class="step"><div class="step-num">1</div><div><strong>Display your sign</strong><p>Print your QR sign and post it anywhere \u2014 bathroom, counter, front door, table. Customers scan or text to reach you privately.</p></div></div>
<div class="step"><div class="step-num">2</div><div><strong>AI reads every message</strong><p>Emergencies, operational issues, complaints, compliments, questions \u2014 each one is triaged instantly and the customer gets an appropriate response.</p></div></div>
<div class="step"><div class="step-num">3</div><div><strong>You get alerted by text</strong><p>Only for things that actually need your attention. Reply OK to acknowledge, or REPLY to respond directly to the customer. Everything else stays quiet.</p></div></div>
</div>
<div class="sign-img">
<img src="data:image/jpeg;base64,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" alt="Hotline sign in a bathroom" />
<p>We'll help you design your sign. Customers scan or text \u2014 you get alerted.</p>
</div>
<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
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
</div>

<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
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
<footer>Hotline &middot; AI-powered customer alerts for small businesses &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
<script>
async function signup(){
  const name=document.getElementById('f-name').value.trim();
  let phone=document.getElementById('f-phone').value.trim().replace(/[\\s\\-\\(\\)]/g,'');
  let phone2=document.getElementById('f-phone2').value.trim().replace(/[\\s\\-\\(\\)]/g,'');
  const email=document.getElementById('f-email').value.trim();
  const url=document.getElementById('f-url').value.trim();
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
  btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Setting up...';res.style.display='none';
  try{const r=await fetch('/signup/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,phone2,email,website_url:url})});const d=await r.json();
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
<strong>Website:</strong> <a href="https://HotlineTXT.com">HotlineTXT.com</a><br>
<strong>Mailing Address:</strong> Hotline / HotlineTXT.com, 8405 Siskin CV, Austin, TX 78745</p>
</div>
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a></footer>
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
<strong>Website:</strong> <a href="https://HotlineTXT.com">HotlineTXT.com</a><br>
<strong>Mailing Address:</strong> Hotline / HotlineTXT.com, 8405 Siskin CV, Austin, TX 78745</p>
</div>
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a></footer>
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
    if not name: return {"error":"Business name required"}
    if not phone or not phone.startswith("+"): return {"error":"Valid phone with country code required"}

    base = os.getenv("BASE_URL", "https://hotlinetxt.com")

    # Build business ID
    biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    with get_db() as c:
        if _fetchone(c,_q("SELECT id FROM businesses WHERE id=?"), (biz_id,)):
            biz_id = biz_id[:25]+"-"+datetime.now(timezone.utc).strftime("%H%M%S")

    extra = phone2 if phone2 and phone2.startswith("+") else ""
    business_code = create_business(biz_id, name, phone, "", extra_phones=extra, email=email, website_url=website_url)
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
