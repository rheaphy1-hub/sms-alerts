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
from fastapi.responses import JSONResponse, RedirectResponse

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

# --- Version info (bump VERSION on each new index.py file) ---
VERSION = "v42"
BUILD_TIME = datetime.now(timezone.utc).isoformat()
FEATURE_FLAGS = {
    "tier3_conf_gate": 0.4,
    "alert_dedupe_minutes": 5,
    "classifier_history": True,
    "process_fail_traceback": True,
}

# --- Google Analytics ---
GA_MEASUREMENT_ID = "G-6YYB2N0BSS"
_GA_SCRIPT = (
    f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>'
    f'<script>window.dataLayer=window.dataLayer||[];'
    f'function gtag(){{dataLayer.push(arguments);}}'
    f'gtag("js",new Date());gtag("config","{GA_MEASUREMENT_ID}");</script>'
)

def _ga(html: str) -> str:
    """Inject GA tracking tag before </head>. Wrap every public HTML Response with this."""
    return html.replace("</head>", _GA_SCRIPT + "</head>", 1)

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
            alert_include_images INTEGER DEFAULT 1, created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS messages (
            id {s} {pk}, business_id TEXT NOT NULL, from_number TEXT NOT NULL,
            message_text TEXT NOT NULL, tier INTEGER, category TEXT, sentiment TEXT,
            confidence REAL, summary TEXT, acknowledged INTEGER DEFAULT 0,
            alerted INTEGER DEFAULT 0, explanation TEXT, image_url TEXT, created_at TEXT NOT NULL)""",
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
        f"""CREATE TABLE IF NOT EXISTS media_files (
            id TEXT PRIMARY KEY, business_id TEXT NOT NULL, message_id INTEGER,
            content_type TEXT NOT NULL DEFAULT 'image/jpeg',
            data BYTEA NOT NULL, created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS media_pending (
            business_id TEXT NOT NULL, customer_phone TEXT NOT NULL,
            media_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
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
                         ("stripe_sub_id","\'\'"),("zip","\'\'"),("city","\'\'"),("state","\'\'"),("vertical","\'\'")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE businesses ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except: pass
    # messages table additive columns
    for col, default in [("auto_reply","\'\'"),("explanation","\'\'"),("image_url","\'\'")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE messages ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
        except: pass
    # businesses table additive columns (image preferences)
    for col, default in [("alert_include_images","1")]:
        try:
            with get_db() as c: _execute(c, f"ALTER TABLE businesses ADD COLUMN {col} INTEGER DEFAULT {default}")
        except: pass
    # Drop stale unique constraint on twilio_number (all businesses share one number)
    if USE_POSTGRES:
        for constraint in ("businesses_twilio_number_key",):
            try:
                with get_db() as c: _execute(c, f"ALTER TABLE businesses DROP CONSTRAINT IF EXISTS {constraint}")
            except: pass
        # Backfill any empty twilio_number to shared number
        try:
            with get_db() as c: _execute(c, "UPDATE businesses SET twilio_number=%s WHERE twilio_number='' OR twilio_number IS NULL", ("+18888235592",))
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

def get_business_by_id(bid):
    """Look up a business by its ID."""
    with get_db() as c:
        return _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (bid,))

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

def create_business(biz_id, name, owner_phone, twilio_number="", extra_phones="", email="", website_url="", business_code="", zip_code="", vertical=""):
    now = datetime.now(timezone.utc).isoformat()
    all_phones = ",".join([owner_phone] + [p.strip() for p in extra_phones.split(",") if p.strip()]) if extra_phones else owner_phone
    website_info = scrape_website_info(website_url) if website_url else ""
    if not business_code:
        business_code = _gen_business_code()
    trial_end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    city, state = _lookup_zip(zip_code) if zip_code else ("", "")
    # Try full INSERT with zip/city/state/vertical; fall back without if columns don't exist yet
    for use_zip_cols in (True, False):
        try:
            with get_db() as c:
                if use_zip_cols:
                    _execute(c, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,email,website_url,website_info,twilio_number,business_code,trial_ends_at,sub_status,zip,city,state,vertical,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
                             (biz_id, name, owner_phone, all_phones, email or "", website_url or "", website_info, twilio_number or "", business_code, trial_end, "trialing", zip_code or "", city, state, vertical or "", now))
                else:
                    _execute(c, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,email,website_url,website_info,twilio_number,business_code,trial_ends_at,sub_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
                             (biz_id, name, owner_phone, all_phones, email or "", website_url or "", website_info, twilio_number or "", business_code, trial_end, "trialing", now))
            return business_code
        except Exception as e:
            err_msg = str(e).lower()
            if use_zip_cols and ("zip" in err_msg or "city" in err_msg or "state" in err_msg or "vertical" in err_msg or "column" in err_msg):
                logger.warning(f"create_business: zip/city/state/vertical columns missing, retrying without — {e}")
                continue
            logger.error(f"create_business failed for {biz_id}: {e}")
            return None
    return None

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

def store_message(bid, fn, mt, cl, explanation="", image_url=""):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as c:
        q = _q("INSERT INTO messages (business_id,from_number,message_text,tier,category,sentiment,confidence,summary,explanation,image_url,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        p = (bid,fn,mt,cl.get("tier"),cl.get("category"),cl.get("sentiment"),cl.get("confidence"),cl.get("summary",""),explanation,image_url,now)
        if USE_POSTGRES: cur = _execute(c, q+" RETURNING id", p); return cur.fetchone()[0]
        else: return _execute(c, q, p).lastrowid

def log_alert(mid, bid, at):
    with get_db() as c: _execute(c, _q("INSERT INTO alert_log (message_id,business_id,alert_type,sent_at) VALUES (?,?,?,?)"), (mid,bid,at,datetime.now(timezone.utc).isoformat()))

def update_auto_reply(mid, text):
    with get_db() as c: _execute(c, _q("UPDATE messages SET auto_reply=? WHERE id=?"), (text or "", mid))

def get_recent_customer_history(bid, customer_phone, minutes=30, limit=6):
    """Return last `limit` messages between this customer and the business in the past `minutes`,
    oldest-first, as [{customer, reply}]. Used to give the classifier conversation context."""
    cust = _normalize_phone(customer_phone)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    out = []
    try:
        with get_db() as c:
            rows = _fetchall(c, _q("SELECT message_text, auto_reply FROM messages WHERE business_id=? AND from_number=? AND created_at>=? ORDER BY created_at DESC LIMIT ?"),
                             (bid, cust, cutoff, limit))
            for r in reversed(rows or []):
                out.append({"customer": r.get("message_text") or "", "reply": r.get("auto_reply") or ""})
    except Exception as e:
        logger.error(f"get_recent_customer_history failed: {e}")
    return out

def get_last_alert_at_for_customer(bid, customer_phone, minutes=30):
    """Return the most recent alert_log timestamp for this customer in the past `minutes`, or None.
    Used to de-dupe alerts during an active back-and-forth so the operator gets one alert per real issue."""
    cust = _normalize_phone(customer_phone)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        with get_db() as c:
            row = _fetchone(c, _q("SELECT MAX(a.sent_at) as last_at FROM alert_log a JOIN messages m ON a.message_id=m.id WHERE a.business_id=? AND m.from_number=? AND a.sent_at>=?"),
                            (bid, cust, cutoff))
            if row and row.get("last_at"): return row["last_at"]
    except Exception as e:
        logger.error(f"get_last_alert_at_for_customer failed: {e}")
    return None

# --- Live conversation state (15-min operator-takeover window) ---
CONVERSATION_WINDOW_MIN = 15

def mark_owner_replied(bid, customer_phone):
    """Record that the operator just replied to this customer. Suppresses AI auto-replies for CONVERSATION_WINDOW_MIN minutes."""
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

def find_customer_business(customer_phone):
    """Look up which business this customer has been talking to recently (via BC code or previous messages)."""
    clean = _normalize_phone(customer_phone)
    with get_db() as c:
        # Find most recent message from this customer
        row = _fetchone(c, _q("SELECT business_id FROM messages WHERE from_number=? ORDER BY created_at DESC LIMIT 1"), (clean,))
        if row:
            biz_id = row.get("business_id")
            if biz_id:
                return get_business_by_id(biz_id)
    return None

def is_conversation_active(bid, customer_phone):
    """True if operator replied to this customer in the last CONVERSATION_WINDOW_MIN minutes."""
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
    """Allow alerts if trialing (within window), active, or comped. Block if expired/canceled."""
    status = biz.get("sub_status") or "trialing"
    if status in ("active", "comped"):
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
            link_part = f"\nSubscribe so you don't miss a critical issue from your customers &#9888;\n{PAYMENT_LINK}" if PAYMENT_LINK else ""
            msg = f"Your free Hotline trial ends tomorrow.{link_part}"
            for p in phones: send_sms(p, msg)
            logger.info(f"[TRIAL WARNING] {biz['id']}")
            sent += 1
        elif days == 0:
            set_sub_status(biz["id"], "expired")
            link_part = f"\n{PAYMENT_LINK}" if PAYMENT_LINK else " Reply BILLING to reactivate."
            msg = f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers &#9888;{link_part}"
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

def send_sms(to, body, from_number="", media_url=""):
    sender = from_number or _twilio_from
    if not _twilio_client: logger.info(f"[DRY-RUN] {sender} -> {to}: {body}"); return True
    try:
        kwargs = dict(body=body, from_=sender, to=to)
        if media_url:
            kwargs["media_url"] = [media_url]
        _twilio_client.messages.create(**kwargs); return True
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
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, structural flooding (basement, building, lobby), gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, burst pipe. NOT Tier 1: Toilet or sink overflow/flooding — that is Tier 2 equipment/cleanliness (plumbing issue, not structural emergency).
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
- Tier 3: Empathetic. ALWAYS start with "Thank you for reaching out." Acknowledge frustration. Ask for specifics ONLY if genuinely needed ("Which area?" "What exactly happened?"). Natural tone, no corporate language.
- Tier 4 positive: Warm, friendly. ALWAYS start with "Thank you!" Genuine appreciation, use exclamation marks.
- Tier 4 inquiry: ALWAYS start with "Thank you for contacting us." NEVER answer factual questions (hours, address, menu, prices, directions). If genuinely vague or unclear, ask one clarifying question. Forward to management. Natural conversation, not templates.

FOLLOW-UP QUESTIONS (ask for clarity ONLY in these cases):
- Tier 3 (Complaint/Reputation): Ask specifics to help resolution. "Which [machine/area]?" or "What specifically happened?"
- Tier 4 Inquiry (Vague): Ask for clarity since you cannot answer without details. "Which location?" or "Can you tell us more?"
- NEVER ask follow-ups for: Tier 1 (emergency — no time), Tier 2 clear issues (management knows), Tier 4 positive (just thank them).

HARD RULES:
- NEVER fabricate business information.
- NEVER promise action will be taken. Business decides. You acknowledge and forward.
- NEVER claim to have contacted emergency services.
- NEVER use words like "immediately", "shortly", "soon", "right away", "quickly", "asap", "will get back to you". Avoid all urgency language about timing.
- NEVER ask follow-up questions for Tier 1 (emergency), Tier 2 (clear issues), or Tier 4 positive.
- Only ask follow-ups for: Tier 3 complaints (if specifics needed) or Tier 4 vague inquiries (if clarification needed).
- Keep auto_reply under 160 characters.
- Vary responses naturally. Don't repeat same template. Sound conversational, not corporate.
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
- "Bathroom is flooding!" = Tier 2, cleanliness. Plumbing issue, not structural emergency.
- "Basement is flooding!" = Tier 1, safety. Structural flooding = always emergency.
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


def classify_message(text, website_info="", history=None):
    """Classify a customer SMS. If history (list of {customer, reply} dicts) is provided,
    the AI uses prior turns as context so it doesn't reclassify follow-ups from scratch
    or ask circular clarifying questions."""
    # SAFETY FIRST: Check for emergency keywords BEFORE calling AI.
    # The AI can misinterpret literal emergencies as figurative language.
    # Regex-based detection is fast, reliable, and errs on the side of caution.
    emergency_check = _check_emergency_keywords(text)
    if emergency_check:
        return emergency_check
    
    ctx = f"Business website info (use ONLY for answering basic questions like hours/address): {website_info}" if website_info else "No business website info available. Do NOT guess answers to customer questions."
    prompt = CLASSIFICATION_PROMPT.replace("{website_context}", ctx)
    if _ai_client:
        try:
            # Build user message — include conversation history if present
            if history:
                user_msg = "Conversation so far (same customer):\n"
                for h in history[-6:]:
                    cust = (h.get("customer") or "").strip()
                    rep = (h.get("reply") or "").strip()
                    if cust: user_msg += f'Customer: "{cust}"\n'
                    if rep: user_msg += f'System replied: "{rep}"\n'
                user_msg += (f'\nNew message from same customer: "{text}"\n\n'
                             f'Classify with full context. If this is a follow-up to a prior complaint, '
                             f'KEEP the same tier and category — do NOT reclassify from scratch. '
                             f'If the operator already has enough info, do NOT ask another follow-up question.')
            else:
                user_msg = f'Classify this customer SMS:\n\n"{text}"'
            raw = _anthropic_http(prompt, user_msg)
            if raw.startswith("```"): raw = raw.split("\n",1)[1].rsplit("```",1)[0].strip()
            r = json.loads(raw)
            r["tier"] = max(1,min(4,int(r.get("tier",4))))
            r["confidence"] = max(0.0,min(1.0,float(r.get("confidence",0.5))))
            for k,v in [("category","other"),("sentiment","neutral"),("summary",text[:50]),("auto_reply","Thanks for reaching out. We've received your message.")]:
                r.setdefault(k,v)
            return r
        except Exception as e: logger.error(f"AI classify failed: {e}")
    return _classify_fallback(text)

def _check_emergency_keywords(text):
    """Fast regex-based emergency detection. Runs BEFORE AI to catch literal emergencies.
    
    Three-tier approach:
    - ALWAYS_EMERGENCY: Words that are virtually never figurative. Skip AI entirely.
    - CONTEXTUAL_FIRE: "fire"/"burning" combined with location words = clearly literal.
    - MAYBE_EMERGENCY: Ambiguous safety words. Escalate to Tier 1 but clarify.
    """
    import re as _re
    t = text.lower()
    t_clean = _re.sub(r"[^a-z0-9 ]", " ", t)
    
    # --- ALWAYS Tier 1: These words are almost never figurative ---
    always_emergency = ["911","ambulance","seizure","stabbed","shot","overdose",
                        "gas leak","not breathing","heart attack","collapsed",
                        "unconscious","burst pipe","evacuate",
                        "burning down","on fire call"]
    if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in always_emergency):
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.95,
                "summary":"Emergency reported",
                "auto_reply":"Thank you for alerting us. Call 911 immediately. Evacuate the building if safe to do so."}
    
    # --- CONTEXTUAL FIRE: "fire"/"burning" + location/structure word = literal ---
    fire_figurative = ["fire her","fire him","fire them","fire that","fire the ","fire this",
                       "dumpster fire","on fire with","on fire today","fired","crossfire",
                       "campfire","open fire on","gunfire","you re fired","you are fired",
                       "getting fired","got fired"]
    has_fire = ("fire" in t_clean or "burning" in t_clean) and not any(p in t_clean for p in fire_figurative)
    
    if has_fire:
        # If fire/burning appears with a location word, it's clearly literal → ALWAYS Tier 1
        location_words = ["building","kitchen","store","office","room","bathroom","ceiling",
                          "wall","floor","roof","basement","garage","warehouse","lobby",
                          "house","apartment","unit","suite","hallway","restaurant","shop"]
        if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in location_words):
            return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.95,
                    "summary":"Fire/burning reported at location",
                    "auto_reply":"Thank you for alerting us. Call 911 immediately. Evacuate the building if safe to do so."}
    
    # --- CONTEXTUAL FLOODING: structural flooding is Tier 1, toilet/sink flooding is Tier 2 ---
    has_flooding = any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in ["flooding","flooded","flood"])
    if has_flooding:
        # Toilet/sink flooding is equipment failure, not structural emergency
        plumbing_words = ["toilet","sink","faucet","drain","urinal","bidet"]
        if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in plumbing_words):
            return None  # Let AI classify as Tier 2 equipment
        # Structural flooding (basement, building, etc.) is always Tier 1
        structural_words = ["basement","building","floor","lobby","ceiling","electrical","room","office","warehouse","garage"]
        if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in structural_words):
            return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.95,
                    "summary":"Structural flooding reported",
                    "auto_reply":"Thank you for alerting us. Call 911 immediately. Evacuate the building if safe to do so."}
        # Generic "flooding" without context → maybe emergency (clarify)
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.85,
                "summary":"Possible flooding reported",
                "auto_reply":"This sounds like it could be an emergency. If you are in immediate danger, please call 911 now. Can you tell us exactly what's happening?",
                "_maybe_emergency": True}
    
    # --- MAYBE Tier 1: Could be literal or figurative ---
    maybe_emergency = ["smoke","sparks","electrical","water leak","flood",
                       "bleeding","weapon","gun","violence","injury","hurt"]
    if has_fire: maybe_emergency.append("fire"); maybe_emergency.append("burning")
    
    if any(_re.search(r"\b" + _re.escape(w) + r"\b", t_clean) for w in maybe_emergency):
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.85,
                "summary":"Possible emergency reported",
                "auto_reply":"This sounds like it could be an emergency. If you are in immediate danger, please call 911 now. Can you tell us exactly what's happening?",
                "_maybe_emergency": True}
    
    return None

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
                "auto_reply":"Thank you for alerting us. Call 911 now. Evacuate if safe to do so."}
    question_words = ["what time","when do","where is","where are","do you have","is there","how do i","how much","can i","are you open"]
    if any(w in t for w in question_words) or t.endswith("?"):
        return {"tier":4,"category":"inquiry","sentiment":"neutral","confidence":0.7,"summary":"Customer inquiry",
                "auto_reply":"Thanks for reaching out. We'll have someone get back to you."}
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


# --- Operator commands ---
# Context and reply-mode are stored in DB so they survive server restarts (Vercel serverless)


# --- Generate explanation for operator ---
def generate_explanation(tier, category):
    """Generate concise operator-facing explanation of concern and required action."""
    explanations = {
        (1, "safety"): "Active safety emergency. Immediate life/property risk. Call 911 if not already done. Evacuate if necessary.",
        (2, "equipment"): "Equipment down. Risk: lost revenue, customer abandonment, negative review. Diagnose or call repair service.",
        (2, "staffing"): "Staffing gap detected. Risk: customer frustration, service delays, negative review. Check coverage immediately.",
        (2, "access"): "Access failure. Risk: locked-out customer, liability, complaint escalation. Check locks/codes immediately.",
        (2, "supply"): "Supply depleted. Risk: service interruption, customer frustration. Reorder immediately or notify staff.",
        (2, "cleanliness"): "Sanitation failure. Risk: health code violation, customer disgust, review damage. Clean immediately.",
        (2, "wait_time"): "Long wait reported. Risk: customer abandonment, negative review. Review staffing/capacity immediately.",
        (3, "other"): "Customer concern noted. Risk: online review, customer loss, brand damage. Follow up promptly.",
        (4, "inquiry"): "Customer question. No immediate action required; respond when able.",
        (4, "other"): "Feedback received. No action required.",
    }
    key = (tier, category)
    if key in explanations: return explanations[key]
    tier_fallbacks = {1: "Safety emergency detected. Immediate action required.",2: "Operational issue detected. Investigate and respond within 1 hour.",3: "Customer concern noted. Follow up to preserve relationship.",4: "Message received. No immediate action required."}
    return tier_fallbacks.get(tier, "Alert received.")

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
# in the operator's local time without asking them to configure anything.
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

# --- Zip code to timezone mapping (first 3 digits of US zip) ---
_ZIP3_TZ = {
    # Eastern
    "006":"America/New_York","007":"America/New_York","008":"America/New_York","009":"America/New_York",
    "010":"America/New_York","011":"America/New_York","012":"America/New_York","013":"America/New_York",
    "014":"America/New_York","015":"America/New_York","016":"America/New_York","017":"America/New_York",
    "018":"America/New_York","019":"America/New_York","020":"America/New_York","021":"America/New_York",
    "022":"America/New_York","023":"America/New_York","024":"America/New_York","025":"America/New_York",
    "026":"America/New_York","027":"America/New_York","028":"America/New_York","029":"America/New_York",
    "030":"America/New_York","031":"America/New_York","032":"America/New_York","033":"America/New_York",
    "034":"America/New_York","035":"America/New_York","036":"America/New_York","037":"America/New_York",
    "038":"America/New_York","039":"America/New_York","040":"America/New_York","041":"America/New_York",
    "042":"America/New_York","043":"America/New_York","044":"America/New_York","045":"America/New_York",
    "046":"America/New_York","047":"America/New_York","048":"America/New_York","049":"America/New_York",
    "050":"America/New_York","051":"America/New_York","052":"America/New_York","053":"America/New_York",
    "054":"America/New_York","055":"America/New_York","056":"America/New_York","057":"America/New_York",
    "058":"America/New_York","059":"America/New_York","060":"America/New_York","061":"America/New_York",
    "062":"America/New_York","063":"America/New_York","064":"America/New_York","065":"America/New_York",
    "066":"America/New_York","067":"America/New_York","068":"America/New_York","069":"America/New_York",
    "070":"America/New_York","071":"America/New_York","072":"America/New_York","073":"America/New_York",
    "074":"America/New_York","075":"America/New_York","076":"America/New_York","077":"America/New_York",
    "078":"America/New_York","079":"America/New_York","080":"America/New_York","081":"America/New_York",
    "082":"America/New_York","083":"America/New_York","084":"America/New_York","085":"America/New_York",
    "086":"America/New_York","087":"America/New_York","088":"America/New_York","089":"America/New_York",
    "100":"America/New_York","101":"America/New_York","102":"America/New_York","103":"America/New_York",
    "104":"America/New_York","105":"America/New_York","106":"America/New_York","107":"America/New_York",
    "108":"America/New_York","109":"America/New_York","110":"America/New_York","111":"America/New_York",
    "112":"America/New_York","113":"America/New_York","114":"America/New_York","115":"America/New_York",
    "116":"America/New_York","117":"America/New_York","118":"America/New_York","119":"America/New_York",
    "120":"America/New_York","121":"America/New_York","122":"America/New_York","123":"America/New_York",
    "124":"America/New_York","125":"America/New_York","126":"America/New_York","127":"America/New_York",
    "128":"America/New_York","129":"America/New_York","130":"America/New_York","131":"America/New_York",
    "132":"America/New_York","133":"America/New_York","134":"America/New_York","135":"America/New_York",
    "136":"America/New_York","137":"America/New_York","138":"America/New_York","139":"America/New_York",
    "140":"America/New_York","141":"America/New_York","142":"America/New_York","143":"America/New_York",
    "144":"America/New_York","145":"America/New_York","146":"America/New_York","147":"America/New_York",
    "148":"America/New_York","149":"America/New_York","150":"America/New_York","151":"America/New_York",
    "152":"America/New_York","153":"America/New_York","154":"America/New_York","155":"America/New_York",
    "156":"America/New_York","157":"America/New_York","158":"America/New_York","159":"America/New_York",
    "160":"America/New_York","161":"America/New_York","162":"America/New_York","163":"America/New_York",
    "164":"America/New_York","165":"America/New_York","166":"America/New_York","167":"America/New_York",
    "168":"America/New_York","169":"America/New_York","170":"America/New_York","171":"America/New_York",
    "172":"America/New_York","173":"America/New_York","174":"America/New_York","175":"America/New_York",
    "176":"America/New_York","177":"America/New_York","178":"America/New_York","179":"America/New_York",
    "180":"America/New_York","181":"America/New_York","182":"America/New_York","183":"America/New_York",
    "184":"America/New_York","185":"America/New_York","186":"America/New_York","187":"America/New_York",
    "188":"America/New_York","189":"America/New_York","190":"America/New_York","191":"America/New_York",
    "192":"America/New_York","193":"America/New_York","194":"America/New_York","195":"America/New_York",
    "196":"America/New_York",
    # Southeast (Eastern)
    "200":"America/New_York","201":"America/New_York","202":"America/New_York","203":"America/New_York",
    "204":"America/New_York","205":"America/New_York","206":"America/New_York","207":"America/New_York",
    "208":"America/New_York","209":"America/New_York","210":"America/New_York","211":"America/New_York",
    "212":"America/New_York","214":"America/New_York","215":"America/New_York","216":"America/New_York",
    "217":"America/New_York","218":"America/New_York","219":"America/New_York",
    "220":"America/New_York","221":"America/New_York","222":"America/New_York","223":"America/New_York",
    "224":"America/New_York","225":"America/New_York","226":"America/New_York","227":"America/New_York",
    "228":"America/New_York","229":"America/New_York","230":"America/New_York","231":"America/New_York",
    "232":"America/New_York","233":"America/New_York","234":"America/New_York","235":"America/New_York",
    "236":"America/New_York","237":"America/New_York","238":"America/New_York","239":"America/New_York",
    "240":"America/New_York","241":"America/New_York","242":"America/New_York","243":"America/New_York",
    "244":"America/New_York","245":"America/New_York","246":"America/New_York","247":"America/New_York",
    # Central
    "350":"America/Chicago","351":"America/Chicago","352":"America/Chicago","354":"America/Chicago",
    "355":"America/Chicago","356":"America/Chicago","357":"America/Chicago","358":"America/Chicago",
    "359":"America/Chicago","360":"America/Chicago","361":"America/Chicago","362":"America/Chicago",
    "363":"America/Chicago","364":"America/Chicago","365":"America/Chicago","366":"America/Chicago",
    "367":"America/Chicago","368":"America/Chicago","369":"America/Chicago",
    "370":"America/Chicago","371":"America/Chicago","372":"America/Chicago","373":"America/Chicago",
    "374":"America/Chicago","375":"America/Chicago","376":"America/Chicago","377":"America/Chicago",
    "378":"America/Chicago","379":"America/Chicago","380":"America/Chicago","381":"America/Chicago",
    "382":"America/Chicago","383":"America/Chicago","384":"America/Chicago","385":"America/Chicago",
    "386":"America/Chicago","387":"America/Chicago","388":"America/Chicago","389":"America/Chicago",
    "390":"America/Chicago","391":"America/Chicago","392":"America/Chicago","393":"America/Chicago",
    "394":"America/Chicago","395":"America/Chicago","396":"America/Chicago","397":"America/Chicago",
    # Texas (Central)
    "700":"America/Chicago","701":"America/Chicago","703":"America/Chicago","704":"America/Chicago",
    "705":"America/Chicago","706":"America/Chicago","707":"America/Chicago","708":"America/Chicago",
    "710":"America/Chicago","711":"America/Chicago","712":"America/Chicago","713":"America/Chicago",
    "714":"America/Chicago","716":"America/Chicago","717":"America/Chicago","718":"America/Chicago",
    "719":"America/Chicago","720":"America/Chicago",
    "733":"America/Chicago","734":"America/Chicago","735":"America/Chicago","736":"America/Chicago",
    "737":"America/Chicago","738":"America/Chicago","739":"America/Chicago",
    "740":"America/Chicago","741":"America/Chicago","743":"America/Chicago","744":"America/Chicago",
    "745":"America/Chicago","746":"America/Chicago","747":"America/Chicago","748":"America/Chicago",
    "749":"America/Chicago","750":"America/Chicago","751":"America/Chicago","752":"America/Chicago",
    "753":"America/Chicago","754":"America/Chicago","755":"America/Chicago","756":"America/Chicago",
    "757":"America/Chicago","758":"America/Chicago","759":"America/Chicago","760":"America/Chicago",
    "761":"America/Chicago","762":"America/Chicago","763":"America/Chicago","764":"America/Chicago",
    "765":"America/Chicago","766":"America/Chicago","767":"America/Chicago","768":"America/Chicago",
    "769":"America/Chicago","770":"America/Chicago","772":"America/Chicago","773":"America/Chicago",
    "774":"America/Chicago","775":"America/Chicago","776":"America/Chicago","777":"America/Chicago",
    "778":"America/Chicago","779":"America/Chicago","780":"America/Chicago","781":"America/Chicago",
    "782":"America/Chicago","783":"America/Chicago","784":"America/Chicago","785":"America/Chicago",
    "786":"America/Chicago","787":"America/Chicago","788":"America/Chicago","789":"America/Chicago",
    "790":"America/Chicago","791":"America/Chicago","792":"America/Chicago","793":"America/Chicago",
    "794":"America/Chicago","795":"America/Chicago","796":"America/Chicago","797":"America/Chicago",
    "798":"America/Chicago","799":"America/Chicago",
    # Mountain
    "800":"America/Denver","801":"America/Denver","802":"America/Denver","803":"America/Denver",
    "804":"America/Denver","805":"America/Denver","806":"America/Denver","807":"America/Denver",
    "808":"America/Denver","809":"America/Denver","810":"America/Denver","811":"America/Denver",
    "812":"America/Denver","813":"America/Denver","814":"America/Denver","815":"America/Denver",
    "816":"America/Denver","820":"America/Denver","821":"America/Denver","822":"America/Denver",
    "823":"America/Denver","824":"America/Denver","825":"America/Denver","826":"America/Denver",
    "827":"America/Denver","828":"America/Denver","829":"America/Denver","830":"America/Denver",
    "831":"America/Denver","832":"America/Denver","833":"America/Denver","834":"America/Denver",
    "835":"America/Denver","836":"America/Denver","837":"America/Denver","838":"America/Denver",
    "840":"America/Denver","841":"America/Denver","842":"America/Denver","843":"America/Denver",
    "844":"America/Denver","845":"America/Denver","846":"America/Denver","847":"America/Denver",
    # Arizona (no DST)
    "850":"America/Phoenix","852":"America/Phoenix","853":"America/Phoenix","855":"America/Phoenix",
    "856":"America/Phoenix","857":"America/Phoenix",
    # Mountain continued
    "859":"America/Denver","860":"America/Denver",
    "870":"America/Denver","871":"America/Denver","872":"America/Denver","873":"America/Denver",
    "874":"America/Denver","875":"America/Denver","876":"America/Denver","877":"America/Denver",
    "878":"America/Denver","879":"America/Denver","880":"America/Denver","881":"America/Denver",
    "882":"America/Denver","883":"America/Denver","884":"America/Denver",
    # Pacific
    "900":"America/Los_Angeles","901":"America/Los_Angeles","902":"America/Los_Angeles",
    "903":"America/Los_Angeles","904":"America/Los_Angeles","905":"America/Los_Angeles",
    "906":"America/Los_Angeles","907":"America/Los_Angeles","908":"America/Los_Angeles",
    "910":"America/Los_Angeles","911":"America/Los_Angeles","912":"America/Los_Angeles",
    "913":"America/Los_Angeles","914":"America/Los_Angeles","915":"America/Los_Angeles",
    "916":"America/Los_Angeles","917":"America/Los_Angeles","918":"America/Los_Angeles",
    "919":"America/Los_Angeles","920":"America/Los_Angeles","921":"America/Los_Angeles",
    "922":"America/Los_Angeles","923":"America/Los_Angeles","924":"America/Los_Angeles",
    "925":"America/Los_Angeles","926":"America/Los_Angeles","927":"America/Los_Angeles",
    "928":"America/Los_Angeles","930":"America/Los_Angeles","931":"America/Los_Angeles",
    "932":"America/Los_Angeles","933":"America/Los_Angeles","934":"America/Los_Angeles",
    "935":"America/Los_Angeles","936":"America/Los_Angeles","937":"America/Los_Angeles",
    "938":"America/Los_Angeles","939":"America/Los_Angeles","940":"America/Los_Angeles",
    "941":"America/Los_Angeles","942":"America/Los_Angeles","943":"America/Los_Angeles",
    "944":"America/Los_Angeles","945":"America/Los_Angeles","946":"America/Los_Angeles",
    "947":"America/Los_Angeles","948":"America/Los_Angeles","949":"America/Los_Angeles",
    "950":"America/Los_Angeles","951":"America/Los_Angeles","952":"America/Los_Angeles",
    "953":"America/Los_Angeles","954":"America/Los_Angeles","955":"America/Los_Angeles",
    "956":"America/Los_Angeles","957":"America/Los_Angeles","958":"America/Los_Angeles",
    "959":"America/Los_Angeles","960":"America/Los_Angeles","961":"America/Los_Angeles",
    # Oregon/Washington (Pacific)
    "970":"America/Los_Angeles","971":"America/Los_Angeles","972":"America/Los_Angeles",
    "973":"America/Los_Angeles","974":"America/Los_Angeles","975":"America/Los_Angeles",
    "976":"America/Los_Angeles","977":"America/Los_Angeles","978":"America/Los_Angeles",
    "979":"America/Los_Angeles","980":"America/Los_Angeles","981":"America/Los_Angeles",
    "982":"America/Los_Angeles","983":"America/Los_Angeles","984":"America/Los_Angeles",
    "985":"America/Los_Angeles","986":"America/Los_Angeles","988":"America/Los_Angeles",
    "989":"America/Los_Angeles","990":"America/Los_Angeles","991":"America/Los_Angeles",
    "992":"America/Los_Angeles","993":"America/Los_Angeles","994":"America/Los_Angeles",
    # Alaska/Hawaii
    "995":"America/Anchorage","996":"America/Anchorage","997":"America/Anchorage",
    "998":"America/Anchorage","967":"Pacific/Honolulu","968":"Pacific/Honolulu",
    # Midwest (Central)
    "400":"America/Chicago","401":"America/Chicago","402":"America/Chicago","403":"America/Chicago",
    "404":"America/Chicago","405":"America/Chicago","406":"America/Chicago","407":"America/Chicago",
    "408":"America/Chicago","409":"America/Chicago","410":"America/Chicago","411":"America/Chicago",
    "412":"America/Chicago","413":"America/Chicago","414":"America/Chicago","415":"America/Chicago",
    "416":"America/Chicago","417":"America/Chicago","418":"America/Chicago","420":"America/Chicago",
    "421":"America/Chicago",
    "430":"America/New_York","431":"America/New_York","432":"America/New_York","433":"America/New_York",
    "434":"America/New_York","435":"America/New_York","436":"America/New_York","437":"America/New_York",
    "438":"America/New_York","439":"America/New_York","440":"America/New_York","441":"America/New_York",
    "442":"America/New_York","443":"America/New_York","444":"America/New_York","445":"America/New_York",
    "446":"America/New_York","447":"America/New_York","448":"America/New_York","449":"America/New_York",
    "450":"America/New_York","451":"America/New_York","452":"America/New_York","453":"America/New_York",
    "454":"America/New_York","455":"America/New_York","456":"America/New_York","457":"America/New_York",
    "458":"America/New_York","459":"America/New_York","460":"America/New_York","461":"America/New_York",
    # Illinois, Wisconsin, Minnesota, Iowa (Central)
    "500":"America/Chicago","501":"America/Chicago","502":"America/Chicago","503":"America/Chicago",
    "504":"America/Chicago","505":"America/Chicago","506":"America/Chicago","507":"America/Chicago",
    "508":"America/Chicago","509":"America/Chicago","510":"America/Chicago","511":"America/Chicago",
    "512":"America/Chicago","513":"America/Chicago","514":"America/Chicago","515":"America/Chicago",
    "516":"America/Chicago","520":"America/Chicago","521":"America/Chicago","522":"America/Chicago",
    "523":"America/Chicago","524":"America/Chicago","525":"America/Chicago","526":"America/Chicago",
    "527":"America/Chicago","528":"America/Chicago","530":"America/Chicago","531":"America/Chicago",
    "532":"America/Chicago","534":"America/Chicago","535":"America/Chicago","537":"America/Chicago",
    "538":"America/Chicago","539":"America/Chicago","540":"America/Chicago","541":"America/Chicago",
    "542":"America/Chicago","543":"America/Chicago","544":"America/Chicago","545":"America/Chicago",
    "546":"America/Chicago","547":"America/Chicago","548":"America/Chicago","549":"America/Chicago",
    "550":"America/Chicago","551":"America/Chicago","553":"America/Chicago","554":"America/Chicago",
    "556":"America/Chicago","557":"America/Chicago","558":"America/Chicago","559":"America/Chicago",
    "560":"America/Chicago","561":"America/Chicago","562":"America/Chicago",
    "563":"America/Chicago","564":"America/Chicago","565":"America/Chicago","566":"America/Chicago",
    "567":"America/Chicago",
    # Missouri, Kansas, Nebraska, Dakotas (Central)
    "600":"America/Chicago","601":"America/Chicago","602":"America/Chicago","603":"America/Chicago",
    "604":"America/Chicago","605":"America/Chicago","606":"America/Chicago","607":"America/Chicago",
    "608":"America/Chicago","609":"America/Chicago","610":"America/Chicago","611":"America/Chicago",
    "612":"America/Chicago","613":"America/Chicago","614":"America/Chicago","615":"America/Chicago",
    "616":"America/Chicago","617":"America/Chicago","618":"America/Chicago","619":"America/Chicago",
    "620":"America/Chicago","621":"America/Chicago","622":"America/Chicago","623":"America/Chicago",
    "624":"America/Chicago","625":"America/Chicago","626":"America/Chicago","627":"America/Chicago",
    "628":"America/Chicago","629":"America/Chicago","630":"America/Chicago","631":"America/Chicago",
    "633":"America/Chicago","634":"America/Chicago","635":"America/Chicago","636":"America/Chicago",
    "637":"America/Chicago","638":"America/Chicago","639":"America/Chicago","640":"America/Chicago",
    "641":"America/Chicago","644":"America/Chicago","645":"America/Chicago","646":"America/Chicago",
    "647":"America/Chicago","648":"America/Chicago","650":"America/Chicago","651":"America/Chicago",
    "652":"America/Chicago","653":"America/Chicago","654":"America/Chicago","655":"America/Chicago",
    "656":"America/Chicago","657":"America/Chicago","658":"America/Chicago","660":"America/Chicago",
    "661":"America/Chicago","662":"America/Chicago","664":"America/Chicago","665":"America/Chicago",
    "666":"America/Chicago","667":"America/Chicago","668":"America/Chicago","669":"America/Chicago",
    "670":"America/Chicago","671":"America/Chicago","672":"America/Chicago","673":"America/Chicago",
    "674":"America/Chicago","675":"America/Chicago","676":"America/Chicago","677":"America/Chicago",
    "678":"America/Chicago","679":"America/Chicago","680":"America/Chicago","681":"America/Chicago",
    "683":"America/Chicago","684":"America/Chicago","685":"America/Chicago","686":"America/Chicago",
    "687":"America/Chicago","688":"America/Chicago","689":"America/Chicago","690":"America/Chicago",
    "691":"America/Chicago","692":"America/Chicago","693":"America/Chicago",
    # Florida (Eastern)
    "320":"America/New_York","321":"America/New_York","322":"America/New_York","323":"America/New_York",
    "324":"America/New_York","325":"America/New_York","326":"America/New_York","327":"America/New_York",
    "328":"America/New_York","329":"America/New_York","330":"America/New_York","331":"America/New_York",
    "332":"America/New_York","334":"America/New_York","335":"America/New_York","336":"America/New_York",
    "337":"America/New_York","338":"America/New_York","339":"America/New_York","341":"America/New_York",
    "342":"America/New_York","344":"America/New_York","346":"America/New_York","347":"America/New_York",
    "349":"America/New_York",
    # Georgia, Carolinas (Eastern)  
    "290":"America/New_York","291":"America/New_York","292":"America/New_York","293":"America/New_York",
    "294":"America/New_York","295":"America/New_York","296":"America/New_York","297":"America/New_York",
    "298":"America/New_York","299":"America/New_York","300":"America/New_York","301":"America/New_York",
    "302":"America/New_York","303":"America/New_York","304":"America/New_York","305":"America/New_York",
    "306":"America/New_York","307":"America/New_York","308":"America/New_York","309":"America/New_York",
    "310":"America/New_York","311":"America/New_York","312":"America/New_York","313":"America/New_York",
    "314":"America/New_York","315":"America/New_York","316":"America/New_York","317":"America/New_York",
    "318":"America/New_York","319":"America/New_York",
    # Austin TX specifically
    "786":"America/Chicago","787":"America/Chicago",
    "300":"America/New_York",
    "248":"America/New_York","249":"America/New_York",
}

def _tz_for_business(business):
    """Pick an IANA tz for a business. Zip code wins, then state, then area code.
    Falls back to env DEFAULT_TZ, then UTC. Logs resolution path."""
    if not business: return os.getenv("DEFAULT_TZ","UTC")
    biz_id = business.get("id","?")
    
    # 1. Zip code (most accurate) — try lookup table first
    zip_code = (business.get("zip") or "").strip()
    if len(zip_code) >= 3:
        z3 = zip_code[:3]
        if z3 in _ZIP3_TZ:
            tz = _ZIP3_TZ[z3]
            logger.debug(f"[TZ] {biz_id}: zip={zip_code} (z3={z3}) → {tz} (from map)")
            return tz
        # Fallback: if zip exists but not in map, try live lookup via _lookup_zip
        try:
            city, state = _lookup_zip(zip_code)
            if state and state in _STATE_TZ:
                tz = _STATE_TZ[state]
                logger.info(f"[TZ] {biz_id}: zip={zip_code} → state={state} → {tz} (live lookup)")
                return tz
        except Exception as e:
            logger.debug(f"[TZ] {biz_id}: live zip lookup failed: {e}")
    
    # 2. State (from zip lookup at signup) — fallback if zip failed
    state = (business.get("state") or "").upper().strip()
    if state in _STATE_TZ:
        tz = _STATE_TZ[state]
        logger.debug(f"[TZ] {biz_id}: state={state} → {tz}")
        return tz
    
    # 3. Area code of operator phone (least reliable)
    phone = business.get("owner_phone") or ""
    digits = re.sub(r"\D","",phone)
    if len(digits)==11 and digits[0]=="1": digits = digits[1:]
    if len(digits)>=10:
        ac = digits[:3]
        if ac in _AREA_CODE_TZ:
            tz = _AREA_CODE_TZ[ac]
            logger.debug(f"[TZ] {biz_id}: phone={phone} (ac={ac}) → {tz}")
            return tz
    
    # Final fallback
    default_tz = os.getenv("DEFAULT_TZ","UTC")
    logger.warning(f"[TZ] {biz_id}: no match found, using default: {default_tz}")
    return default_tz

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
    # (so the operator can't accidentally text "STATUS" to the customer).
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
        # If operator types another reserved command, fall through to handle it
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
                logger.info(f"[OPERATOR REPLY] biz={bid} msg_id={reply_mid} to={msg['from_number']}")
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
            return f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers &#9888;{link_part}"

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
        # Diagnostic: show which biz the operator is tied to and the last 3 messages
        # stored under that biz_id. Lets us see when a customer message is being
        # routed to a different business row than the operator expects.
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
{"<p style='color:#c0392b;font-size:14px'>&#9888; "+str(u)+" unacknowledged</p>" if u>0 else ""}
{"<p style='font-size:14px'>Top category: <strong>"+tc+"</strong></p>" if f>0 else ""}
<p style="font-size:13px;color:#aaa;margin-top:24px">Reply MENU to your Hotline number for commands.</p></div>"""

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
Customers scan the QR to send you private feedback. You get a text alert when something needs attention \u2014 including the customer's exact message and our auto-reply.

Quick commands:
REPLY \u2014 Respond to a customer
CLOSE \u2014 End a conversation
STATUS \u2014 Your current settings
PAUSE / RESUME \u2014 Stop or restart alerts
MENU \u2014 Full command list

Emergencies always get through."""


# --- Routes ---
@app.get("/")
def root():
    _ensure_init(); return Response(content=_ga(HOMEPAGE_HTML), media_type="text/html")

@app.get("/health")
def health(): _ensure_init(); return {"status":"ok"}

@app.get("/version")
def version():
    """Returns current code version, build time, and feature flags.
    Use this to verify which version of index.py is actually deployed."""
    _ensure_init()
    return {
        "version": VERSION,
        "build_time": BUILD_TIME,
        "features": FEATURE_FLAGS,
        "twilio_configured": bool(_twilio_client),
        "ai_configured": bool(_ai_client),
        "db": "postgres" if USE_POSTGRES else "sqlite",
    }

# --- Media storage (images from MMS) ---
def _download_and_store_media(twilio_media_url, business_id, message_id=None):
    """Download image from Twilio (authenticated) and store in Neon DB. Returns media ID or None."""
    try:
        import urllib.request, base64, uuid
        twilio_sid = os.getenv("TWILIO_ACCOUNT_SID","")
        twilio_auth = os.getenv("TWILIO_AUTH_TOKEN","")
        if not twilio_sid or not twilio_auth:
            logger.error("[MEDIA] Missing Twilio credentials for media download")
            return None
        
        # Twilio media URLs need auth — use Basic auth with Account SID and Auth Token
        req = urllib.request.Request(twilio_media_url)
        credentials = base64.b64encode(f"{twilio_sid}:{twilio_auth}".encode()).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        req.add_header("User-Agent", "Hotline/1.0")
        
        logger.info(f"[MEDIA] Downloading from {twilio_media_url[:80]}...")
        with urllib.request.urlopen(req, timeout=25) as resp:
            image_data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
        
        if not image_data or len(image_data) < 100:
            logger.warning(f"[MEDIA] Download returned empty/tiny response ({len(image_data)} bytes)")
            return None
        if len(image_data) > 5_000_000:
            logger.warning(f"[MEDIA] Image too large ({len(image_data)} bytes), skipping")
            return None
        
        media_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as c:
            if USE_POSTGRES:
                import psycopg2
                _execute(c, "INSERT INTO media_files (id, business_id, message_id, content_type, data, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                         (media_id, business_id, message_id, content_type, psycopg2.Binary(image_data), now))
            else:
                _execute(c, "INSERT INTO media_files (id, business_id, message_id, content_type, data, created_at) VALUES (?,?,?,?,?,?)",
                         (media_id, business_id, message_id, content_type, image_data, now))
        logger.info(f"[MEDIA] Stored {len(image_data)} bytes as {media_id} type={content_type} for biz={business_id}")
        return media_id
    except Exception as e:
        logger.error(f"[MEDIA] Download/store failed: {e}")
        return None

def _get_public_media_url(media_id):
    """Return the public URL for a stored media file."""
    base = os.getenv("BASE_URL", "https://hotline-sms.vercel.app")
    return f"{base}/media/{media_id}"

def _store_pending_media(business_id, customer_phone, media_id):
    """Store a pending media ID for a customer who sent an image without text."""
    phone = _normalize_phone(customer_phone)
    with get_db() as c:
        # Upsert: replace any existing pending media for this customer+business
        _execute(c, _q("DELETE FROM media_pending WHERE business_id=? AND customer_phone=?"), (business_id, phone))
        _execute(c, _q("INSERT INTO media_pending (business_id, customer_phone, media_id, created_at) VALUES (?,?,?,?)"),
                 (business_id, phone, media_id, datetime.now(timezone.utc).isoformat()))

def _get_pending_media(business_id, customer_phone):
    """Retrieve and return pending media ID for a customer, if any."""
    phone = _normalize_phone(customer_phone)
    with get_db() as c:
        row = _fetchone(c, _q("SELECT media_id FROM media_pending WHERE business_id=? AND customer_phone=?"), (business_id, phone))
    return row["media_id"] if row else None

def _clear_pending_media(business_id, customer_phone):
    """Clear pending media after it's been attached to a message."""
    phone = _normalize_phone(customer_phone)
    with get_db() as c:
        _execute(c, _q("DELETE FROM media_pending WHERE business_id=? AND customer_phone=?"), (business_id, phone))

@app.get("/media/{media_id}")
def serve_media(media_id: str):
    """Serve a stored media file publicly — no auth required."""
    _ensure_init()
    with get_db() as c:
        row = _fetchone(c, _q("SELECT content_type, data FROM media_files WHERE id=?"), (media_id,))
    if not row:
        return Response(content="Not found", status_code=404)
    data = row["data"]
    if isinstance(data, memoryview): data = bytes(data)
    return Response(content=data, media_type=row.get("content_type","image/jpeg"),
                    headers={"Cache-Control":"public, max-age=86400"})

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
    # Process directly instead of calling incoming_sms (which requires a Request object)
    code = _parse_business_code_from_body(body)
    if code:
        biz = get_business_by_code(code)
        if biz:
            clean_body = _scrub_hotline_header(body)
            if not clean_body:
                return {"from": from_num, "to": to_num, "body": body, "auto_reply_sent": "Got it! Now just describe what's wrong and send it to us.", "twiml": "blank"}
            auto_reply = _process_customer_message(biz, from_num, clean_body)
            return {"from": from_num, "to": to_num, "body": body, "auto_reply_sent": auto_reply}
    return {"from": from_num, "to": to_num, "body": body, "auto_reply_sent": "(no BC code found)", "twiml": "empty"}

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
            f"Display your Hotline (PDF): {base}/signs/{code}.pdf\n"
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

@app.post("/admin/update-business")
async def admin_update_business(request: Request):
    """Update editable business fields: name, owner_phone, zip, email, website_url, digest_freq, alert_tier."""
    _ensure_init()
    if not _get_admin_session(request): return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    biz_id = body.get("biz_id","").strip()
    if not biz_id: return {"error":"biz_id required"}
    
    # Fetch current business
    with get_db() as c:
        biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    if not biz: return {"error":"Business not found"}, 404
    
    # Extract and validate fields
    name = body.get("name","").strip()
    owner_phone = body.get("owner_phone","").strip()
    zip_code = body.get("zip","").strip()
    email = body.get("email","").strip()
    website_url = body.get("website_url","").strip()
    digest_freq = body.get("digest_freq","weekly").strip().lower()
    alert_tier = body.get("alert_tier","tier2").strip().lower()
    vertical = body.get("vertical","").strip().lower()
    
    # Validation
    errors = []
    if not name: errors.append("Business name required")
    if not owner_phone: errors.append("Owner phone required")
    if owner_phone and not re.match(r"^\+?1?\d{10}$", owner_phone.replace("-","").replace(" ","").replace("(","").replace(")","")):
        errors.append("Invalid phone format")
    if zip_code and not re.match(r"^\d{5}$", zip_code):
        errors.append("Zip must be 5 digits")
    if email and not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        errors.append("Invalid email format")
    if digest_freq not in ("daily", "weekly"):
        errors.append("digest_freq must be 'daily' or 'weekly'")
    if alert_tier not in ("tier2", "tier3"):
        errors.append("alert_tier must be 'tier2' or 'tier3'")
    
    if errors: return {"error": "; ".join(errors)}, 400
    
    # Normalize phone to +1 format
    digits = re.sub(r"\D","", owner_phone)
    if len(digits) == 10: digits = "1" + digits
    elif len(digits) == 11 and digits[0] != "1": digits = "1" + digits[1:]
    normalized_phone = f"+{digits}" if digits else owner_phone
    
    # If zip changed, re-lookup city/state
    city, state = biz.get("city",""), biz.get("state","")
    if zip_code != (biz.get("zip") or ""):
        city, state = _lookup_zip(zip_code) if zip_code else ("", "")
        logger.info(f"[ADMIN] {biz_id}: zip changed from {biz.get('zip')} to {zip_code}, re-looked up city={city}, state={state}")
    
    # Convert alert_tier to alert_tier3 (legacy column)
    alert_tier3 = 1 if alert_tier == "tier3" else 0
    
    # Update database
    with get_db() as c:
        _execute(c, _q("UPDATE businesses SET name=?, owner_phone=?, zip=?, city=?, state=?, email=?, website_url=?, digest_freq=?, alert_tier3=?, vertical=? WHERE id=?"),
                 (name, normalized_phone, zip_code, city, state, email, website_url, digest_freq, alert_tier3, vertical, biz_id))
    
    # Log the changes
    logger.info(f"[ADMIN] {biz_id}: Updated business info — name={name}, phone={normalized_phone}, zip={zip_code}, email={email}, digest={digest_freq}, tier={alert_tier}, vertical={vertical}")
    
    # Fetch updated business to return
    with get_db() as c:
        updated_biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    
    tz_name = _tz_for_business(updated_biz)
    return {
        "success": True,
        "business": {
            "id": updated_biz["id"],
            "name": updated_biz["name"],
            "owner_phone": updated_biz["owner_phone"],
            "zip": updated_biz["zip"],
            "city": updated_biz["city"],
            "state": updated_biz["state"],
            "timezone": tz_name,
            "email": updated_biz["email"],
            "website_url": updated_biz["website_url"],
            "digest_freq": updated_biz["digest_freq"],
            "alert_tier": "tier3" if updated_biz["alert_tier3"] else "tier2",
            "business_code": updated_biz["business_code"],
        }
    }

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
            msg = f"Your free Hotline trial has ended. Subscribe so you don't miss a critical issue from your customers &#9888;{link_part}"
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
            f'<td style="padding:12px 16px;font-weight:600"><a href="#" onclick="openDrawer(\'{bid}\',\'{b["name"].replace("\'", "")}\');return false" style="color:#1a1a1a;text-decoration:none;border-bottom:1px solid #e0e0dc">{b["name"]}</a><br><span style="font-size:11px;color:#2563eb;cursor:pointer;text-decoration:underline" onclick="editPhones(\'{bid}\',\'{alert_phones_display.replace("\"","&quot;")}\');">{alert_phones_display}</span></td>'
            f'<td style="padding:12px 16px;font-family:monospace;font-size:13px;color:#ea580c;font-weight:600">{b.get("business_code","—")}</td>' \
            f'<td style="padding:12px 16px">{("<span style=\'background:#f0fdf4;color:#166534;font-size:10px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase\'>" + b.get("vertical","").replace("-"," ").title() + "</span>") if b.get("vertical") else "<span style=\'color:#ccc;font-size:11px\'>—</span>"}</td>'
            f'<td style="padding:12px 16px;text-align:center">{s["total_messages"]}</td>'
            f'<td style="padding:12px 16px;text-align:center">{s["flagged_issues"]}</td>'
            f'<td style="padding:12px 16px">{badge_html}{trial_info}</td>'
            f'<td style="padding:12px 16px;white-space:nowrap">'
            f'<a href="#" onclick="adminResend(\'{bid}\');return false" style="color:#2563eb;font-size:12px;margin-right:10px">Resend</a>'
            f'<a href="#" onclick="openBilling(\'{bid}\',\'{b["name"]}\',\'{bstatus}\',\'{trial_end_val}\');return false" style="color:#7c3aed;font-size:12px;margin-right:10px">Billing</a>'
            f'<a href="#" onclick="adminRemove(\'{bid}\',\'{b["name"]}\');return false" style="color:#dc2626;font-size:12px">Remove</a>'
            f'</td></tr>'
        )
    if not rows: rows = '<tr><td colspan="7" style="padding:24px;text-align:center;color:#999">No businesses yet.</td></tr>'

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
  </div>
  <h2 style="font-size:16px;font-weight:700;margin:0 0 12px">Signups by Vertical</h2>
  <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:28px">
    {"".join(
      f'<div style="background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:12px 18px;display:flex;align-items:center;gap:10px">' +
      f'<span style="font-size:11px;text-transform:uppercase;color:#aaa;letter-spacing:.05em">{label}</span>' +
      f'<span style="font-size:20px;font-weight:700;color:#1a1a1a">{sum(1 for b in all_biz if (b.get("vertical") or "") == slug)}</span></div>'
      for slug, label in [("laundromat","Laundromat"),("selfstorage","Self Storage"),("mhc","Mobile Home Park"),("gym","Gym"),("carwash","Car Wash"),("rvpark","RV Park"),("","Other / Direct")]
    )}
  </div>'''

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline Admin</title>
<style>
#drawer{{position:fixed;top:0;right:-500px;width:500px;height:100vh;background:#fff;border-left:1px solid #e0e0dc;box-shadow:-4px 0 24px rgba(0,0,0,0.08);transition:right 0.25s ease;z-index:200;overflow-y:auto;padding:24px 28px}}#drawer.open{{right:0}}@media(max-width:560px){{#drawer{{width:100%;right:-100%}}}}
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
      <button onclick="doSendBillingSms()" style="width:100%;padding:8px;background:#f5f5f0;color:#333;border:1px solid #e0e0dc;border-radius:6px;font-size:13px;cursor:pointer">📱 Send Billing SMS to Operator</button>
    </div>
  </div>
</div>

<!-- Edit Business Modal -->
<div id="edit-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:350;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:12px;padding:28px;width:420px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.15);max-height:90vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h3 style="margin:0;font-size:16px">Edit Business</h3>
      <a href="#" onclick="closeEditModal();return false" style="color:#888;font-size:20px;text-decoration:none">&times;</a>
    </div>
    <div id="edit-error" style="display:none;background:#fee2e2;color:#991b1b;padding:10px 12px;border-radius:6px;margin-bottom:16px;font-size:13px"></div>
    <div style="display:flex;flex-direction:column;gap:14px">
      <div>
        <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Business Name *</label>
        <input id="em-name" type="text" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
      </div>
      <div>
        <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Owner Phone *</label>
        <input id="em-phone" type="tel" placeholder="+1(555)555-1234" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Zipcode</label>
          <input id="em-zip" type="text" placeholder="12345" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">City, State</label>
          <input id="em-location" type="text" disabled style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:13px;background:#f8f8f6;box-sizing:border-box;color:#aaa">
        </div>
      </div>
      <div>
        <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Email</label>
        <input id="em-email" type="email" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
      </div>
      <div>
        <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Website URL</label>
        <input id="em-website" type="url" placeholder="https://example.com" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Digest Frequency</label>
          <select id="em-digest" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;color:#888;margin-bottom:4px">Vertical</label>
          <select id="em-vertical" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
            <option value="">— Not set —</option>
            <option value="laundromat">Laundromat</option>
            <option value="carwash">Car Wash</option>
            <option value="selfstorage">Self Storage</option>
            <option value="mhc">Mobile Home Park</option>
            <option value="rvpark">RV Park</option>
            <option value="gym">24/7 Gym</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div>
          <label style="display:block;font-size:12px;font-weight:600;color:#666;margin-bottom:4px">Alert Tier</label>
          <select id="em-tier" style="width:100%;padding:8px 10px;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;box-sizing:border-box">
            <option value="tier2">Tier 2 (Critical)</option>
            <option value="tier3">Tier 3 (All)</option>
          </select>
        </div>
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-top:20px;border-top:1px solid #f0f0ec;padding-top:16px">
      <button onclick="saveBusinessEdit()" style="flex:1;padding:10px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer">Save Changes</button>
      <button onclick="closeEditModal()" style="flex:1;padding:10px;background:#f5f5f0;color:#333;border:1px solid #e0e0dc;border-radius:6px;font-size:14px;cursor:pointer">Cancel</button>
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
    window._drawerBiz=b; window._drawerBizId=bizId;
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
      <div>${{msg_rows}}</div>
      <div style="border-top:1px solid #f0f0ec;margin-top:20px;padding-top:16px;display:flex;gap:10px">
        <button onclick="openEditModal(window._drawerBizId,window._drawerBiz);return false" style="flex:1;padding:8px 10px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600">✏️ Edit</button>
      </div>`;
  }}catch(e){{document.getElementById("drawer-body").innerHTML="<p style='color:#dc2626'>Error: "+e.message+"</p>";}}
}}
var _emBizId="",_emData={{}};
function openEditModal(bizId,data){{
  _emBizId=bizId;_emData=data;
  document.getElementById("em-name").value=data.name||"";
  document.getElementById("em-phone").value=data.owner_phone||"";
  document.getElementById("em-zip").value=data.zip||"";
  document.getElementById("em-location").value=(data.city||"")+(data.state?" "+data.state:"");
  document.getElementById("em-email").value=data.email||"";
  document.getElementById("em-website").value=data.website_url||"";
  document.getElementById("em-digest").value=data.digest_freq||"weekly";
  document.getElementById("em-tier").value=(data.alert_tier3?"tier3":(data.alert_tier||"tier2"));
  document.getElementById("em-vertical").value=data.vertical||data.vertical_slug||"";
  document.getElementById("edit-error").style.display="none";
  document.getElementById("edit-modal").style.display="flex";
}}
function closeEditModal(){{
  document.getElementById("edit-modal").style.display="none";
  _emBizId="";_emData={{}};
}}
async function saveBusinessEdit(){{
  const name=document.getElementById("em-name").value.trim();
  const phone=document.getElementById("em-phone").value.trim();
  const zip=document.getElementById("em-zip").value.trim();
  const email=document.getElementById("em-email").value.trim();
  const website=document.getElementById("em-website").value.trim();
  const digest=document.getElementById("em-digest").value;
  const tier=document.getElementById("em-tier").value;
  const errEl=document.getElementById("edit-error");
  if(!name||!phone){{errEl.textContent="Name and phone are required";errEl.style.display="block";return;}}
  const vertical=document.getElementById("em-vertical").value;
  try{{
    const r=await fetch("/admin/update-business",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{biz_id:_emBizId,name:name,owner_phone:phone,zip:zip,email:email,website_url:website,digest_freq:digest,alert_tier:tier,vertical:vertical}})}});
    const d=await r.json();
    if(!d.success){{errEl.textContent=d.error||"Failed to save";errEl.style.display="block";return;}}
    toast("Business updated",true);
    closeEditModal();
    closeDrawer();
    location.reload();
  }}catch(e){{errEl.textContent="Error: "+e.message;errEl.style.display="block";}}
}}
document.getElementById("billing-modal").addEventListener("click",function(e){{if(e.target===this)closeBilling();}});
document.getElementById("edit-modal").addEventListener("click",function(e){{if(e.target===this)closeEditModal();}});
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
    """Extract BC#### from message body like 'HOTLINE BC4729 bathroom is dirty'.
    Handles various formats and Twilio quirks."""
    if not body:
        return None
    
    # Primary: Look for BC#### as a whole word
    m = re.search(r"\bBC\d{4}\b", body.upper())
    if m:
        return m.group(0)
    
    # Secondary: Look for HOTLINE BC#### (even without word boundary)
    m = re.search(r"HOTLINE\s+BC(\d{4})", body.upper(), re.IGNORECASE)
    if m:
        return f"BC{m.group(1)}"
    
    # Tertiary: Just BC#### anywhere (looser match for Twilio edge cases)
    m = re.search(r"BC(\d{4})", body.upper())
    if m:
        return f"BC{m.group(1)}"
    
    return None


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

def _process_customer_message(biz, sender, body, image_url=""):
    """Classify + alert for a customer message. Returns the auto-reply text (or empty if suppressed)."""
    try:
        website_info = biz.get("website_info", "")
        # Pull recent back-and-forth between this customer and this business so the
        # classifier doesn't reclassify follow-ups from scratch or loop on clarifying questions.
        history = get_recent_customer_history(biz["id"], sender, minutes=30, limit=6)
        c = classify_message(body, website_info=website_info, history=history)
        explanation = generate_explanation(c["tier"], c.get("category", "other"))
        
        # If customer sent an image, download and store it for public access
        public_media_url = ""
        if image_url:
            media_id = _download_and_store_media(image_url, biz["id"])
            if media_id:
                public_media_url = _get_public_media_url(media_id)
        
        msg_id = store_message(biz["id"], sender, body, c, explanation=explanation, image_url=public_media_url or image_url)
        tier, conf, summary = c["tier"], c["confidence"], c.get("summary", "Issue reported")
        cat = c.get("category", "other")

        # If the operator has recently replied to this customer, the human is on the
        # line. Don't step on them with an AI message. Alerts still fire.
        convo_active = is_conversation_active(biz["id"], sender)
        if convo_active:
            auto_reply = ""
            logger.info(f"[CONVO ACTIVE] Suppressing auto-reply for {sender} \u2192 {biz['id']} (operator active)")
        else:
            auto_reply = c.get("auto_reply") or "Thanks for reaching out. We've received your message."
        update_auto_reply(msg_id, auto_reply)

        alert_phones = get_alert_phones(biz)
        # Tier 3 gate lowered from 0.5 to 0.4 — short clear complaints like "food is terrible"
        # often come back with conf 0.4–0.5. Tier 2 stays at 0.7 (higher bar for operational).
        should_alert_t3 = bool(biz.get("alert_tier3")) and tier == 3 and conf >= 0.4
        should_alert = tier == 1 or (tier == 2 and conf > 0.7) or should_alert_t3
        paused = bool(biz.get("paused"))
        recent_count = get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW)

        logger.info(f"[CLASSIFY] biz={biz['id']} tier={tier} conf={conf:.2f} cat={cat} alert_tier3={biz.get('alert_tier3')} should_alert={should_alert} summary={summary!r}")

        # Dedupe alerts within an active thread. If we already alerted this customer in the
        # past 30 min AND the new tier isn't an escalation (lower-numbered), skip the alert.
        # Tier 1 is ALWAYS sent regardless of dedupe (spec: emergencies always fire).
        dedupe_skip = False
        if should_alert and tier != 1:
            last_alert_at = get_last_alert_at_for_customer(biz["id"], sender, minutes=5)
            if last_alert_at:
                # Check if previous alert was same tier or lower-severity (higher number).
                # Only re-alert if THIS message is more severe than what we last alerted on.
                try:
                    with get_db() as conn:
                        prev = _fetchone(conn, _q("SELECT m.tier as ptier FROM alert_log a JOIN messages m ON a.message_id=m.id WHERE a.business_id=? AND m.from_number=? AND a.sent_at=? LIMIT 1"),
                                         (biz["id"], _normalize_phone(sender), last_alert_at))
                        prev_tier = prev.get("ptier") if prev else None
                        if prev_tier is not None and tier >= int(prev_tier):
                            dedupe_skip = True
                            logger.info(f"[ALERT DEDUPED] biz={biz['id']} sender={sender} prev_tier={prev_tier} new_tier={tier} last_alert={last_alert_at} \u2014 already alerted on same/higher severity in last 5min")
                except Exception as e:
                    logger.error(f"Dedupe check failed (will send alert): {e}")

        _trial_ok = can_send_alerts(biz)
        if not _trial_ok and tier != 1:
            logger.info(f"[ALERT SKIPPED] reason=trial_expired biz={biz['id']} tier={tier}")
        elif not alert_phones:
            logger.info(f"[ALERT SKIPPED] reason=no_alert_phones biz={biz['id']} tier={tier}")
        elif not should_alert:
            reasons = []
            if tier == 2 and conf <= 0.7: reasons.append(f"tier2_low_conf({conf:.2f})")
            if tier == 3 and not biz.get("alert_tier3"): reasons.append("tier3_disabled")
            if tier == 3 and biz.get("alert_tier3") and conf < 0.4: reasons.append(f"tier3_low_conf({conf:.2f})")
            if tier == 4: reasons.append("tier4_routine")
            logger.info(f"[ALERT SKIPPED] reason={'|'.join(reasons) or 'policy'} biz={biz['id']} tier={tier} conf={conf:.2f}")
        elif paused and tier != 1:
            logger.info(f"[ALERT SKIPPED] reason=paused biz={biz['id']} tier={tier}")
        elif dedupe_skip:
            pass  # already logged
        elif recent_count >= RATE_LIMIT_MAX:
            logger.warning(f"[RATE LIMITED] {biz['id']} hit {recent_count} alerts in {RATE_LIMIT_WINDOW}min window")
        else:
            # Self-contained alert: everything the operator needs in one message.
            if tier == 1:
                header = "\U0001f6a8 URGENT"
            elif cat == "inquiry":
                header = "\u2753 Customer question"
            elif tier == 2:
                header = "&#9888; Issue"
            else:
                header = "\U0001f4ac Feedback"
            when = _fmt_ts(datetime.now(timezone.utc).isoformat(), biz)
            reply_block = f"We replied:\n{auto_reply}\n\n" if auto_reply else "(AI silent \u2014 conversation active)\n\n"
            alert = (f"{header} ({when})\n"
                     f"Category: {cat}\n"
                     f"Concern: {explanation}\n\n"
                     f"Customer:\n{body}\n\n"
                     f"{reply_block}"
                     f"Reply REPLY to message customer back.")
            if public_media_url and biz.get("alert_include_images"):
                alert += "\n📷 Photo attached"
            for p in alert_phones:
                ok = send_sms(p, alert, media_url=public_media_url if (public_media_url and biz.get("alert_include_images")) else "")
                logger.info(f"[ALERT SENT] to={p} ok={ok} biz={biz['id']} tier={tier}")
            mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")

        return auto_reply
    except Exception as e:
        import traceback
        logger.error(f"[PROCESS FAIL] biz={biz.get('id')} sender={sender} body={body[:80]!r} err={e}\n{traceback.format_exc()}")
        return "Thanks for reaching out. We've received your message."


@app.post("/sms/incoming")
async def incoming_sms(request: Request):
    _ensure_init()
    # ── Twilio signature validation ─────────────────────────────────
    # Prevents spoofed POST requests from triggering alerts
    _twilio_auth = os.getenv("TWILIO_AUTH_TOKEN", "")
    if _twilio_auth:
        try:
            from twilio.request_validator import RequestValidator as _TwilioValidator
            _tv = _TwilioValidator(_twilio_auth)
            _url = str(request.url)
            _sig = request.headers.get("X-Twilio-Signature", "")
            _post = dict(await request.form())
            if not _tv.validate(_url, _post, _sig):
                logger.warning("[SMS] Invalid Twilio signature — request rejected")
                from twilio.twiml.messaging_response import MessagingResponse as _MR
                return Response(content=str(_MR()), media_type="application/xml", status_code=200)
            form_data = _post
        except ImportError:
            logger.warning("[SMS] twilio.request_validator not available — skipping sig check")
            form_data = await request.form()
        except Exception as _e:
            logger.error(f"[SMS] Signature validation error: {_e}")
            form_data = await request.form()
    else:
        form_data = await request.form()
    # ── End Twilio validation ───────────────────────────────────────
    sender = (form_data.get("From") or "").strip()
    body = (form_data.get("Body") or "").strip()
    media_url = (form_data.get("MediaUrl0") or "").strip()
    num_media = form_data.get("NumMedia", "0")
    
    logger.info(f"[RAW BODY] {body!r}")
    logger.info(f"[MEDIA CHECK] MediaUrl0={media_url!r} NumMedia={num_media!r}")
    # Log all form keys that contain 'media' (case-insensitive) to find image params
    media_keys = [k for k in form_data.keys() if 'media' in k.lower() or 'Media' in k]
    if media_keys:
        logger.info(f"[MEDIA KEYS] {media_keys}")

    # If the message body contains a BC#### code, treat it as a customer
    # message even when the sender is a registered operator. This lets operators
    # test their own hotline from their personal phone without their texts
    # getting captured by the operator-command handler.
    code = _parse_business_code_from_body(body)
    logger.info(f"[DEBUG] BC code parse result: {code!r} from body: {body[:100]!r}")
    if code:
        biz = get_business_by_code(code)
        logger.info(f"[DEBUG] Found business for code {code}: {biz['id'] if biz else 'NOT FOUND'}")
        if biz:
            clean_body = _scrub_hotline_header(body)
            has_meaningful_text = len(clean_body.strip()) > 5
            
            if media_url and not has_meaningful_text:
                # Image only without text — download and store now, ask for description
                media_id = _download_and_store_media(media_url, biz["id"])
                if media_id:
                    # Store pending media_id for this customer so we can attach it to their next message
                    _store_pending_media(biz["id"], sender, media_id)
                    logger.info(f"[MMS IMAGE ONLY] {sender} → {biz['id']} — image stored as {media_id}, awaiting description")
                else:
                    logger.info(f"[MMS IMAGE ONLY] {sender} → {biz['id']} — image download failed")
                return _twiml("Photo received. Quick description: what's going on?")
            elif not clean_body:
                logger.info(f"[BLANK MSG] {sender} → {biz['id']} — awaiting message")
                return _twiml("Got it! Now just describe what's wrong and send it to us.")
            
            # Has text — process with optional image
            # Check if there's a pending image from a previous image-only message
            pending_media_url = ""
            pending_mid = _get_pending_media(biz["id"], sender) if not media_url else None
            if pending_mid:
                pending_media_url = _get_public_media_url(pending_mid)
                _clear_pending_media(biz["id"], sender)
                logger.info(f"[PENDING MEDIA] Attaching stored image {pending_mid} to follow-up from {sender}")
            
            # Process the message and send reply FIRST (don't block on image download)
            auto_reply = _process_customer_message(biz, sender, clean_body, image_url=pending_media_url)
            reply_response = _twiml(auto_reply)
            
            # AFTER reply is queued, try to download and store the image
            # If this fails or times out, the customer still got their reply
            if media_url and not pending_media_url:
                try:
                    mid = _download_and_store_media(media_url, biz["id"])
                    if mid:
                        public_url = _get_public_media_url(mid)
                        # Update the stored message with the image URL
                        with get_db() as conn:
                            _execute(conn, _q("UPDATE messages SET image_url=? WHERE id=(SELECT id FROM messages WHERE business_id=? AND from_number=? ORDER BY created_at DESC LIMIT 1)"),
                                     (public_url, biz["id"], _normalize_phone(sender)))
                        # Send photo as separate MMS to operator if alert was sent
                        if biz.get("alert_include_images"):
                            alert_phones = get_alert_phones(biz)
                            for p in alert_phones:
                                send_sms(p, "📷 Photo from customer:", media_url=public_url)
                except Exception as e:
                    logger.error(f"[MEDIA POST-REPLY] Image download failed (reply was still sent): {e}")
            
            return reply_response
        else:
            logger.warning(f"[NO BIZ] Received code {code!r} but no matching business")
            return _twiml("Thanks for reaching out. We couldn't find that business code.")

    # 1. Check if sender is a registered operator/alert-phone
    owner_biz = get_business_by_owner(sender)
    if owner_biz:
        logger.info(f"[OPERATOR CMD] biz={owner_biz['id']} cmd={body!r}")
        resp = handle_owner_command(body, owner_biz, sender_phone=sender)
        if not resp: return _twiml("")
        return _twiml(resp)

    # 2. No BC code — check if customer has an active conversation
    customer_biz = find_customer_business(sender)
    if customer_biz:
        logger.info(f"[CUSTOMER RETURN] {sender} → {customer_biz['id']} — conversation continues")
        # Check for pending image from a previous image-only message
        pending_media_url = ""
        pending_mid = _get_pending_media(customer_biz["id"], sender) if not media_url else None
        if pending_mid:
            pending_media_url = _get_public_media_url(pending_mid)
            _clear_pending_media(customer_biz["id"], sender)
            logger.info(f"[PENDING MEDIA] Attaching stored image {pending_mid} to follow-up from {sender}")
        
        # Process and reply FIRST
        auto_reply = _process_customer_message(customer_biz, sender, body, image_url=pending_media_url)
        reply_response = _twiml(auto_reply)
        
        # AFTER reply, try to download new image if present
        if media_url and not pending_media_url:
            try:
                mid = _download_and_store_media(media_url, customer_biz["id"])
                if mid:
                    public_url = _get_public_media_url(mid)
                    with get_db() as conn:
                        _execute(conn, _q("UPDATE messages SET image_url=? WHERE id=(SELECT id FROM messages WHERE business_id=? AND from_number=? ORDER BY created_at DESC LIMIT 1)"),
                                 (public_url, customer_biz["id"], _normalize_phone(sender)))
                    if customer_biz.get("alert_include_images"):
                        for p in get_alert_phones(customer_biz):
                            send_sms(p, "📷 Photo from customer:", media_url=public_url)
            except Exception as e:
                logger.error(f"[MEDIA POST-REPLY] Image download failed: {e}")
        
        return reply_response
    
    # 3. Unknown sender with no context
    logger.info(f"[NO CONTEXT] no BC code or prior conversation from {sender}")
    return _twiml("")

def _twiml(msg):
    if not msg:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Message>'+msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")+'</Message></Response>', media_type="application/xml")


# --- Shared nav + styles ---
NAV_CSS = """
body{background:#f8f8f6}
.nav{display:flex;align-items:center;justify-content:center;padding:12px 24px;max-width:100%;margin:0 auto;position:relative}.nav .logo{flex:0 0 auto}.nav-links{position:absolute;right:24px;display:flex;gap:20px;align-items:center}
.nav .logo{font-size:13px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#ea580c;text-decoration:none;display:flex;align-items:center}
.nav .logo svg{height:36px;width:auto}
.nav-links{display:flex;gap:20px;align-items:center;margin-left:auto}
.nav-links a{font-size:14px;color:#666;text-decoration:none;font-weight:500}
.nav-links a:hover{color:#1a1a1a}
.nav-links .signup-btn{background:#ea580c;color:#fff;padding:8px 16px;border-radius:6px;font-weight:600}
.nav-links .signup-btn:hover{background:#dc2626;color:#fff}
.dropdown{position:relative}
.dropdown>a::after{content:" ▾";font-size:10px;opacity:0.6}
.dropdown-menu{display:none;position:absolute;top:100%;left:50%;transform:translateX(-50%);background:transparent;min-width:180px;z-index:100;padding-top:6px}.dropdown-menu-inner{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:8px;box-shadow:0 8px 24px rgba(0,0,0,0.1)}
.dropdown-menu-inner a{display:block;padding:8px 14px;font-size:13px;color:#444;border-radius:6px;white-space:nowrap}
.dropdown-menu-inner a:hover{background:#fff7ed;color:#ea580c}
.dropdown:hover .dropdown-menu{display:block}
.hamburger{display:none;cursor:pointer;font-size:22px;color:#666}
@media(max-width:600px){.nav{flex-wrap:wrap;padding:8px 16px}.nav .logo{position:static;transform:none;flex:0 0 auto}.nav .logo svg{height:36px}.nav-links{display:none;position:absolute;top:48px;right:16px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:12px;flex-direction:column;gap:10px;box-shadow:0 4px 12px rgba(0,0,0,0.08);z-index:10;margin-left:0}.nav-links.open{display:flex}.hamburger{display:block;margin-left:auto}}
"""

NAV_HTML = """<nav class="nav"><a href="/" class="logo"><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="300" viewBox="0 0 224.87999 67.499998" preserveAspectRatio="xMidYMid meet" version="1.0"><defs><clipPath id="d1n"><path d="M 0.765625 9 L 48 9 L 48 57 L 0.765625 57 Z M 0.765625 9 " clip-rule="nonzero"/></clipPath><clipPath id="d2n"><path d="M 208 20 L 223.992188 20 L 223.992188 45 L 208 45 Z M 208 20 " clip-rule="nonzero"/></clipPath></defs><g clip-path="url(#d1n)"><path fill="#ea580c" d="M 7.839844 9.40625 L 40.8125 9.40625 C 44.589844 9.40625 47.878906 12.699219 47.878906 16.488281 L 47.878906 49.542969 C 47.878906 53.332031 44.589844 56.625 40.8125 56.625 L 7.839844 56.625 C 4.0625 56.625 0.777344 53.332031 0.777344 49.542969 L 0.777344 16.488281 C 0.777344 12.699219 4.0625 9.40625 7.839844 9.40625 Z " fill-opacity="1" fill-rule="nonzero"/></g><g fill="#ffffff" fill-opacity="1"><g transform="translate(10.726965, 46.401259)"><path d="M 20.734375 -12.542969 L 8.230469 -12.542969 L 8.230469 0 L 3.109375 0 L 3.109375 -29.109375 L 8.175781 -29.109375 L 8.175781 -17.214844 L 20.734375 -17.214844 L 20.734375 -29.109375 L 25.816406 -29.109375 L 25.816406 0 L 20.734375 0 Z M 20.734375 -12.542969 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(62.007197, 44.82787)"><path d="M 17.277344 -10.453125 L 6.859375 -10.453125 L 6.859375 0 L 2.589844 0 L 2.589844 -24.257812 L 6.8125 -24.257812 L 6.8125 -14.34375 L 17.277344 -14.34375 L 17.277344 -24.257812 L 21.515625 -24.257812 L 21.515625 0 L 17.277344 0 Z M 17.277344 -10.453125 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(89.370287, 44.82787)"><path d="M 12.859375 -24.640625 C 16.707031 -24.640625 19.71875 -23.261719 21.894531 -20.5 L 22.734375 -19.265625 C 23.976562 -17.132812 24.597656 -14.632812 24.597656 -11.769531 C 24.597656 -8.511719 23.691406 -5.726562 21.882812 -3.402344 C 18.375 -0.136719 15.867188 0.722656 12.886719 0.722656 C 9.1875 0.722656 6.246094 -0.585938 4.0625 -3.203125 C 2.140625 -5.503906 1.179688 -8.421875 1.179688 -11.953125 C 1.179688 -16 2.40625 -19.210938 4.859375 -21.585938 C 6.980469 -23.625 9.648438 -24.640625 12.859375 -24.640625 Z M 12.859375 -20.75 C 10.363281 -20.75 8.425781 -19.769531 7.046875 -17.816406 C 5.949219 -16.25 5.402344 -14.292969 5.402344 -11.953125 C 5.402344 -8.839844 6.324219 -6.480469 8.167969 -4.867188 C 9.445312 -3.738281 11.019531 -3.171875 12.886719 -3.171875 C 15.363281 -3.171875 17.292969 -4.132812 18.675781 -6.050781 C 19.796875 -7.585938 20.359375 -9.511719 20.359375 -11.828125 C 20.359375 -15.089844 19.414062 -17.527344 17.523438 -19.136719 C 16.257812 -20.210938 14.699219 -20.75 12.859375 -20.75 Z "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(118.49594, 44.82787)"><path d="M 12.421875 -20.363281 L 12.421875 0 L 8.203125 0 L 8.203125 -20.363281 L 0.660156 -20.363281 L 0.660156 -24.257812 L 19.921875 -24.257812 L 19.921875 -20.363281 Z "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(142.379885, 44.82787)"><path d="M 6.71875 -24.257812 L 6.71875 -3.894531 L 18.035156 -3.894531 L 18.035156 0 L 2.5 0 L 2.5 -24.257812 Z "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(164.53192, 44.82787)"><path d="M 3.128906 -24.257812 L 7.394531 -24.257812 L 7.394531 0 L 3.128906 0 Z "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(177.963102, 44.82787)"><path d="M 21.574219 -24.257812 L 21.574219 0 L 17.265625 0 L 6.445312 -17.007812 L 6.445312 0 L 2.375 0 L 2.375 -24.257812 L 6.5625 -24.257812 L 17.507812 -7.082031 L 17.507812 -24.257812 Z "/></g></g><g clip-path="url(#d2n)"><g fill="#ea580c" fill-opacity="1"><g transform="translate(205.326192, 44.82787)"><path d="M 7.042969 -10.453125 L 7.042969 -3.894531 L 20.546875 -3.894531 L 20.546875 0 L 2.820312 0 L 2.820312 -24.257812 L 19.980469 -24.257812 L 19.980469 -20.363281 L 7.042969 -20.363281 L 7.042969 -14.34375 L 19.519531 -14.34375 L 19.519531 -10.453125 Z "/></g></g></g></svg></a>
<div class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')">&#9776;</div>
<div class="nav-links"><a href="/">Demo</a><a href="/how-it-works">How It Works</a><div class="dropdown"><a href="/industries">Who We Support</a><div class="dropdown-menu"><div class="dropdown-menu-inner"><a href="/laundromat">Laundromat</a><a href="/selfstorage">Self Storage</a><a href="/mhc">Mobile Home Parks</a><a href="/gym">24/7 Gym</a><a href="/carwash">Car Wash</a><a href="/rvpark">RV Parks</a></div></div></div><a href="/resources">Resources</a><a href="/signup" class="signup-btn">Sign Up</a></div></nav>"""


# --- Demo page (homepage) ---
DEMO_PROMPT = """You are simulating a business's customer feedback SMS system for a live demo called Hotline.

TIER DEFINITIONS:
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, structural flooding (basement, building, lobby), gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, burst pipe. NOT Tier 1: Toilet or sink overflow — that is Tier 2 equipment/cleanliness.
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
- Tier 3 (reputation): Ask for more detail to help operator respond.
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
- "Your bathroom is flooding!" = Tier 2, cleanliness. Plumbing issue.
- "Basement is flooding!" = Tier 1, safety. Structural flooding = emergency.
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
    explanation = generate_explanation(c["tier"], c.get("category", "other"))
    return {"tier":c["tier"],"category":c["category"],"sentiment":c["sentiment"],"confidence":c["confidence"],
            "summary":c["summary"],"auto_reply":c["auto_reply"],"explanation":explanation,
            "tier_label":{1:"Emergency",2:"Business-Critical",3:"Reputation Risk",4:"Routine"}.get(c["tier"],"Unknown"),
            "would_alert":c["tier"]==1 or (c["tier"]==2 and c["confidence"]>0.7)}



# ── Vertical Landing Pages ──────────────────────────────────────────────────

def _make_vertical_page(slug, label, headline, sub, scenarios, step1, step2, step3, placements=None, plural=None):
    """Generate a vertical landing page with integrated demo (matching main demo exactly)."""
    # Generate scenario chips HTML
    ex_chips_html = "".join(f'<div class="ex" onclick="tryEx(this)">{s}</div>' for s in scenarios)
    
    # Generate placements grid HTML
    if placements is None:
        placements = []
    placements_html = "".join(f'<div style="padding:14px;background:#fff;border:1px solid #e0e0dc;border-radius:8px;text-align:center;font-size:13px;color:#666">{p}</div>' for p in placements)
    
    # Demo JS (adapted from main demo - uses v-cust and v-oper IDs)
    DEMO_JS = """let lastData=null,replyMode=false,history=[],demoCount=0,maxDemo=10,filterMode='critical';
const mc=document.getElementById('v-cust'),mo=document.getElementById('v-oper');
function addB(c,cls,text,tier){const d=document.createElement('div');d.className='bubble '+cls;if(tier)d.setAttribute('data-tier',tier);d.innerHTML=text;c.appendChild(d);c.scrollTop=c.scrollHeight;if(mo===c)filterDemo(filterMode)}
async function sendDemo(){const inp=document.getElementById('v-input'),btn=document.getElementById('v-btn'),text=inp.value.trim();if(!text)return;if(demoCount>=maxDemo){addB(mc,'system','Demo limit reached. <a href="/signup" style="color:#ea580c">Sign up free</a>');return}inp.value='';btn.disabled=true;demoCount++;addB(mc,'out-blue',text);addB(mo,'system','<span class="spinner"></span> Reading...');try{const r=await fetch('/demo/classify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,history})});const d=await r.json();lastData=d;if(mo.lastChild)mo.lastChild.remove();const reply=d.auto_reply||'Thanks for letting us know.';const cat=(d.category||'general').replace(/_/g,' ');const concern=d.concern||d.explanation||'';
history.push({customer:text,reply});if(history.length>6)history.shift();await new Promise(r=>setTimeout(r,250));addB(mc,'in',reply);await new Promise(r=>setTimeout(r,350));const t=new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});if(d.tier===1){const ch=concern?'<div style="font-size:10px;color:inherit;margin-bottom:4px;opacity:0.85">'+concern+'</div>':'';const msg='<div style="font-weight:700;font-size:11px;margin-bottom:4px">🚨 URGENT &nbsp;'+t+'</div>'+'<div style="font-size:10px;margin-bottom:3px;opacity:0.75">'+cat+'</div>'+ch+'<div style="font-size:11px;margin-bottom:3px"><strong>Customer:</strong><br>'+text+'</div>'+'<div style="font-size:11px;margin-bottom:4px"><strong>We replied:</strong><br>'+reply+'</div>'+'<div style="font-size:10px;opacity:0.65">Reply REPLY to message customer back.</div>';addB(mo,'alert-red',msg,1)}else if(d.tier===2){const ch=concern?'<div style="font-size:10px;color:inherit;margin-bottom:4px;opacity:0.85">'+concern+'</div>':'';const msg='<div style="font-weight:700;font-size:11px;margin-bottom:4px">⚠️ ISSUE &nbsp;'+t+'</div>'+'<div style="font-size:10px;margin-bottom:3px;opacity:0.75">'+cat+'</div>'+ch+'<div style="font-size:11px;margin-bottom:3px"><strong>Customer:</strong><br>'+text+'</div>'+'<div style="font-size:11px;margin-bottom:4px"><strong>We replied:</strong><br>'+reply+'</div>'+'<div style="font-size:10px;opacity:0.65">Reply REPLY to message customer back.</div>';addB(mo,'alert',msg,2)}else if(d.tier===3){const ch=concern?'<div style="font-size:10px;opacity:0.8">'+concern+'</div>':'';const msg='<div style="font-weight:700;font-size:11px;margin-bottom:4px">ℹ️ FEEDBACK &nbsp;'+t+'</div>'+'<div style="font-size:10px;margin-bottom:2px;opacity:0.75">'+cat+'</div>'+ch+'<div style="font-size:11px;opacity:0.85">'+text+'</div>';addB(mo,'feedback',msg,3)}else{const msg='<div style="font-weight:700;font-size:11px">✓ LOGGED &nbsp;'+t+'</div>'+'<div style="font-size:10px;margin-top:2px;opacity:0.7">'+cat+'</div>';addB(mo,'info',msg,4)}}catch(e){if(mo.lastChild)mo.lastChild.remove();addB(mo,'system','Error: '+e.message)}btn.disabled=false;inp.focus()}
function tryEx(el){document.getElementById('v-input').value=el.textContent;sendDemo()}
function resetDemo(){while(mc.children.length>0)mc.removeChild(mc.lastChild);while(mo.children.length>0)mo.removeChild(mo.lastChild);addB(mc,'system','Customer messages appear here');addB(mo,'system','Operator alerts appear here');demoCount=0;history=[];replyMode=false;document.getElementById('v-input').value=''}
function filterDemo(m){filterMode=m;document.getElementById('m-filt-crit').className='filter-btn'+(m==='critical'?' active':'');document.getElementById('m-filt-all').className='filter-btn'+(m==='all'?' active':'');mo.querySelectorAll('[data-tier]').forEach(b=>{const t=parseInt(b.getAttribute('data-tier')||'9');b.style.display=m==='all'||t<=2?'':'none'})}
function operatorCmd(raw){const cmd=(raw||'').trim().toUpperCase();const inp=document.getElementById('v-op-inp')||document.getElementById('operator-inp');if(inp)inp.value='';if(!cmd)return;if(replyMode){if(cmd==='NEVERMIND'){replyMode=false;addB(mo,'resp','Reply cancelled.');if(inp)inp.placeholder='Type a command...';return}replyMode=false;addB(mo,'cmd',raw.trim());addB(mo,'resp','Reply sent. AI quiet for 15min.');addB(mc,'in',raw.trim());if(inp)inp.placeholder='Type a command...';return}addB(mo,'cmd',raw.trim());if(!lastData&&cmd!=='MENU'){addB(mo,'resp','No active alerts.');return}if(cmd==='REPLY'){if(!lastData){addB(mo,'resp','No messages to reply to.');return}replyMode=true;addB(mo,'resp','Replying to: \"'+(lastData.original_message||'last message').slice(0,50)+'\"\\nType your reply now, or NEVERMIND.');if(inp){inp.placeholder='Type your reply...';inp.focus();}return}if(cmd==='CLOSE'){addB(mo,'resp','Conversation closed. AI auto-replies resumed.');replyMode=false;return}if(cmd==='MENU'||cmd==='?'){addB(mo,'resp','REPLY — Reply to last customer\\nCLOSE — End conversation\\nPAUSE / RESUME\\nMENU — This list');return}if(cmd==='PAUSE'){addB(mo,'resp','Alerts PAUSED. Reply RESUME to turn back on.');return}if(cmd==='RESUME'){addB(mo,'resp','Alerts resumed.');return}addB(mo,'resp','Unknown command. Reply MENU for help.');}"""

    steps_html = f'''<div class="hiw-steps">
<div class="hiw-step"><div class="hiw-num">1</div><div><strong>{step1[0]}</strong><p>{step1[1]}</p></div></div>
<div class="hiw-step"><div class="hiw-num">2</div><div><strong>{step2[0]}</strong><p>{step2[1]}</p></div></div>
<div class="hiw-step"><div class="hiw-num">3</div><div><strong>{step3[0]}</strong><p>{step3[1]}</p></div></div>
</div>'''

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline for {plural or label+"s"}</title><link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet"><style>
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}}a{{color:#ea580c;text-decoration:none}}
{NAV_CSS}
.hero{{text-align:center;padding:40px 24px 20px;max-width:700px;margin:0 auto}}.v-label{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#ea580c;margin-bottom:10px}}
h1{{font-size:clamp(28px,5vw,40px);font-weight:700;line-height:1.15;margin-bottom:12px;letter-spacing:-0.02em;color:#1a1a1a}}h1 em{{font-style:normal;color:#ea580c}}.sub{{font-size:16px;color:#888;max-width:480px;margin:0 auto 20px}}.urgency{{font-size:13px;color:#aaa;margin-bottom:8px}}.panel-label{{font-size:12px;font-weight:500;color:#bbb;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.06em}}
.phones{{display:flex;gap:24px;margin:0 auto 20px;justify-content:center;align-items:flex-start;max-width:860px;padding:0 20px}}.device{{width:320px;flex-shrink:0}}.frame{{background:#fff;border-radius:36px;border:3px solid #e0e0dc;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,0.08)}}.notch{{width:100px;height:28px;background:#fff;border-radius:0 0 16px 16px;margin:0 auto;position:relative;z-index:2}}.notch::before{{content:'';width:8px;height:8px;background:#e8e8e4;border-radius:50%;position:absolute;right:20px;top:8px}}.statusbar{{display:flex;justify-content:space-between;padding:2px 20px 6px;font-size:11px;color:#aaa;margin-top:-10px}}.phone-label-bar{{text-align:center;padding:6px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.06em;border-bottom:1px solid #f0f0ec}}.phone-label-bar.customer{{color:#2563eb}}.phone-label-bar.operator{{color:#ea580c}}.pref-bar{{display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 20px;flex-wrap:wrap}}.pref-label{{font-size:13px;color:#888;font-weight:500}}.filter-btn{{font-size:12px;padding:6px 14px;border-radius:6px;border:1px solid #e0e0dc;background:#fff;color:#888;cursor:pointer;font-family:inherit;font-weight:600;transition:all 0.2s}}.filter-btn.active{{background:#ea580c;color:#fff;border-color:#ea580c}}
.msgs{{height:320px;overflow-y:auto;padding:12px 14px;background:#fafaf8}}.bubble{{padding:9px 13px;border-radius:16px;font-size:13px;margin-bottom:7px;max-width:88%;line-height:1.45;animation:fadeUp 0.3s ease both}}.bubble.in{{background:#e8e8e4;color:#333;border-bottom-left-radius:4px}}.bubble.out-blue{{background:#2563eb;color:#fff;margin-left:auto;border-bottom-right-radius:4px}}.bubble.alert{{background:#fff7ed;border:1px solid #fed7aa;color:#b45309;border-bottom-left-radius:4px}}.bubble.alert-red{{background:#fef2f2;border:1px solid #fecaca;color:#dc2626;border-bottom-left-radius:4px}}.bubble.feedback{{background:#fefce8;border:1px solid #fef08a;color:#a16207;border-bottom-left-radius:4px}}.bubble.info{{background:#f0f0ec;color:#666;border-bottom-left-radius:4px}}.bubble.system{{background:#f0f0ec;color:#999;font-size:11px;text-align:center;max-width:100%;border-radius:8px;padding:6px 10px}}.bubble .lbl{{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:#aaa;margin-bottom:3px}}.meta{{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}}.tag{{font-size:10px;padding:2px 7px;border-radius:4px;font-weight:500}}.tag.t1{{background:#fee2e2;color:#dc2626}}.tag.t2{{background:#fff7ed;color:#b45309}}.tag.t3{{background:#fef9c3;color:#a16207}}.tag.t4{{background:#f0f0ec;color:#888}}@keyframes fadeUp{{from{{opacity:0;transform:translateY(6px)}}to{{opacity:1;transform:none}}}}.input-area{{padding:8px 12px 12px;border-top:1px solid #f0f0ec;background:#fff}}.input-row{{display:flex;gap:6px}}.input-row input{{flex:1;padding:10px 12px;background:#f5f5f0;border:1px solid #e0e0dc;border-radius:20px;font-size:14px;color:#1a1a1a;font-family:inherit}}.input-row input::placeholder{{color:#bbb}}.input-row input:focus{{outline:none;border-color:#ea580c}}.input-row button{{padding:10px 14px;border-radius:50%;border:none;font-size:16px;cursor:pointer;width:40px;height:40px;display:flex;align-items:center;justify-content:center}}.input-row button.blue{{background:#2563eb;color:#fff}}.input-row button.orange{{background:#ea580c;color:#fff}}.input-row button:disabled{{opacity:0.3;cursor:not-allowed}}.operator-cmds{{display:none;padding:4px 12px 6px;gap:5px;flex-wrap:wrap;background:#fff}}.home-bar{{width:120px;height:4px;background:#ddd;border-radius:2px;margin:8px auto 10px}}.examples{{margin-bottom:20px;padding:0 20px}}.examples p{{font-size:12px;color:#aaa;margin-bottom:6px;text-align:center}}.ex-row{{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;max-width:700px;margin:0 auto}}.ex{{font-size:12px;padding:6px 10px;background:#fff;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,0.04)}}.ex:hover{{border-color:#2563eb;color:#1a1a1a}}.cta{{text-align:center;margin:24px 0;padding:0 20px}}.cta a{{display:inline-block;padding:14px 32px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px}}footer{{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}}.spinner{{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:4px}}@keyframes spin{{to{{transform:rotate(360deg)}}}}.howitworks{{max-width:640px;margin:0 auto;padding:0 20px 28px}}.hiw-steps{{display:flex;flex-direction:column;gap:14px}}.hiw-step{{display:flex;align-items:flex-start;gap:14px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px}}.hiw-num{{width:28px;height:28px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:13px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}}.hiw-step strong{{font-size:14px;display:block;margin-bottom:2px}}.hiw-step p{{font-size:13px;color:#888;margin:0;line-height:1.4}}@media(max-width:700px){{.phones{{flex-direction:column;align-items:center}}.device{{width:100%;max-width:360px}}}}
</style></head><body>
{NAV_HTML}
<div class="hero">
<p class="v-label">Hotline for {plural or label+"s"}</p>
<h1>{headline}</h1>
<p class="sub">{sub}</p>
<p class="urgency">No app. No software. No setup. Works by text.</p>
</div>

<div class="examples">
<p class="panel-label">See it in action — try a scenario</p>
<div class="ex-row">{ex_chips_html}</div>
<div style="text-align:center;margin-top:16px"><button onclick="resetDemo()" style="padding:6px 12px;background:#f0f0f0;color:#666;border:1px solid #e0e0dc;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer">Reset</button></div>
</div>

<div class="phones">
<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G&nbsp;88%</span></div>
<div class="phone-label-bar customer">Customer</div>
<div class="msgs" id="v-cust"><div class="bubble system">Customer messages appear here</div></div>
<div class="input-area"><div class="input-row"><input id="v-input" type="text" placeholder="Type a message..."><button id="v-btn" class="blue" onclick="sendDemo()" style="margin:0">▲</button></div></div>
<div class="home-bar"></div>
</div></div>

<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G&nbsp;92%</span></div>
<div class="phone-label-bar operator">Operator</div>
<div class="pref-bar"><div class="pref-label">Alert level:</div><button class="filter-btn active" id="m-filt-crit" onclick="filterDemo('critical')">Critical only</button><button class="filter-btn" id="m-filt-all" onclick="filterDemo('all')">All messages</button></div>
<div class="msgs" id="v-oper"><div class="bubble system">Operator alerts appear here</div></div>
<div class="input-area"><div class="input-row"><input style="flex:1" type="text" placeholder="Type a message..."><button class="blue" style="margin:0">▲</button></div></div>
<div class="home-bar"></div>
</div></div>
</div>

<div class="cta"><a href="/signup">Get Hotline for your {(plural or label+"s").lower()} →</a></div>

<div class="howitworks" style="margin-top:40px;border-top:1px solid #e0e0dc;padding-top:28px">
<h2 style="font-size:20px;font-weight:700;margin-bottom:20px;text-align:center">Where to display your Hotline</h2>
<p style="text-align:center;font-size:14px;color:#888;margin-bottom:16px">Customers scan from any location — choose what works best for your operation.</p>
<div class="placement-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">
{placements_html}
</div>
</div>

<div class="howitworks">
{steps_html}
</div>

<footer>8405 Siskin CV, Austin TX 78745 · <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>

<script>
{DEMO_JS}
</script>
</body></html>'''
    
    return html


VERTICAL_LAUNDROMAT_HTML = _make_vertical_page(
    slug="laundromat",
    label="Laundromat",
    headline="Your machines break. Your customers leave. You find out tomorrow.",
    sub="Hotline alerts you the moment something goes wrong — broken washers, flooded floors, locked doors — before it costs you.",
    scenarios=[
        "Washer #4 is leaking all over the floor",
        "Dryer 7 took my money but won\'t start",
        "The front door is locked, I can\'t get in",
        "Change machine won\'t accept my bill",
        "The bathroom is flooding",
        "Washer 2 won\'t spin — clothes soaking wet",
        "All the soap dispensers are empty",
        "Parking lot exit is blocked",
    ],
    placements=["Washer", "Dryer", "Coin dispenser", "Wall by entrance"],
    step1=("Display your Hotline", "Takes 60 seconds. Works on every machine, door, and wall in your laundromat."),
    step2=("Customers text when something\'s wrong", "No app needed. They text the number on the sign. Every message is read automatically."),
    step3=("You get alerted instantly by text", "Critical issues go straight to your phone. You reply by text. Done."),
    plural="Laundromats"
)

VERTICAL_CARWASH_HTML = _make_vertical_page(
    slug="carwash",
    label="Car Wash",
    headline="Your wash is down. Customers are leaving. You have no idea.",
    sub="Whether you run self-serve bays or an unattended tunnel — Hotline tells you the moment payment fails, equipment jams, or a customer is stuck.",
    scenarios=[
        "Bay 2 won\'t start after I paid",
        "Tunnel stopped with my car still inside",
        "Card reader on bay 3 isn\'t working",
        "Car is stuck, exit door won\'t open",
        "Vacuum on the right side is broken",
        "Exit gate is stuck closed",
        "Dryer at the end of the tunnel isn\'t working",
        "Touchscreen is frozen, can\'t select a wash",
    ],
    placements=["Payment booth", "Tunnel entrance", "Lane entrance", "Waiting area"],
    step1=("Display your Hotline", "One sign at entry and one at each bay or tunnel entrance covers your facility."),
    step2=("Customers text equipment issues instantly", "No app. No phone call. They text. Hotline categorizes urgency."),
    step3=("You get a text the moment something breaks", "Know before a line forms. Fix it before you lose the revenue."),
    plural="Car Washes"
)

VERTICAL_SELFSTORAGE_HTML = _make_vertical_page(
    slug="selfstorage",
    label="Self Storage",
    headline="Your gate is broken. Your tenant is locked out. You\'re the last to know.",
    sub="Hotline puts a direct line between your tenants and you — so access issues, lock problems, and facility failures don\'t spiral.",
    scenarios=[
        "Gate keypad isn\'t accepting my code",
        "My unit lock is jammed, I can\'t get in",
        "Elevator is out of service",
        "Hallway lights are out in building C",
        "There\'s water dripping from the ceiling near unit 42",
        "Security camera is visibly broken",
        "After-hours intercom isn\'t working",
        "Dumpster area is completely blocked",
    ],
    placements=["Gate entrance", "Office door", "Unit entrance", "Access kiosk"],
    step1=("Display your Hotline", "One sign at the gate, one at the office, one in each hallway."),
    step2=("Tenants text issues directly to you", "No hold music. No ignored voicemails. A text you actually see."),
    step3=("You get alerted before it becomes a complaint", "Resolve access issues fast. Protect your retention."),
    plural="Self Storage Facilities"
)

VERTICAL_GYM_HTML = _make_vertical_page(
    slug="gym",
    label="24/7 Gym",
    headline="Your equipment is down. Your members are frustrated. You\'re not there.",
    sub="Hotline gives your members a direct line to you — so broken equipment, access failures, and safety issues don\'t go unreported.",
    scenarios=[
        "Treadmill 4 is making a loud grinding noise",
        "My access fob isn\'t working at the front door",
        "Men\'s bathroom has water on the floor",
        "AC hasn\'t been working all morning",
        "Cable machine is broken, nobody fixed it",
        "Locker 22 is stuck and won\'t open",
        "Front door isn\'t locking after entry",
        "No hot water in the showers",
    ],
    placements=["Front desk", "Entrance", "Locker room", "Equipment area"],
    step1=("Display your Hotline", "One sign at the entrance, one near equipment, one in each locker room."),
    step2=("Members text issues the moment they happen", "No more angry reviews because no one to tell. They tell you."),
    step3=("You get alerted before it becomes a problem", "Fix equipment fast. Keep members happy. Protect your retention."),
    plural="24/7 Gyms"
)


VERTICAL_MHC_HTML = _make_vertical_page(
    slug="mhc",
    label="Mobile Home Parks",
    headline="A pipe bursts at lot 14. Your resident has no one to call. You find out when it floods.",
    sub="Hotline puts a direct line between your residents and you — so water, sewer, gate, and utility failures surface before they become liability claims or move-outs.",
    scenarios=[
        "There\'s sewage backing up into my yard",
        "Water main looks broken — water is bubbling up on the road",
        "The front gate won\'t open and I can\'t get in",
        "Streetlights on the back row have been out for days",
        "A tree limb fell and is blocking the road",
        "My water has been shut off all day with no notice",
        "The dumpster area is overflowing again",
        "Can we get the speed bumps repainted?",
    ],
    placements=["Park entrance", "Office window", "Mailbox kiosk", "Laundry / common area"],
    step1=("Display your Hotline", "One sign at the entrance, one at the office, one at the mailboxes. Covers the whole community."),
    step2=("Residents text issues directly to you", "No ignored voicemails. No after-hours service that never calls back. A text you actually see."),
    step3=("You get alerted before it becomes a claim", "Catch water, sewer, and access failures early. Protect habitability and retention."),
    plural="Mobile Home Parks"
)

VERTICAL_RVPARK_HTML = _make_vertical_page(
    slug="rvpark",
    label="RV Parks",
    headline="A hookup fails at site 22. Your guest is stranded. You\'re across the property.",
    sub="Hotline gives every site a direct line to you — so power, water, sewer, and gate problems get reported the moment they happen, not at checkout.",
    scenarios=[
        "No power at my site — breaker won\'t reset",
        "The sewer hookup at site 22 is leaking",
        "Water pressure dropped to nothing across the loop",
        "The bathhouse is out of hot water",
        "Entry gate code isn\'t working",
        "A branch came down on the road to the back loop",
        "WiFi has been down all evening",
        "Could you add a trash can near the dog run?",
    ],
    placements=["Park entrance", "Office / check-in", "Bathhouse", "Hookup pedestal"],
    step1=("Display your Hotline", "One sign at check-in, one at the bathhouse, one at each loop. Coverage everywhere guests are."),
    step2=("Guests text issues from their site", "No walking to the office. No app. They text the number on the sign and Hotline triages it."),
    step3=("You get alerted before the bad review", "Fix hookups and access fast — while the guest is still on-site, not after they\'ve left angry."),
    plural="RV Parks"
)

HOMEPAGE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotline — Real-Time Alerts for Offsite Operators</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.hero{text-align:center;padding:32px 24px 16px;max-width:640px;margin:0 auto}
.hero h1{font-size:clamp(28px,5vw,44px);font-weight:700;line-height:1.15;margin-bottom:10px;letter-spacing:-0.02em}
.hero h1 em{font-style:normal;color:#ea580c}
.hero p{font-size:16px;color:#888;margin-bottom:0}
.industry-bar{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;padding:20px 20px 4px;max-width:700px;margin:0 auto}
.ind-pill{padding:7px 16px;border-radius:99px;border:1.5px solid #e0e0dc;background:#fff;font-size:13px;font-weight:600;color:#888;cursor:pointer;transition:all 0.15s;white-space:nowrap}
.ind-pill.active{background:#ea580c;border-color:#ea580c;color:#fff}
.ind-pill:hover:not(.active){border-color:#ea580c;color:#ea580c}
.try-label{text-align:center;font-size:12px;color:#bbb;padding:10px 0 4px;letter-spacing:0.04em}
.phones{display:flex;gap:20px;margin:0 auto 16px;justify-content:center;align-items:flex-start;max-width:960px;padding:0 16px}
.device{width:380px;flex-shrink:0}
.frame{background:#fff;border-radius:36px;border:3px solid #e0e0dc;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,0.08)}
.notch{width:100px;height:28px;background:#fff;border-radius:0 0 16px 16px;margin:0 auto;position:relative;z-index:2}.notch::before{content:'';width:8px;height:8px;background:#e8e8e4;border-radius:50%;position:absolute;right:20px;top:8px}
.statusbar{display:flex;justify-content:space-between;padding:2px 20px 6px;font-size:11px;color:#aaa;margin-top:-10px}
.phone-label-bar{text-align:center;padding:6px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.06em;border-bottom:1px solid #f0f0ec}
.phone-label-bar.customer{color:#2563eb}.phone-label-bar.operator{color:#ea580c}
.filter-row{display:flex;align-items:center;justify-content:center;gap:8px;padding:6px 14px 4px;background:#fff8f5;border-bottom:1px solid #f0f0ec;font-size:11px;flex-wrap:wrap}
.filter-btn{font-size:11px;padding:3px 10px;border-radius:4px;border:1px solid #e0e0dc;background:#fff;color:#888;cursor:pointer;font-family:inherit;font-weight:600;transition:all 0.15s}
.filter-btn.active{background:#ea580c;color:#fff;border-color:#ea580c}
.msgs{height:310px;overflow-y:auto;padding:10px 12px;background:#fafaf8}
.bubble{padding:8px 12px;border-radius:14px;font-size:12px;margin-bottom:5px;max-width:90%;line-height:1.4;animation:fadeUp 0.3s ease both}
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
.operator-cmds{display:none;padding:4px 12px 6px;gap:5px;flex-wrap:wrap;background:#fff}
.cmd-btn{font-size:11px;padding:5px 10px;background:#f5f5f0;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;font-weight:600}.cmd-btn:hover{border-color:#ea580c;color:#1a1a1a}
.operator-input{display:none}.home-bar{width:120px;height:4px;background:#ddd;border-radius:2px;margin:8px auto 10px}
.ex-area{padding:0 20px 16px;max-width:700px;margin:0 auto}
.ex-row{display:flex;flex-wrap:wrap;gap:6px;justify-content:center}
.ex{font-size:12px;padding:6px 10px;background:#fff;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,0.04);transition:border-color 0.15s}
.ex:hover{border-color:#2563eb;color:#1a1a1a}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:4px}@keyframes spin{to{transform:rotate(360deg)}}
.features{max-width:700px;margin:32px auto;padding:0 24px;display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:640px){.features{grid-template-columns:repeat(2,1fr)}}
.feature{padding:14px 16px;background:#ea580c;border-radius:8px;text-align:left}
.feature-title{font-weight:700;font-size:14px;margin-bottom:4px;color:#fff}
.feature-desc{font-size:12px;color:rgba(255,255,255,0.88);line-height:1.4}
.cta-section{max-width:700px;margin:24px auto 40px;padding:32px 24px;background:#fff7ed;border-radius:12px;text-align:center;border:1px solid #fed7aa}
.cta-section h2{font-size:18px;font-weight:700;margin-bottom:6px;color:#1a1a1a}
.cta-section p{font-size:13px;color:#888;margin-bottom:16px}
.cta-section a{display:inline-block;padding:12px 28px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:15px;text-decoration:none}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
@media(max-width:700px){.phones{flex-direction:column;align-items:center}.device{width:100%;max-width:420px}.industry-bar{gap:6px}}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<h1>Operate from a distance &amp; never miss a <em>critical issue</em> again.</h1>
<p>Customers text. Hotline triages and alerts you — automatically.</p>
</div>
<div class="industry-bar">
<div class="ind-pill active" onclick="setIndustry('laundromat',this)">Laundromat</div>
<div class="ind-pill" onclick="setIndustry('selfstorage',this)">Self Storage</div>
<div class="ind-pill" onclick="setIndustry('mhc',this)">Mobile Home Parks</div>
<div class="ind-pill" onclick="setIndustry('gym',this)">24/7 Gym</div>
<div class="ind-pill" onclick="setIndustry('carwash',this)">Car Wash</div>
<div class="ind-pill" onclick="setIndustry('rvpark',this)">RV Parks</div>
</div>
<div style="display:flex;align-items:center;justify-content:center;gap:12px;padding:10px 0 4px"><p class="try-label" style="padding:0;margin:0">Try a scenario or type your own</p><button onclick="resetDemo()" style="padding:4px 10px;background:#f0f0f0;color:#999;border:1px solid #e0e0dc;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer">Reset</button></div>
<div class="ex-area" style="padding-bottom:12px">
<div class="ex-row" id="ex-row"></div>
</div>
<div class="phones">
<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G &nbsp; 87%</span></div>
<div class="phone-label-bar customer">Customer</div>
<div class="msgs" id="m-cust"><div class="bubble system">Tap a scenario or type a message below</div></div>
<div class="input-area"><div class="input-row">
<input type="text" id="cust-input" placeholder="Type a message..." onkeydown="if(event.key==='Enter')sendDemo()">
<button class="blue" id="cust-btn" onclick="sendDemo()">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
<div class="device"><div class="frame">
<div class="notch"></div><div class="statusbar"><span>9:41</span><span>5G &nbsp; 92%</span></div>
<div class="phone-label-bar operator">Operator</div>
<div class="filter-row"><span style="font-weight:600;color:#888;font-size:11px">Alert level:</span>
<button class="filter-btn active" id="h-filt-crit" onclick="setFilter('critical')">Critical only</button>
<button class="filter-btn" id="h-filt-all" onclick="setFilter('all')">All messages</button></div>
<div class="msgs" id="m-operator"><div class="bubble system">Operator alerts appear here</div></div>
<div class="operator-cmds" id="operator-cmds">
<div class="cmd-btn" onclick="operatorCmd('REPLY')">REPLY</div>
<div class="cmd-btn" onclick="operatorCmd('CLOSE')">CLOSE</div>
<div class="cmd-btn" onclick="operatorCmd('MENU')">MENU</div>
</div>
<div class="input-area operator-input" id="operator-input"><div class="input-row">
<input type="text" id="operator-inp" placeholder="Type a command..." onkeydown="if(event.key==='Enter')operatorCmd(this.value)">
<button class="orange" onclick="operatorCmd(document.getElementById('operator-inp').value)">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
</div>

<div class="features">
<div class="feature"><div class="feature-title">One-minute setup</div><div class="feature-desc">No app. No software. Works by text.</div></div>
<div class="feature"><div class="feature-title">Tier alerts only</div><div class="feature-desc">Critical issues reach you. Low-priority messages don't.</div></div>
<div class="feature"><div class="feature-title">Your number stays private</div><div class="feature-desc">Customers text a shared number. Yours never shows.</div></div>
<div class="feature"><div class="feature-title">Always on</div><div class="feature-desc">Works 24/7 even when you're not there.</div></div>
</div>
<div class="cta-section">
<h2>Try free for 14 days</h2>
<p>No credit card required.</p>
<a href="/signup">Get Started &rarr;</a>
</div>
<footer>Hotline &middot; Real-time alerts for offsite operators &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
<script>
const mc=document.getElementById('m-cust'),mo=document.getElementById('m-operator');
let lastData=null,replyMode=false,history=[],demoCount=0,maxDemo=10,filterMode='critical';

const CHIPS={
  laundromat:[
    "Water is pouring out from under washer #4",
    "Dryer 7 took my money and won't start",
    "The change machine is out of quarters",
    "Door lock is broken and won't open",
    "Soap dispenser is empty on machine 2",
    "The TV in the waiting area is really loud"
  ],
  carwash:[
    "My car is stuck inside the tunnel right now",
    "Bay 2 won't start and I already paid",
    "The card reader isn't working on any bay",
    "Vacuum hose on bay 4 is completely broken",
    "There's no soap coming out in bay 1",
    "The trash can out front is overflowing"
  ],
  selfstorage:[
    "Water is actively leaking into my unit",
    "Gate keypad won't accept my access code",
    "The elevator has been out of service all day",
    "My unit door lock is jammed and won't open",
    "Hallway lights on floor 2 are all out",
    "Could you add a bench near the entrance?"
  ],
  mhc:[
    "There's sewage backing up into my yard",
    "Water main looks broken — water is bubbling up on the road",
    "The front gate won't open and I can't get in",
    "Streetlights on the back row have been out for days",
    "My water has been shut off all day with no notice",
    "Can we get the speed bumps repainted?"
  ],
  rvpark:[
    "No power at my site — breaker won't reset",
    "The sewer hookup at site 22 is leaking",
    "Water pressure dropped to nothing across the loop",
    "The bathhouse is out of hot water",
    "Entry gate code isn't working",
    "Could you add a trash can near the dog run?"
  ],
  gym:[
    "Someone is having a medical emergency near the squat rack",
    "My access fob stopped working at the front door",
    "Treadmill 3 is making a loud grinding noise",
    "There's no hot water in the men's showers",
    "The cable on the lat pulldown machine snapped",
    "Can you add more paper towels near the free weights?"
  ]
};

function setIndustry(key,el){
  document.querySelectorAll('.ind-pill').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  renderChips(key);
}

function renderChips(key){
  const row=document.getElementById('ex-row');
  row.innerHTML=(CHIPS[key]||CHIPS.laundromat).map(t=>`<div class="ex" onclick="tryEx(this)">${t}</div>`).join('');
}

function tryEx(el){document.getElementById('cust-input').value=el.textContent;sendDemo();}

function addB(cont,cls,html){
  const d=document.createElement('div');d.className='bubble '+cls;
  if(cls==='alert-red')d.setAttribute('data-tier','1');
  else if(cls==='alert')d.setAttribute('data-tier','2');
  else if(cls==='feedback')d.setAttribute('data-tier','3');
  else if(cls==='info')d.setAttribute('data-tier','4');
  d.innerHTML=html;cont.appendChild(d);cont.scrollTop=cont.scrollHeight;
  if(cont===mo)applyFilter();
}

function applyFilter(){
  mo.querySelectorAll('.bubble[data-tier]').forEach(b=>{
    const t=parseInt(b.getAttribute('data-tier'));
    b.style.display=(filterMode==='all'||t<=2)?'':'none';
  });
}

function setFilter(m){
  filterMode=m;
  document.getElementById('h-filt-crit').className='filter-btn'+(m==='critical'?' active':'');
  document.getElementById('h-filt-all').className='filter-btn'+(m==='all'?' active':'');
  applyFilter();
}

function resetDemo(){
  [mc,mo].forEach(c=>{while(c.firstChild)c.removeChild(c.firstChild)});
  addB(mc,'system','Tap a scenario or type a message below');
  addB(mo,'system','Operator alerts appear here');
  demoCount=0;history=[];replyMode=false;lastData=null;
  document.getElementById('cust-input').value='';
  document.getElementById('operator-cmds').style.display='none';
  document.getElementById('operator-input').style.display='none';
}

function operatorCmd(raw){
  const cmd=(raw||'').trim().toUpperCase();
  const inp=document.getElementById('operator-inp');
  if(inp)inp.value='';
  if(!cmd)return;
  if(replyMode){
    if(cmd==='NEVERMIND'){replyMode=false;addB(mo,'resp','Reply cancelled.');if(inp)inp.placeholder='Type a command...';return}
    replyMode=false;
    addB(mo,'cmd',raw.trim());
    addB(mo,'resp','Reply sent. AI quiet for 15 min.');
    addB(mc,'in',raw.trim());
    if(inp)inp.placeholder='Type a command...';
    return;
  }
  addB(mo,'cmd',raw.trim());
  if(!lastData&&cmd!=='MENU'){addB(mo,'resp','No active alerts.');return}
  if(cmd==='REPLY'){
    if(!lastData){addB(mo,'resp','No messages to reply to.');return}
    replyMode=true;
    const preview=(lastData.original_message||'last message').slice(0,50);
    addB(mo,'resp',`Replying to: "${preview}"\\nType your reply now, or NEVERMIND.`);
    document.getElementById('operator-input').style.display='block';
    if(inp){inp.placeholder='Type your reply...';inp.focus();}
    return;
  }
  if(cmd==='CLOSE'){addB(mo,'resp','Conversation closed. AI auto-replies resumed.');replyMode=false;document.getElementById('operator-input').style.display='none';return}
  if(cmd==='MENU'||cmd==='?'){addB(mo,'resp','REPLY — Reply to last customer\\nCLOSE — End conversation\\nPAUSE / RESUME\\nMENU — This list');return}
  if(cmd==='PAUSE'){addB(mo,'resp','Alerts PAUSED. Reply RESUME to turn back on.');return}
  if(cmd==='RESUME'){addB(mo,'resp','Alerts resumed.');return}
  addB(mo,'resp','Unknown command. Reply MENU for help.');
}

async function sendDemo(){
  const inp=document.getElementById('cust-input'),btn=document.getElementById('cust-btn');
  const text=inp.value.trim();if(!text)return;
  if(demoCount>=maxDemo){addB(mc,'system','Demo limit reached. <a href="/signup" style="color:#ea580c">Sign up free &rarr;</a>');return}
  inp.value='';btn.disabled=true;demoCount++;replyMode=false;
  addB(mc,'out-blue',text);
  addB(mo,'system','<span class="spinner"></span> Processing...');
  try{
    const r=await fetch('/demo/classify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,history})});
    const d=await r.json();lastData=d;
    if(mo.lastChild&&mo.lastChild.classList.contains('system'))mo.removeChild(mo.lastChild);
    const reply=d.auto_reply||'Thanks for letting us know.';
    const cat=(d.category||'general').replace(/_/g,' ');
    const concern=d.concern||d.explanation||'';
    history.push({customer:text,reply});if(history.length>6)history.shift();
    await new Promise(r=>setTimeout(r,250));addB(mc,'in',reply);
    await new Promise(r=>setTimeout(r,350));
    const t=new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
    if(d.tier===1){
      const ch=concern?'Concern: '+concern+'<br><br>':'';
      addB(mo,'alert-red','<div style="font-weight:600;font-size:11px;margin-bottom:4px">🚨 URGENT ('+t+')</div>Category: '+cat+'<br><br>'+ch+'<strong>Customer:</strong><br>'+text+'<br><br><strong>We replied:</strong><br>'+reply+'<div style="margin-top:6px;font-size:10px;opacity:0.8">Reply REPLY to message customer back.</div>');
      document.getElementById('operator-cmds').style.display='flex';
    }else if(d.tier===2){
      const ch=concern?'Concern: '+concern+'<br><br>':'';
      addB(mo,'alert','<div style="font-weight:600;font-size:11px;margin-bottom:4px">⚠️ ISSUE ('+t+')</div>Category: '+cat+'<br><br>'+ch+'<strong>Customer:</strong><br>'+text+'<br><br><strong>We replied:</strong><br>'+reply+'<div style="margin-top:6px;font-size:10px;opacity:0.8">Reply REPLY to message customer back.</div>');
      document.getElementById('operator-cmds').style.display='flex';
    }else if(d.tier===3){
      const ch=concern?'Concern: '+concern+'<br><br>':'';
      addB(mo,'feedback','<div style="font-weight:600;font-size:11px;margin-bottom:4px">ℹ️ FEEDBACK ('+t+')</div>Category: '+cat+'<br><br>'+ch);
    }else{
      addB(mo,'info','<div style="font-weight:600;font-size:11px;margin-bottom:4px">✓ LOGGED ('+t+')</div>Category: '+cat);
    }
  }catch(e){if(mo.lastChild&&mo.lastChild.classList.contains('system'))mo.removeChild(mo.lastChild);addB(mo,'system','Error: '+e.message)}
  btn.disabled=false;inp.focus();
}

// Init
renderChips('laundromat');
</script></body></html>"""

DEMO_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotline — Stop losing customers to fixable problems</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.top{text-align:center;padding:32px 24px 20px;max-width:640px;margin:0 auto}
h1{font-size:clamp(28px,5vw,40px);font-weight:700;line-height:1.15;margin-bottom:12px;letter-spacing:-0.02em;color:#1a1a1a}h1 em{font-style:normal;color:#ea580c}
.sub{font-size:16px;color:#888;max-width:480px;margin:0 auto 20px}
.phones{display:flex;gap:24px;margin:0 auto 20px;justify-content:center;align-items:flex-start;max-width:860px;padding:0 20px}
.device{width:380px;flex-shrink:0}
.frame{background:#fff;border-radius:36px;border:3px solid #e0e0dc;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,0.08)}
.notch{width:100px;height:28px;background:#fff;border-radius:0 0 16px 16px;margin:0 auto;position:relative;z-index:2}.notch::before{content:'';width:8px;height:8px;background:#e8e8e4;border-radius:50%;position:absolute;right:20px;top:8px}
.statusbar{display:flex;justify-content:space-between;padding:2px 20px 6px;font-size:11px;color:#aaa;margin-top:-10px}
.phone-label-bar{text-align:center;padding:6px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.06em;border-bottom:1px solid #f0f0ec}
.phone-label-bar.customer{color:#2563eb}.phone-label-bar.operator{color:#ea580c}
.pref-bar{display:flex;align-items:center;justify-content:center;gap:8px;padding:12px 20px;flex-wrap:wrap}
.pref-label{font-size:13px;color:#888;font-weight:500}
.filter-btn{font-size:12px;padding:6px 14px;border-radius:6px;border:1px solid #e0e0dc;background:#fff;color:#888;cursor:pointer;font-family:inherit;font-weight:600;transition:all 0.2s}
.filter-btn.active{background:#ea580c;color:#fff;border-color:#ea580c}

.msgs{height:320px;overflow-y:auto;padding:12px 14px;background:#fafaf8}
.bubble{padding:8px 12px;border-radius:14px;font-size:12px;margin-bottom:5px;max-width:90%;line-height:1.4;animation:fadeUp 0.3s ease both}
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
.operator-cmds{display:none;padding:4px 12px 6px;gap:5px;flex-wrap:wrap;background:#fff}
.cmd-btn{font-size:11px;padding:5px 10px;background:#f5f5f0;border:1px solid #e0e0dc;border-radius:6px;color:#666;cursor:pointer;font-family:monospace;font-weight:600}.cmd-btn:hover{border-color:#ea580c;color:#1a1a1a}
.operator-input{display:none}.home-bar{width:120px;height:4px;background:#ddd;border-radius:2px;margin:8px auto 10px}
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
<h1 style="max-width:800px;margin:0 auto 12px;font-size:clamp(28px,4vw,44px);line-height:1.15">Know when your business needs you.<br><em>Hotline handles the rest.</em></h1>
<p class="sub">Customers text. Hotline alerts you when something actually needs your attention.</p>
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
<div class="phone-label-bar operator">Operator</div>
<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 14px 4px;background:#fff8f5;border-bottom:1px solid #f0f0ec;font-size:11px;color:#aaa;gap:6px"><span style="font-weight:600;color:#888;white-space:nowrap">Alert level:</span><div style="display:flex;gap:4px"><button class="filter-btn active" id="filt-crit" onclick="setFilter('critical')" style="font-size:10px;padding:3px 10px;border-radius:4px">🔴 Critical only</button><button class="filter-btn" id="filt-all" onclick="setFilter('all')" style="font-size:10px;padding:3px 10px;border-radius:4px">📋 All messages</button></div></div>
<div class="msgs" id="m-operator"><div class="bubble system">Operator alerts appear here</div></div>
<div class="operator-cmds" id="operator-cmds">
<div class="cmd-btn" onclick="operatorCmd('REPLY')">REPLY</div>
<div class="cmd-btn" onclick="operatorCmd('CLOSE')">CLOSE</div>
<div class="cmd-btn" onclick="operatorCmd('MENU')">MENU</div>
</div>
<div class="input-area operator-input" id="operator-input"><div class="input-row">
<input type="text" id="operator-inp" placeholder="Type a command..." onkeydown="if(event.key==='Enter')operatorCmd(this.value)">
<button class="orange" onclick="operatorCmd(document.getElementById('operator-inp').value)">&#9650;</button>
</div></div><div class="home-bar"></div>
</div></div>
</div>


<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>

<footer>Hotline &middot; Real-time SMS alerts for offsite operators &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa;display:inline-flex;align-items:center;gap:4px;vertical-align:middle"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/></svg>Instagram</a></footer>
<script>
let lastData=null,replyMode=false,history=[],demoCount=0,maxDemo=10,filterMode='critical';
const mc=document.getElementById('m-cust'),mo=document.getElementById('m-operator');
function addB(c,cls,label,text,tier){const d=document.createElement('div');d.className='bubble '+cls;if(tier)d.setAttribute('data-tier',tier);let h='';if(label)h+='<div class="lbl">'+label+'</div>';h+=text.replace(/\\n/g,'<br>');d.innerHTML=h;c.appendChild(d);c.scrollTop=c.scrollHeight;applyFilter();return d}
function tryEx(el){document.getElementById('cust-input').value=el.textContent;sendDemo()}
function showOperatorInput(){document.getElementById('operator-cmds').style.display='flex';document.getElementById('operator-input').style.display='block'}
function hideOperatorInput(){document.getElementById('operator-cmds').style.display='none';document.getElementById('operator-input').style.display='none'}
function resetDemo(){history=[];lastData=null;replyMode=false;demoCount=0;mc.innerHTML='<div class="bubble system">Customer messages appear here</div>';mo.innerHTML='<div class="bubble system">Operator alerts appear here</div>';document.getElementById('cust-input').value='';document.getElementById('operator-inp').value='';hideOperatorInput();addB(mo,'resp','','Conversation reset. Ready for a new scenario.')}
function setFilter(mode){filterMode=mode;document.getElementById('filt-all').className='filter-btn'+(mode==='all'?' active':'');document.getElementById('filt-crit').className='filter-btn'+(mode==='critical'?' active':'');applyFilter()}
function applyFilter(){mo.querySelectorAll('.bubble[data-tier]').forEach(function(b){var t=parseInt(b.getAttribute('data-tier'));b.style.display=(filterMode==='all'||t<=2)?'':'none'})}
function fmtTime(){return new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})}

(function(){document.getElementById('filt-crit').classList.add('active')})();

function operatorCmd(raw){
  const cmd=(raw||'').trim().toUpperCase();
  const inp=document.getElementById('operator-inp');
  inp.value='';
  if(!cmd)return;

  // In reply mode: any non-command text goes to customer
  if(replyMode){
    if(cmd==='NEVERMIND'){replyMode=false;addB(mo,'resp','','Reply cancelled.');inp.placeholder='Type a command...';return}
    if(cmd==='CLOSE'){replyMode=false;addB(mo,'resp','','Conversation closed. AI auto-replies resumed.');inp.placeholder='Type a command...';return}
    replyMode=false;
    addB(mo,'cmd','',raw.trim());
    addB(mo,'resp','','Reply sent. AI quiet for 15min.\\nType CLOSE when done, or just let it time out.');
    addB(mc,'in','Operator reply',raw.trim());
    inp.placeholder='Type a command...';
    return;
  }

  addB(mo,'cmd','',raw.trim());
  if(!lastData&&cmd!=='MENU'){addB(mo,'resp','','No active alerts.');return}

  if(cmd==='REPLY'){
    if(!lastData){addB(mo,'resp','','No messages to reply to.');return}
    replyMode=true;
    addB(mo,'resp','','Replying to: "'+lastData.original_message.slice(0,60)+'"\\nType your reply now, or NEVERMIND.\\nType CLOSE when finished to close the line with customer.');
    inp.placeholder='Type your reply...';
    inp.focus();
    return;
  }
  if(cmd==='CLOSE'){addB(mo,'resp','','Conversation closed. AI auto-replies resumed.');return}
  if(cmd==='MENU'||cmd==='?'){
    addB(mo,'resp','','Commands:\\nREPLY \u2014 Reply to last customer\\nCLOSE \u2014 End conversation\\nSTATUS \u2014 Alert status + level\\nALERTS \u2014 Change alert level\\nTIER2 \u2014 Critical only\\nTIER3 \u2014 Add reputation alerts\\nPAUSE / RESUME\\nBILLING \u2014 Subscription\\nMENU \u2014 This message');
    return;
  }
  if(cmd==='STATUS'){addB(mo,'resp','','&#128276; Alerts ON.\\nAlert level: Tier 2 critical only\\nReply ALERTS to change.');return}
  if(cmd==='PAUSE'){addB(mo,'resp','','&#128244; Alerts PAUSED. Reply RESUME to turn back on.');return}
  if(cmd==='RESUME'){addB(mo,'resp','','&#128276; Alerts resumed.');return}
  addB(mo,'resp','','Unknown command. Reply MENU for commands.');
}

async function sendDemo(){
  const inp=document.getElementById('cust-input');
  const btn=document.getElementById('cust-btn');
  const text=inp.value.trim();
  if(!text)return;
  if(demoCount>=maxDemo){addB(mc,'system','','Demo limit reached. <a href="/signup" style="color:#ea580c">Sign up</a> to get started!');return}
  inp.value='';btn.disabled=true;demoCount++;replyMode=false;
  addB(mc,'out-blue','',text);
  addB(mo,'system','','<span class="spinner"></span> Processing...');
  try{
    const r=await fetch('/demo/classify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,history:history})});
    const d=await r.json();d.original_message=text;lastData=d;
    mo.lastChild.remove();
    history.push({customer:text,reply:d.auto_reply});if(history.length>10)history.shift();
    await new Promise(r=>setTimeout(r,300));
    addB(mc,'in','Auto-reply',d.auto_reply);
    await new Promise(r=>setTimeout(r,400));
    const when=fmtTime();
    if(d.tier===1){
      const alert='🚨 URGENT ('+when+')\\nCategory: '+d.category.replace('_',' ')+'\\nConcern: '+d.explanation+'\\n\\nCustomer:\\n'+text+'\\n\\nWe replied:\\n'+d.auto_reply+'\\n\\nReply REPLY to message customer back.';
      addB(mo,'alert-red','',alert,1);showOperatorInput();
    } else if(d.tier===2){
      const alert='⚠️ Issue ('+when+')\\nCategory: '+d.category.replace('_',' ')+'\\nConcern: '+d.explanation+'\\n\\nCustomer:\\n'+text+'\\n\\nWe replied:\\n'+d.auto_reply+'\\n\\nReply REPLY to message customer back.';
      addB(mo,'alert','',alert,2);showOperatorInput();
    } else if(d.tier===3){
      const alert='💬 Feedback ('+when+')\\nCategory: '+d.category.replace('_',' ')+'\\nConcern: '+d.explanation+'\\n\\nCustomer:\\n'+text+'\\n\\nWe replied:\\n'+d.auto_reply;
      addB(mo,'feedback','',alert,3);showOperatorInput();
    } else {
      addB(mo,'info','','&#128172; '+d.summary,4);showOperatorInput();
    }
  }catch(e){mo.lastChild.remove();addB(mo,'system','','Demo error. Try again.')}
  btn.disabled=false;inp.focus();
}
</script></body></html>"""

@app.get("/demo")
def demo_page(): _ensure_init(); return Response(content=_ga(DEMO_HTML), media_type="text/html")


# --- How It Works page ---
HOW_IT_WORKS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>How It Works — Hotline</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#f8f8f6;color:#333}.nav{display:flex;align-items:center;padding:12px 24px;position:relative}.nav .logo{flex:1;text-align:center}.nav .logo svg{height:36px}.nav-links{position:absolute;right:24px;display:flex;gap:20px;align-items:center}.nav a{text-decoration:none;color:#333;font-size:13px;font-weight:500}.nav a.signup-btn{background:#ea580c;color:#fff;padding:8px 16px;border-radius:6px;font-weight:600}.container{max-width:700px;margin:40px auto;padding:0 24px}.hero{text-align:center;margin-bottom:40px}.hero h1{font-size:42px;font-weight:700;line-height:1.2;margin-bottom:16px;color:#1a1a1a}.hero p{font-size:16px;color:#666}.steps{display:flex;flex-direction:column;gap:30px;margin:50px 0}.step{display:flex;gap:20px}.step-num{flex-shrink:0;width:40px;height:40px;background:#ea580c;color:#fff;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px}.step-content h3{font-weight:600;font-size:16px;margin-bottom:8px;color:#1a1a1a}.step-content p{font-size:14px;color:#666;line-height:1.5}.section-divider{margin:40px 0;padding:40px 0;border-top:1px solid #e0e0dc}.placement-title{font-size:18px;font-weight:700;margin-bottom:24px;color:#1a1a1a;text-align:center}.placement-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:0}@media(max-width:700px){.placement-grid{grid-template-columns:repeat(2,1fr)}}.placement-card{padding:18px 20px;background:#fff;border-radius:8px;border:1px solid #e0e0dc}.placement-card h4{font-weight:700;font-size:14px;margin-bottom:6px}.placement-card p{font-size:13px;color:#666;margin:0;line-height:1.5}.placement-list{display:flex;flex-direction:column;gap:0;margin-bottom:40px;border:1px solid #e0e0dc;border-radius:10px;overflow:hidden}.placement-row{display:flex;align-items:baseline;padding:14px 20px;border-bottom:1px solid #e0e0dc;background:#fff}.placement-row:last-child{border-bottom:none}.placement-row:hover{background:#fff7ed;text-decoration:none}.placement-row{text-decoration:none;color:inherit}.placement-arrow{margin-left:auto;color:#ea580c;font-size:18px;font-weight:300;opacity:0.6}.placement-label{font-weight:700;font-size:14px;color:#ea580c;min-width:140px;flex-shrink:0}.placement-spots{font-size:13px;color:#666;line-height:1.5}.cta{text-align:center;padding:40px 24px;background:#fff7ed;border-radius:12px;border:1px solid #fed7aa}.cta h2{font-size:20px;font-weight:700;margin-bottom:8px;color:#1a1a1a}.cta p{font-size:14px;color:#888;margin-bottom:20px}.cta a{display:inline-block;padding:12px 28px;background:#ea580c;color:#fff;border-radius:6px;font-weight:700;text-decoration:none}.footer{margin-top:60px;padding-top:24px;border-top:1px solid #e0e0dc;text-align:center;font-size:13px;color:#999}a{color:#ea580c;text-decoration:none}.dropdown{position:relative;display:inline-block}.dropdown-menu{display:none;position:absolute;min-width:180px;z-index:100;top:100%;right:0;padding-top:8px}.dropdown-menu-inner{display:flex;flex-direction:column;gap:4px;background:#fff;box-shadow:0 8px 16px rgba(0,0,0,0.1);border-radius:8px;padding:8px;border:1px solid #e0e0dc}.dropdown-menu a{display:block;padding:8px 12px;border-radius:4px;transition:background 0.2s}.dropdown-menu a:hover{background:#f5f5f5}.dropdown:hover .dropdown-menu{display:block}</style></head><body><nav class="nav"><a href="/" class="logo"><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="300" viewBox="0 0 224.87999 67.499998" preserveAspectRatio="xMidYMid meet" version="1.0"><defs><clipPath id="d1h"><path d="M 0.765625 9 L 48 9 L 48 57 L 0.765625 57 Z M 0.765625 9 " clip-rule="nonzero"/></clipPath><clipPath id="d2h"><path d="M 208 20 L 223.992188 20 L 223.992188 45 L 208 45 Z M 208 20 " clip-rule="nonzero"/></clipPath></defs><g clip-path="url(#d1h)"><path fill="#ea580c" d="M 7.839844 9.40625 L 40.8125 9.40625 C 41.277344 9.40625 41.738281 9.449219 42.191406 9.542969 C 42.648438 9.632812 43.089844 9.765625 43.515625 9.945312 C 43.945312 10.121094 44.351562 10.339844 44.738281 10.597656 C 45.125 10.855469 45.480469 11.152344 45.808594 11.480469 C 46.136719 11.808594 46.429688 12.167969 46.6875 12.554688 C 46.945312 12.941406 47.164062 13.347656 47.339844 13.777344 C 47.519531 14.207031 47.652344 14.648438 47.742188 15.105469 C 47.832031 15.5625 47.878906 16.023438 47.878906 16.488281 L 47.878906 49.542969 C 47.878906 50.007812 47.832031 50.46875 47.742188 50.925781 C 47.652344 51.382812 47.519531 51.824219 47.339844 52.253906 C 47.164062 52.683594 46.945312 53.09375 46.6875 53.480469 C 46.429688 53.867188 46.136719 54.222656 45.808594 54.550781 C 45.480469 54.878906 45.125 55.175781 44.738281 55.433594 C 44.351562 55.691406 43.945312 55.910156 43.515625 56.085938 C 43.089844 56.265625 42.648438 56.398438 42.191406 56.492188 C 41.738281 56.582031 41.277344 56.625 40.8125 56.625 L 7.839844 56.625 C 7.378906 56.625 6.917969 56.582031 6.460938 56.492188 C 6.007812 56.398438 5.566406 56.265625 5.136719 56.085938 C 4.707031 55.910156 4.300781 55.691406 3.914062 55.433594 C 3.53125 55.175781 3.171875 54.878906 2.84375 54.550781 C 2.515625 54.222656 2.222656 53.867188 1.964844 53.480469 C 1.707031 53.09375 1.492188 52.683594 1.3125 52.253906 C 1.136719 51.824219 1 51.382812 0.910156 50.925781 C 0.820312 50.46875 0.777344 50.007812 0.777344 49.542969 L 0.777344 16.488281 C 0.777344 16.023438 0.820312 15.5625 0.910156 15.105469 C 1 14.648438 1.136719 14.207031 1.3125 13.777344 C 1.492188 13.347656 1.707031 12.941406 1.964844 12.554688 C 2.222656 12.167969 2.515625 11.808594 2.84375 11.480469 C 3.171875 11.152344 3.53125 10.855469 3.914062 10.597656 C 4.300781 10.339844 4.707031 10.121094 5.136719 9.945312 C 5.566406 9.765625 6.007812 9.632812 6.460938 9.542969 C 6.917969 9.449219 7.378906 9.40625 7.839844 9.40625 Z M 7.839844 9.40625 " fill-opacity="1" fill-rule="nonzero"/></g><g fill="#ffffff" fill-opacity="1"><g transform="translate(10.726965, 46.401259)"><path d="M 20.734375 -12.542969 L 8.230469 -12.542969 L 8.230469 0 L 3.109375 0 L 3.109375 -29.109375 L 8.175781 -29.109375 L 8.175781 -17.214844 L 20.734375 -17.214844 L 20.734375 -29.109375 L 25.816406 -29.109375 L 25.816406 0 L 20.734375 0 Z M 20.734375 -12.542969 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(62.007197, 44.82787)"><path d="M 17.277344 -10.453125 L 6.859375 -10.453125 L 6.859375 0 L 2.589844 0 L 2.589844 -24.257812 L 6.8125 -24.257812 L 6.8125 -14.34375 L 17.277344 -14.34375 L 17.277344 -24.257812 L 21.515625 -24.257812 L 21.515625 0 L 17.277344 0 Z M 17.277344 -10.453125 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(89.370287, 44.82787)"><path d="M 12.859375 -24.640625 C 16.707031 -24.640625 19.71875 -23.261719 21.894531 -20.5 L 22.734375 -19.265625 C 23.976562 -17.132812 24.597656 -14.632812 24.597656 -11.769531 C 24.597656 -8.511719 23.691406 -5.726562 21.882812 -3.402344 C 21.433594 -2.824219 20.945312 -2.308594 20.40625 -1.859375 C 18.375 -0.136719 15.867188 0.722656 12.886719 0.722656 C 9.1875 0.722656 6.246094 -0.585938 4.0625 -3.203125 C 2.140625 -5.503906 1.179688 -8.421875 1.179688 -11.953125 C 1.179688 -16 2.40625 -19.210938 4.859375 -21.585938 C 6.980469 -23.625 9.648438 -24.640625 12.859375 -24.640625 Z M 12.859375 -20.75 C 10.363281 -20.75 8.425781 -19.769531 7.046875 -17.816406 C 5.949219 -16.25 5.402344 -14.292969 5.402344 -11.953125 C 5.402344 -8.839844 6.324219 -6.480469 8.167969 -4.867188 C 9.445312 -3.738281 11.019531 -3.171875 12.886719 -3.171875 C 15.363281 -3.171875 17.292969 -4.132812 18.675781 -6.050781 C 19.796875 -7.585938 20.359375 -9.511719 20.359375 -11.828125 C 20.359375 -15.089844 19.414062 -17.527344 17.523438 -19.136719 C 16.257812 -20.210938 14.699219 -20.75 12.859375 -20.75 Z M 12.859375 -20.75 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(118.49594, 44.82787)"><path d="M 12.421875 -20.363281 L 12.421875 0 L 8.203125 0 L 8.203125 -20.363281 L 0.660156 -20.363281 L 0.660156 -24.257812 L 19.921875 -24.257812 L 19.921875 -20.363281 Z M 12.421875 -20.363281 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(142.379885, 44.82787)"><path d="M 6.71875 -24.257812 L 6.71875 -3.894531 L 18.035156 -3.894531 L 18.035156 0 L 2.5 0 L 2.5 -24.257812 Z M 6.71875 -24.257812 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(164.53192, 44.82787)"><path d="M 3.128906 -24.257812 L 7.394531 -24.257812 L 7.394531 0 L 3.128906 0 Z M 3.128906 -24.257812 "/></g></g><g fill="#ea580c" fill-opacity="1"><g transform="translate(177.963102, 44.82787)"><path d="M 21.574219 -24.257812 L 21.574219 0 L 17.265625 0 L 6.445312 -17.007812 L 6.445312 0 L 2.375 0 L 2.375 -24.257812 L 6.5625 -24.257812 L 17.507812 -7.082031 L 17.507812 -24.257812 Z M 21.574219 -24.257812 "/></g></g><g clip-path="url(#d2h)"><g fill="#ea580c" fill-opacity="1"><g transform="translate(205.326192, 44.82787)"><path d="M 7.042969 -10.453125 L 7.042969 -3.894531 L 20.546875 -3.894531 L 20.546875 0 L 2.820312 0 L 2.820312 -24.257812 L 19.980469 -24.257812 L 19.980469 -20.363281 L 7.042969 -20.363281 L 7.042969 -14.34375 L 19.519531 -14.34375 L 19.519531 -10.453125 Z M 7.042969 -10.453125 "/></g></g></g></svg></a><div class="nav-links"><a href="/">Demo</a><a href="/how-it-works">How It Works</a><div class="dropdown"><a href="/industries">Who We Support</a><div class="dropdown-menu"><div class="dropdown-menu-inner"><a href="/laundromat">Laundromat</a><a href="/selfstorage">Self Storage</a><a href="/mhc">Mobile Home Parks</a><a href="/gym">24/7 Gym</a><a href="/carwash">Car Wash</a><a href="/rvpark">RV Parks</a></div></div></div><a href="/resources">Resources</a><a href="/signup" class="signup-btn">Sign Up</a></div></nav><div class="container"><div class="hero"><h1>How It Works</h1><p>Hotline reads, triages, and tiers every message automatically — so only what matters reaches you, instantly.</p></div><div class="steps"><div class="step"><div class="step-num">1</div><div class="step-content"><h3>Display your Hotline</h3><p>Put your Hotline where customers can find it — a QR code or text number, right where problems happen.</p></div></div><div class="step"><div class="step-num">2</div><div class="step-content"><h3>Customer texts — and gets an instant reply</h3><p>The moment something's wrong, they text. Hotline responds automatically in seconds, so the customer knows they've been heard.</p></div></div><div class="step"><div class="step-num">3</div><div class="step-content"><h3>Hotline triages and tiers it — automatically</h3><p>Every message is read and sorted in real time. A flooding bathroom (Tier 1) is not the same as a vending machine out of snacks (Tier 4) — and Hotline knows the difference.</p></div></div><div class="step"><div class="step-num">4</div><div class="step-content"><h3>You hear only what matters</h3><p>Critical issues reach you instantly with the customer's words and the tier. Low-priority feedback is logged, not pushed. No noise, no app, no dashboard.</p></div></div></div><div class="section-divider" style="border-top:none;padding-top:0;margin-top:0"><h2 class="placement-title">Manage everything by text.</h2><p style="text-align:center;font-size:14px;color:#888;margin:-16px 0 28px">No app. No dashboard. No login. Your phone is the dashboard.</p><div style="background:#1a1a1a;border-radius:14px;overflow:hidden"><div style="display:flex;align-items:center;gap:16px;padding:18px 24px;border-bottom:1px solid #2a2a2a"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">REPLY</span><span style="font-size:14px;color:#ccc">Open a direct line to the last customer</span></div><div style="display:flex;align-items:center;gap:16px;padding:18px 24px;border-bottom:1px solid #2a2a2a"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">CLOSE</span><span style="font-size:14px;color:#ccc">End the conversation, auto-replies resume</span></div><div style="display:flex;align-items:center;gap:16px;padding:18px 24px;border-bottom:1px solid #2a2a2a"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">STATUS</span><span style="font-size:14px;color:#ccc">See your current alert settings</span></div><div style="display:flex;align-items:center;gap:16px;padding:18px 24px;border-bottom:1px solid #2a2a2a"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">PAUSE&nbsp;/&nbsp;RESUME</span><span style="font-size:14px;color:#ccc">Stop or restart alerts</span></div><div style="display:flex;align-items:center;gap:16px;padding:18px 24px;border-bottom:1px solid #2a2a2a"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">TIER2&nbsp;/&nbsp;TIER3</span><span style="font-size:14px;color:#ccc">Switch between critical-only or all alerts</span></div><div style="display:flex;align-items:center;gap:16px;padding:18px 24px"><span style="background:#ea580c;color:#fff;font-size:11px;font-weight:700;letter-spacing:0.08em;padding:5px 12px;border-radius:6px;white-space:nowrap">MENU</span><span style="font-size:14px;color:#ccc">See all commands</span></div></div></div><div class="section-divider"><h2 class="placement-title" style="margin-bottom:16px">The four tiers</h2><div class="placement-grid" style="margin-bottom:48px"><div class="placement-card" style="border-left:3px solid #dc2626"><h4 style="color:#dc2626">Tier 1 · Urgent</h4><p style="font-size:13px;color:#666;margin:0;line-height:1.5">Safety, flooding, break-ins. Reaches you instantly.</p></div><div class="placement-card" style="border-left:3px solid #ea580c"><h4 style="color:#ea580c">Tier 2 · Issue</h4><p style="font-size:13px;color:#666;margin:0;line-height:1.5">Broken equipment, access problems. Sent right away.</p></div><div class="placement-card" style="border-left:3px solid #ca8a04"><h4 style="color:#ca8a04">Tier 3 · Feedback</h4><p style="font-size:13px;color:#666;margin:0;line-height:1.5">Complaints, suggestions. Logged for your digest.</p></div><div class="placement-card" style="border-left:3px solid #9ca3af"><h4 style="color:#6b7280">Tier 4 · Logged</h4><p style="font-size:13px;color:#666;margin:0;line-height:1.5">Minor notes, spam filtered. Recorded quietly.</p></div></div><h2 class="placement-title">Where to Display Your Hotline</h2><div class="placement-list"><a href="/laundromat" class="placement-row"><span class="placement-label">Laundromat</span><span class="placement-spots">Next to washers &nbsp;·&nbsp; By dryers &nbsp;·&nbsp; Coin dispenser &nbsp;·&nbsp; Entrance wall</span><span class="placement-arrow">&rsaquo;</span></a><a href="/selfstorage" class="placement-row"><span class="placement-label">Self Storage</span><span class="placement-spots">Gate entrance &nbsp;·&nbsp; Office door &nbsp;·&nbsp; Unit entrance &nbsp;·&nbsp; Access kiosk</span><span class="placement-arrow">&rsaquo;</span></a><a href="/mhc" class="placement-row"><span class="placement-label">Mobile Home Parks</span><span class="placement-spots">Park entrance &nbsp;·&nbsp; Office window &nbsp;·&nbsp; Mailbox kiosk &nbsp;·&nbsp; Common area</span><span class="placement-arrow">&rsaquo;</span></a><a href="/gym" class="placement-row"><span class="placement-label">24/7 Gym</span><span class="placement-spots">Front desk &nbsp;·&nbsp; Main entrance &nbsp;·&nbsp; Locker room &nbsp;·&nbsp; Equipment area</span><span class="placement-arrow">&rsaquo;</span></a><a href="/carwash" class="placement-row"><span class="placement-label">Car Wash</span><span class="placement-spots">Payment booth &nbsp;·&nbsp; Tunnel entrance &nbsp;·&nbsp; Lane entrance &nbsp;·&nbsp; Waiting area</span><span class="placement-arrow">&rsaquo;</span></a><a href="/rvpark" class="placement-row"><span class="placement-label">RV Parks</span><span class="placement-spots">Check-in &nbsp;·&nbsp; Bathhouse &nbsp;·&nbsp; Hookup pedestal &nbsp;·&nbsp; Loop entrance</span><span class="placement-arrow">&rsaquo;</span></a></div></div><div class="cta"><h2>Ready to get started?</h2><p>14-day free trial. No credit card required.</p><a href="/signup">Sign Up Now</a></div></div><div class="footer"><p>© Hotline. All rights reserved.</p></div></body></html>"""



@app.get("/how-it-works")
def how_it_works_page():
    _ensure_init()
    return Response(content=_ga(HOW_IT_WORKS_HTML), media_type="text/html")


# --- Industries page ---
INDUSTRIES_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Who We Support — Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',system-ui,sans-serif;background:#f8f8f6;color:#1a1a1a;-webkit-font-smoothing:antialiased}a{color:#ea580c;text-decoration:none}
""" + NAV_CSS + """
.hero{text-align:center;padding:52px 24px 40px;max-width:680px;margin:0 auto}
h1{font-size:clamp(26px,4.5vw,40px);font-weight:700;margin-bottom:14px;line-height:1.18;letter-spacing:-0.02em}
h1 em{font-style:normal;color:#ea580c}
.sub{font-size:16px;color:#666;line-height:1.6;max-width:560px;margin:0 auto}
.problem{max-width:720px;margin:0 auto;padding:0 24px 48px}
.problem-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:40px}
@media(max-width:600px){.problem-grid{grid-template-columns:1fr}}
.prob-card{background:#fff;border:1px solid #e0e0dc;border-radius:12px;padding:20px 22px}
.prob-card .icon{font-size:22px;margin-bottom:10px}
.prob-card h3{font-size:15px;font-weight:700;margin-bottom:6px}
.prob-card p{font-size:13px;color:#666;line-height:1.55;margin:0}
.solution{background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;padding:28px;margin-bottom:40px}
.solution h2{font-size:18px;font-weight:700;color:#c2410c;margin-bottom:14px}
.solution-steps{display:flex;flex-direction:column;gap:10px}
.sol-step{display:flex;align-items:flex-start;gap:12px}
.sol-num{width:24px;height:24px;border-radius:50%;background:#ea580c;color:#fff;font-weight:700;font-size:11px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;margin-top:1px}
.sol-step p{font-size:14px;color:#7c2d12;line-height:1.5;margin:0}
.sol-step strong{color:#9a3412}
.multi{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:20px 22px;margin-bottom:40px}
.multi h3{font-size:14px;font-weight:700;color:#166534;margin-bottom:6px}
.multi p{font-size:13px;color:#15803d;line-height:1.5;margin:0}
h2.sect{font-size:20px;font-weight:700;margin-bottom:6px;text-align:center}
.sect-sub{font-size:14px;color:#888;text-align:center;margin-bottom:20px}
.verticals{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:48px}
@media(max-width:700px){.verticals{grid-template-columns:1fr 1fr}}
.v-card{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:18px 14px;text-align:left;transition:border-color 0.15s,box-shadow 0.15s;display:block;color:#1a1a1a}
.v-card:hover{border-color:#ea580c;box-shadow:0 3px 12px rgba(234,88,12,0.1);color:#1a1a1a}
.v-card h3{font-size:14px;font-weight:700;margin-bottom:5px;color:#1a1a1a}
.v-card p{font-size:12px;color:#888;line-height:1.4;margin:0}
.v-card .v-cta{display:inline-block;margin-top:10px;font-size:11px;font-weight:700;color:#ea580c;text-transform:uppercase;letter-spacing:0.05em}
.cta-block{text-align:center;padding:0 24px 56px}
.cta-block a{display:inline-block;padding:15px 36px;background:#ea580c;color:#fff;border-radius:8px;font-weight:700;font-size:16px}
.cta-block .fine{display:block;margin-top:10px;font-size:13px;color:#aaa}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
</style></head><body>
""" + NAV_HTML + """
<div class="hero">
<h1>Built for owners who operate from a distance.</h1>
<p class="sub">You built your business to run lean. The problem is when it breaks down at 7pm on a Saturday, your customers have no one to tell — and you have no way to know.</p>
</div>

<div class="problem">

<h2 class="sect">The problem with operating remotely</h2>
<p class="sect-sub" style="margin-bottom:20px">Equipment fails silently. Customers leave frustrated. You find out too late.</p>

<div class="problem-grid">
<div class="prob-card">
<div class="icon">&#128683;</div>
<h3>No one to tell</h3>
<p>When there's no staff on site, customers who hit a problem have nowhere to go. Most don't call. They leave, post a review, and don't come back.</p>
</div>
<div class="prob-card">
<div class="icon">&#9201;</div>
<h3>You find out too late</h3>
<p>A jammed machine, a broken gate, a flooded bathroom — every minute you don't know is lost revenue and a worsening situation.</p>
</div>
<div class="prob-card">
<div class="icon">&#128247;</div>
<h3>Reviews before you can respond</h3>
<p>The first time you hear about a problem is often a 1-star review. By then the customer is gone and the damage is done.</p>
</div>
<div class="prob-card">
<div class="icon">&#128181;</div>
<h3>Silent revenue loss</h3>
<p>A broken pay kiosk or downed gate doesn't announce itself. You just notice the numbers look off at the end of the week.</p>
</div>
</div>

<div class="solution">
<h2>How Hotline fixes this</h2>
<div class="solution-steps">
<div class="sol-step"><div class="sol-num">1</div><p><strong>Display your Hotline.</strong> Put it up in 60 seconds. It gives customers a direct text line to you — right where they need it.</p></div>
<div class="sol-step"><div class="sol-num">2</div><p><strong>Customers text when something's wrong.</strong> No app. No account. They text the number on the sign. Every message gets read by AI.</p></div>
<div class="sol-step"><div class="sol-num">3</div><p><strong>Smart categorizations every message.</strong> Emergencies and equipment failures reach you within seconds. Routine feedback gets logged. Spam is filtered. You only hear what actually needs you.</p></div>
<div class="sol-step"><div class="sol-num">4</div><p><strong>You take action from anywhere.</strong> Get the alert, assess the situation, and act \u2014 call a vendor, reply to the customer, or head over yourself. No app, no dashboard, no login required.</p></div>
</div>
</div>

<div class="multi">
<h3>&#127970; Running multiple locations?</h3>
<p>Sign up each location separately — each gets its own sign and business code. All alerts route to the same phone number. One inbox, full visibility across every location.</p>
</div>

<h2 class="sect">Operations We Serve</h2>
<div class="verticals">
<a href="/laundromat" class="v-card"><h3>Laundromat</h3><p>Machine failures, leaks, access issues, coin jams</p><span class="v-cta">See it &rarr;</span></a>
<a href="/selfstorage" class="v-card"><h3>Self Storage</h3><p>Gate access, unit locks, leaks, after-hours issues</p><span class="v-cta">See it &rarr;</span></a>
<a href="/mhc" class="v-card"><h3>Mobile Home Parks</h3><p>Water &amp; sewer, gate access, utilities, road hazards</p><span class="v-cta">See it &rarr;</span></a>
<a href="/gym" class="v-card"><h3>24/7 Gym</h3><p>Broken equipment, access fobs, safety issues</p><span class="v-cta">See it &rarr;</span></a>
<a href="/carwash" class="v-card"><h3>Car Wash</h3><p>Bay jams, tunnel stops, payment failures, stuck gates</p><span class="v-cta">See it &rarr;</span></a>
<a href="/rvpark" class="v-card"><h3>RV Parks</h3><p>Hookup &amp; power failures, sewer, gate codes, bathhouse</p><span class="v-cta">See it &rarr;</span></a>
</div>

</div>

<div class="cta-block">
<a href="/signup">Start your free trial &rarr;</a>
<span class="fine">14-day free trial. No credit card. Cancel by text.</span>
</div>

<footer>Hotline &middot; Real-time alerts for offsite operators &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a></footer>
</body></html>"""


@app.get("/industries")
def industries_page(): _ensure_init(); return Response(content=_ga(INDUSTRIES_HTML), media_type="text/html")


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

_ARTICLE_FOOT = """<footer>Hotline &middot; Real-time SMS alerts for offsite operators &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa;display:inline-flex;align-items:center;gap:4px;vertical-align:middle"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/></svg>Instagram</a></footer>"""

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
<p>How Hotline works, what customers see, what operators see, pricing, privacy, and more. Start here.</p>
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
<p>Physical signs are just the start. The best placements are often digital, and most operators skip them entirely.</p>
<span class="arrow">Read &rarr;</span>
</a>
<a href="/resources/responding-to-alerts" class="card">
<div class="card-meta"><span>03 &mdash; Operations</span><span>3 min read</span></div>
<h2>How to respond to alerts without burning out</h2>
<p>Getting the alert is step one. Here's how to handle it fast without creating new problems for yourself.</p>
<span class="arrow">Read &rarr;</span>
</a>
<a href="/resources/why-staff-fail-you" class="card">
<div class="card-meta"><span>04 &mdash; Operations</span><span>4 min read</span></div>
<h2>Why your staff may be your biggest operational blind spot</h2>
<p>It's not about bad employees. It's about a broken system — and why building your visibility around staff escalation is a costly mistake.</p>
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
<p>How Hotline works, what customers and operators see, pricing, and privacy. If something isn't covered here, email us at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>
</header>

<div class="section">
<div class="section-label">Getting started</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">What is Hotline? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Hotline is an SMS-based alert system for small business operators. Customers text a number or scan a QR code to report issues. Every message is read, classified by urgency, and you get a text alert when something actually needs your attention. You manage everything by text. No app, no dashboard.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">How does setup work? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Sign up at hotlinetxt.com/signup. You'll get a print-ready PDF and a plain QR image texted to you within minutes. Display your Hotline, and the service starts working. The whole process takes under five minutes.</p></div>
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
<div class="faq-a"><p>Operators who use Hotline are usually surprised by how quiet it is. Most days you'll get nothing. When something comes in, it's almost always real and actionable. There is also built-in rate limiting so a single frustrated customer can't flood you with alerts.</p></div>
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
<li><strong>REPLY</strong> - Open a direct line to the last customer</li>
<li><strong>CLOSE</strong> - End the conversation, AI auto-replies resume</li>
<li><strong>NEVERMIND</strong> - Cancel reply mode without sending</li>
<li><strong>STATUS</strong> - See your current alert settings</li>
<li><strong>ALERTS</strong> - View or change your alert level</li>
<li><strong>TIER2</strong> - Critical issues only (emergencies + operations)</li>
<li><strong>TIER3</strong> - Also get reputation and feedback alerts</li>
<li><strong>PAUSE / RESUME</strong> - Stop or restart all non-emergency alerts</li>
<li><strong>MENU</strong> - Full command list</li>
<li><strong>BILLING</strong> - Check subscription status</li>
</ul>
</div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I add a second phone number for a manager or partner? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. You can add a second alert number during signup or ask us to add one after. Both numbers get the same alerts and can use the same commands.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I pause alerts when I'm off the clock? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. Text PAUSE to stop all non-emergency alerts until you're ready. Text RESUME to turn them back on. Tier 1 emergencies always come through regardless.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can I reply directly to a customer? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Yes. Text REPLY after receiving an alert. The system enters reply mode — type your message and it goes to the customer from the Hotline number. They never see your personal cell. Text CLOSE when you're done or let it time out after 15 minutes.</p></div>
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
<div class="faq-a"><p>If a customer texts the Hotline number directly without a QR code scan, the message includes a business code that routes it correctly. If there's no code and no recent session, they'll get a prompt to scan the QR. Messages without a valid business code aren't forwarded to any operator.</p></div>
</div>
</div>

<div class="section">
<div class="section-label">Privacy</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Will the business know it was me who texted? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Every message goes through the Hotline number, not your personal phone. The business operator sees the message content and when it came in. They do not see your phone number, your name, or any identifying information. As far as the operator knows, an anonymous customer sent a message through Hotline.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Can the business see my personal phone number? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. Your message travels through the Hotline system number, not directly from your phone to theirs. The operator's alert shows the message text and timestamp only. Your personal number is never displayed to the business, not in the alert, not in any reply thread, not anywhere in their interface.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Does the business operator have my contact info after I text? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>No. The operator has no way to contact you outside of Hotline unless you choose to share your information in the message itself. If the operator replies using the REPLY command, that message comes back to you through the Hotline number, keeping both sides anonymous throughout the conversation.</p></div>
</div>

<div class="faq-item">
<button class="faq-q" onclick="toggle(this)">Who else can see my message besides the operator? <span class="faq-icon">+</span></button>
<div class="faq-a"><p>Any alert recipients the operator has added, such as a manager or business partner, will receive the same alert text. None of them see your phone number. Messages are stored in Hotline's system for logging. They are not shared with third parties or used for marketing.</p></div>
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
<div class="faq-a"><p>No. Your business name, phone number, and incoming message data are used only to operate the service. Operator data is never sold or shared with advertisers or third parties.</p></div>
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

<p>Every business has this gap. Problems happen at the floor level. Operators operate above it. The information that travels between the two gets filtered by time, by staff who don't want to deliver bad news, and by systems that only catch things after the fact.</p>

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

<p>Most operators display their Hotline near the entrance and call it done. That's the least effective spot. By the time someone's at the exit, the problem is behind them. They're already deciding whether to leave a review.</p>

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

<h2>Digital placements (most operators skip these)</h2>

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

<div class="sample">"Something wrong? Text us. Operator reads every message."</div>
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

<p>That distinction matters. Most operators brace for a flood of notifications and then realize the opposite is true. Every incoming message is read, each customer gets an automatic response with the right tone, and everything is filtered before it reaches you. Compliments, questions, minor complaints, spam, all of it gets processed without you lifting a finger.</p>

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

<p>If the same issue keeps coming through, the pattern is telling you something. Three alerts about the same machine, the same bathroom, the same shift gap? That's not noise. That's a pattern worth fixing.</p>

<h2>Protect your own time</h2>

<p>Text PAUSE to stop non-emergency alerts until you're ready. Text RESUME to turn them back on. Tier 1 emergencies always get through regardless of your settings.</p>

<p>The whole system is built to stay out of your way. The AI runs constantly in the background so you don't have to. When it needs you, it will find you.</p>

""" + _ARTICLE_CTA + """
<a href="/resources" class="back-link">&larr; Back to resources</a>
</article>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""


@app.get("/resources")
def resources_page(): _ensure_init(); return Response(content=_ga(RESOURCES_HTML), media_type="text/html")

@app.get("/resources/faq")
def resources_faq(): _ensure_init(); return Response(content=_ga(RESOURCES_FAQ_HTML), media_type="text/html")

@app.get("/resources/why-you-need-a-hotline")
def resources_article_1(): _ensure_init(); return Response(content=_ga(RESOURCES_ARTICLE_1_HTML), media_type="text/html")

@app.get("/resources/where-to-put-your-qr")
def resources_article_2(): _ensure_init(); return Response(content=_ga(RESOURCES_ARTICLE_2_HTML), media_type="text/html")

@app.get("/resources/responding-to-alerts")
def resources_article_3(): _ensure_init(); return Response(content=_ga(RESOURCES_ARTICLE_3_HTML), media_type="text/html")

RESOURCES_ARTICLE_4_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Why your staff may be your biggest operational blind spot &mdash; Hotline</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>""" + _ARTICLE_CSS + """</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<div class="breadcrumb"><a href="/resources">&larr; Resources</a> &nbsp;/ 04 &mdash; Operations</div>
<header class="ah">
<h1>Why your staff may be your biggest operational blind spot</h1>
<div class="ameta"><span>Operations</span><span>4 min read</span></div>
</header>
<article>

<p class="lead">Your staff isn't trying to fail you. But they might be anyway &mdash; and the system you've built around their ability to escalate is likely more fragile than you think.</p>

<p>When something goes wrong at your location, the assumption is usually the same: someone on staff will notice, someone will escalate, and someone will fix it. That assumption is costing operators real money. Most frontline employees aren't equipped, incentivized, or expected to surface operational problems. Building your visibility strategy around their judgment is one of the most common and costly mistakes physical business operators make.</p>

<p>Here's why.</p>

<h2>1. They were never trained to escalate</h2>

<p>Most frontline employees receive training on how to do their job &mdash; how to operate the register, how to greet customers, how to close up. What they rarely receive is clear guidance on what to do when something breaks: who to call, what to say, how urgent it is, and what happens if they don't.</p>

<p>Without a defined escalation process, most employees default to the path of least resistance: assume someone else will handle it, or wait to see if the problem resolves itself. By the time anyone realizes it won't, hours have passed.</p>

<h2>2. These are entry-level positions</h2>

<p>The people working your front line at a car wash, parking garage, laundromat, or gas station are often in their first or second job. They're new, they're still learning what's expected of them, and they're not thinking about your revenue or your Google rating.</p>

<p>Asking an 18-year-old making $14 an hour to recognize a broken payment terminal, understand its operational impact, find the right person to call, and confidently escalate it is a significant ask. Not because they're incapable &mdash; but because nothing in their experience has prepared them for that level of ownership.</p>

<h2>3. Repairs aren't their responsibility</h2>

<p>From a frontline employee's perspective, broken equipment is someone else's problem. They didn't buy it. They don't maintain it. They can't fix it. When something breaks, the instinct is to mentally hand it off &mdash; "that's a manager thing" or "maintenance handles that" &mdash; and move on.</p>

<p>The problem is that the handoff never actually happens. It just gets assumed. And assumptions are where operational failures live.</p>

<div class="callout">
<div class="callout-label">The real cost</div>
<p>A customer notices the broken machine at 2 PM. Staff assumes someone else reported it. You find out at 9 PM when a review goes live. That gap &mdash; not the broken machine &mdash; is what actually hurt your business.</p>
</div>

<h2>4. They're not driven by revenue</h2>

<p>Your staff doesn't feel the P&L impact of downtime. They don't see the revenue lost when a machine is offline for three hours on a Friday night. They don't read the weekly review report or watch the star rating tick down.</p>

<p>You do. The gap between what your staff cares about and what you care about is completely natural &mdash; but it creates a dangerous blind spot. What feels like an emergency to you barely registers for someone who just wants to get through their shift and go home.</p>

<h2>5. They have no stake in the outcome</h2>

<p>A bad review doesn't affect your employee's paycheck. A lost customer doesn't change their schedule. A reputation hit doesn't impact their career. When there's no personal stake in the outcome, the urgency to act simply isn't there &mdash; even for good, well-meaning people.</p>

<p>This isn't a character flaw. It's human nature. People respond to incentives. And most frontline staff have no incentive to treat a broken machine as a five-alarm fire.</p>

<h2>6. They're checked out</h2>

<p>High-turnover industries &mdash; car washes, laundromats, parking facilities, gas stations &mdash; have a well-documented engagement problem. Many employees are working a job, not building a career. A checked-out employee isn't going out of their way to report a broken kiosk. They're going to assume it's not their problem, assume someone else saw it, and move on.</p>

<p>The "I just work here" mentality isn't cynical. It's a symptom of an environment where nobody has ever made operational awareness feel like part of the job.</p>

<h2>7. They don't want the confrontation</h2>

<p>Telling a manager something is broken can feel like delivering bad news. Some employees worry about being blamed. Others don't want to seem like they're creating problems. In environments where escalation isn't explicitly encouraged, silence becomes the default.</p>

<h2>The real problem: you built your visibility on a fragile foundation</h2>

<p>None of this means your staff are bad employees. It means that relying on staff escalation as your primary method of operational visibility is a structural problem, not a personnel problem.</p>

<p>The businesses that catch issues fastest have stopped waiting for staff to notice. Instead, they've built a direct line between their customers and their operations. When a customer sees something wrong, they can text in immediately. Hotline's AI responds to every message instantly &mdash; 24/7 &mdash; filters out the noise, and passes only real concerns directly to you.</p>

<ul>
<li>No reliance on a 19-year-old remembering to call the manager</li>
<li>No assuming someone else already reported it</li>
<li>No finding out three hours later from a Google review</li>
</ul>

<p>Your staff will continue to be your first line of service. But they were never meant to be your only line of operational visibility. Give your customers a direct line, let AI handle the triage, and stop building your business resilience on a foundation that was never designed to hold it.</p>

""" + _ARTICLE_CTA + """
<a href="/resources" class="back-link">&larr; Back to resources</a>
</article>
</div>
""" + _ARTICLE_FOOT + """
</body></html>"""

@app.get("/resources/why-staff-fail-you")
def resources_article_4(): _ensure_init(); return Response(content=_ga(RESOURCES_ARTICLE_4_HTML), media_type="text/html")


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
input[type=text],input[type=tel],input[type=email],input[type=url],select{width:100%;padding:12px 14px;background:#fafaf8;border:1px solid #e0e0dc;border-radius:8px;font-size:16px;color:#1a1a1a;font-family:inherit}input::placeholder{color:#bbb}input:focus,select:focus{outline:none;border-color:#ea580c}select{appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23999' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center}
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
<h1>Start getting alerts today</h1>
<p class="sub">No app. No software. No training required. Sign up in 30 seconds and get your print-ready Hotline instantly.</p>
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
<label>Type of operation</label><select id="f-vertical"><option value="">Select your operation...</option><option value="laundromat">Laundromat</option><option value="selfstorage">Self Storage</option><option value="mhc">Mobile Home Park</option><option value="gym">24/7 Gym</option><option value="carwash">Car Wash</option><option value="rvpark">RV Park</option><option value="other">Other</option></select>


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

<button class="btn" id="f-btn" onclick="signup()">Start my free trial &rarr;</button>
</div>
<div class="steps">
<div class="step"><div class="step-num">1</div><h3>Sign up</h3><p>Get your QR code and sign in seconds</p></div>
<div class="step"><div class="step-num">2</div><h3>Display your Hotline</h3><p>Place it anywhere customers look.</p></div>
<div class="step"><div class="step-num">3</div><h3>Get alerted</h3><p>Know the moment something needs your attention</p></div>
</div>
</div>
<footer>Hotline &middot; Real-time SMS alerts for offsite operators &middot; <a href="/privacy" style="color:#aaa">Privacy</a> &middot; <a href="/terms" style="color:#aaa">Terms</a> &middot; <a href="mailto:Connect@HotlineTXT.com" style="color:#aaa">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="color:#aaa;display:inline-flex;align-items:center;gap:4px;vertical-align:middle"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/></svg>Instagram</a></footer>
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
  if(!zip||!/^\\d{5}$/.test(zip)){res.className='result err';res.style.display='block';res.textContent='Please enter a valid 5-digit zip code.';return}
  btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Setting up...';res.style.display='none';
  try{const r=await fetch('/signup/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,phone2,email,website_url:url,zip,vertical:document.getElementById('f-vertical').value})});const d=await r.json();
  if(d.success){
    res.className='result ok';res.innerHTML='<strong>You are live!</strong><br><br>Check your texts for your sign PDF and QR code image.<br><br>Code: <strong>'+d.business_code+'</strong><br><a href="'+d.sign_url+'" target="_blank" style="color:#ea580c">Download your sign &rarr;</a>';
    res.style.display='block';btn.textContent='Done!'}
  else{res.className='result err';res.textContent=d.error||'Something went wrong.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my QR code &rarr;'}}
  catch(e){res.className='result err';res.textContent='Connection error.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my QR code &rarr;'}
}
</script></body></html>"""

@app.get("/signup")
def signup_page(): _ensure_init(); return Response(content=_ga(SIGNUP_HTML), media_type="text/html")

@app.get("/laundromat")
def vertical_laundromat(): _ensure_init(); return Response(content=_ga(VERTICAL_LAUNDROMAT_HTML), media_type="text/html")

@app.get("/carwash")
def vertical_carwash(): _ensure_init(); return Response(content=_ga(VERTICAL_CARWASH_HTML), media_type="text/html")

@app.get("/selfstorage")
def vertical_selfstorage(): _ensure_init(); return Response(content=_ga(VERTICAL_SELFSTORAGE_HTML), media_type="text/html")

@app.get("/mhc")
def vertical_mhc(): _ensure_init(); return Response(content=_ga(VERTICAL_MHC_HTML), media_type="text/html")

@app.get("/rvpark")
def vertical_rvpark(): _ensure_init(); return Response(content=_ga(VERTICAL_RVPARK_HTML), media_type="text/html")

@app.get("/parking")
def vertical_parking_redirect(): return RedirectResponse(url="/industries", status_code=301)

@app.get("/gym")
def vertical_gym(): _ensure_init(); return Response(content=_ga(VERTICAL_GYM_HTML), media_type="text/html")


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

<div class="highlight"><p>&#128241; Hotline is an SMS-based customer feedback system. Customers text a business number and business operators receive alerts. This policy explains how we handle that data.</p></div>

<h2>1. Who We Are</h2>
<p>Hotline is operated by HotlineTXT.com (&ldquo;we,&rdquo; &ldquo;our,&rdquo; or &ldquo;us&rdquo;). We provide SMS-based customer alerting services to small businesses. For questions, contact us at <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>

<h2>2. Information We Collect</h2>
<p>We collect the following information when you use Hotline:</p>
<ul>
<li><strong>Customer SMS messages:</strong> The text content of messages sent to a Hotline business number, along with the sender&rsquo;s phone number and timestamp.</li>
<li><strong>Business operator information:</strong> Business name, operator phone number, optional email address, and optional website URL provided during signup.</li>
<li><strong>Usage data:</strong> Message tiers, categories, sentiment classifications, and acknowledgment records generated by our AI system.</li>
</ul>

<h2>3. How We Use Your Information</h2>
<p>We use collected information solely to operate the Hotline service:</p>
<ul>
<li>Classify and route customer messages to business operators via SMS</li>
<li>Send alert notifications to registered business operator phone numbers</li>
<li>Generate weekly digest summaries for business operators (if opted in)</li>
<li>Maintain message logs accessible to the business operator via SMS commands</li>
</ul>
<p>We do <strong>not</strong> sell, rent, or share your personal information with third parties for marketing purposes.</p>

<h2>4. SMS Messaging and Opt-In</h2>
<p><strong>Business operators:</strong> By signing up for Hotline, you consent to receive SMS alerts and notifications from your assigned Hotline number. You may opt out at any time by texting <strong>STOP</strong> to your Hotline number. Standard message and data rates from your carrier may apply.</p>
<p><strong>Customers texting a business:</strong> When you text a Hotline-powered business number, your message and phone number are stored and forwarded to the business operator. You are not opted in to any marketing list. The business may reply to your message directly via SMS.</p>

<h2>5. Data Retention</h2>
<p>Customer messages and associated data are stored for up to 90 days by default. Business operator accounts and associated message history are retained for the duration of the account. You may request deletion by contacting <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>

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
<p>We may update this Privacy Policy from time to time. We will notify registered business operators of material changes via SMS or email. Continued use of Hotline after changes constitutes acceptance of the updated policy.</p>

<h2>10. Contact</h2>
<p>For privacy questions, data deletion requests, or to opt out of SMS communications, contact:</p>
<p style="margin-top:8px"><strong>Email:</strong> <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a><br>
<strong>Website:</strong> <a href="https://HotlineTXT.com">HotlineTXT.com</a></p>
</div>
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:4px;vertical-align:middle"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/></svg>Instagram</a></footer>
</body></html>"""

@app.get("/privacy")
def privacy_page(): _ensure_init(); return Response(content=_ga(PRIVACY_HTML), media_type="text/html")


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
<p>Hotline is an SMS-based system that allows customers to send text messages to a business phone number. The Service uses AI to classify incoming messages and notifies registered business operators of important issues via SMS. Business operators interact with the Service entirely via SMS commands.</p>

<h2>3. SMS Messaging &mdash; Opt-In and Opt-Out</h2>
<p><strong>Business operators:</strong> By completing signup and providing your phone number, you expressly consent to receive SMS messages from Hotline, including:</p>
<ul>
<li>Alert notifications when customers send flagged messages</li>
<li>Weekly digest summaries (if enabled)</li>
<li>Onboarding and setup messages</li>
</ul>
<p>Message frequency varies based on customer activity. Standard message and data rates may apply.</p>
<p>To opt out of SMS alerts at any time, text <strong>STOP</strong> to your assigned Hotline number. You will receive one confirmation message and no further messages will be sent. Text <strong>HELP</strong> for assistance or contact <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a>.</p>
<p><strong>Customers:</strong> Customers who text a Hotline business number are not opting in to any marketing messages. Their messages are forwarded to the relevant business operator only.</p>

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
<footer>Hotline &middot; <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="mailto:Connect@HotlineTXT.com">Connect@HotlineTXT.com</a> &middot; <a href="https://www.instagram.com/hotlinetxt/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:4px;vertical-align:middle"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/></svg>Instagram</a></footer>
</body></html>"""

@app.get("/terms")
def terms_page(): _ensure_init(); return Response(content=_ga(TERMS_HTML), media_type="text/html")

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
            # Notify operator if they were previously blocked
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
                send_sms(p, f"&#9888; Hotline payment failed. Alerts may stop soon.{link_part}")

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
    vertical = (request_data.get("vertical") or "").strip().lower()
    if not name: return {"error":"Business name required"}
    if not phone or not phone.startswith("+"): return {"error":"Valid phone with country code required"}

    base = os.getenv("BASE_URL", "https://hotlinetxt.com")

    # Build business ID — retry with increasingly unique suffixes if collision
    extra = phone2 if phone2 and phone2.startswith("+") else ""
    business_code = None
    base_biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    for attempt in range(3):
        biz_id = base_biz_id
        with get_db() as c:
            if _fetchone(c,_q("SELECT id FROM businesses WHERE id=?"), (biz_id,)):
                biz_id = base_biz_id[:20]+"-"+datetime.now(timezone.utc).strftime("%H%M%S")+"-"+"".join(__import__("random").choices("0123456789",k=4))
        business_code = create_business(biz_id, name, phone, SHARED_NUMBER, extra_phones=extra, email=email, website_url=website_url, zip_code=zip_code, vertical=vertical)
        if business_code:
            break
        logger.warning(f"create_business attempt {attempt+1} failed for {name} ({phone}) biz_id={biz_id}")

    if not business_code:
        logger.error(f"Signup FAILED after 3 attempts for {name} ({phone})")
        ts = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")
        email_html = f"""<div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px">
          <h2 style="color:#dc2626;margin:0 0 16px">&#9888; Signup Failed</h2>
          <p style="font-size:14px;color:#333;margin-bottom:16px">Business creation failed after 3 attempts. Manual follow-up needed.</p>
          <table style="width:100%;border-collapse:collapse;font-size:14px">
            <tr><td style="padding:8px 0;color:#888;width:120px">Name</td><td style="padding:8px 0;font-weight:600">{name}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Phone</td><td style="padding:8px 0;font-family:monospace">{phone}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Partner</td><td style="padding:8px 0;font-family:monospace">{phone2 or "—"}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Email</td><td style="padding:8px 0">{email or "—"}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Website</td><td style="padding:8px 0">{website_url or "—"}</td></tr>
            <tr><td style="padding:8px 0;color:#888">Time</td><td style="padding:8px 0">{ts}</td></tr>
          </table>
          <p style="margin:24px 0 0;font-size:13px;color:#aaa">Add manually at <a href="https://hotlinetxt.com/admin" style="color:#ea580c">hotlinetxt.com/admin</a></p>
        </div>"""
        send_email("Connect@HotlineTXT.com", f"SIGNUP FAILED: {name} ({phone})", email_html)
        return {"success":False,"error":"Setup failed \u2014 please try again in a moment, or contact Connect@HotlineTXT.com for help."}

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
        f"Display your Hotline (PDF): {base}/signs/{business_code}.pdf\n"
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
