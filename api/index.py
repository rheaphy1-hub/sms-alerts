"""
SMS Alert System v2 — Single-file Vercel deployment.
Everything in one file so there's nothing to get wrong with imports or folders.
"""

import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

from fastapi import FastAPI, Form, Response, Query

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sms")

# ---------------------------------------------------------------------------
# Database — Postgres (production) or SQLite (local dev)
# ---------------------------------------------------------------------------
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3


def _pg_connect():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def _sqlite_connect():
    conn = sqlite3.connect(os.getenv("DB_PATH", "alerts.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def get_db():
    conn = _pg_connect() if USE_POSTGRES else _sqlite_connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fetchone(conn, query, params=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _fetchall(conn, query, params=()):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
    cur.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def _execute(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    return cur


def _q(query):
    return query.replace("?", "%s") if USE_POSTGRES else query


def _normalize_phone(phone):
    return phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")


def init_db():
    serial = "SERIAL" if USE_POSTGRES else "INTEGER"
    pk = "PRIMARY KEY" if USE_POSTGRES else "PRIMARY KEY AUTOINCREMENT"
    with get_db() as conn:
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS businesses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            owner_phone TEXT NOT NULL, alert_phones TEXT NOT NULL DEFAULT '',
            twilio_number TEXT NOT NULL UNIQUE,
            muted_until TEXT, paused INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS messages (
            id {serial} {pk}, business_id TEXT NOT NULL, from_number TEXT NOT NULL,
            message_text TEXT NOT NULL, tier INTEGER, category TEXT, sentiment TEXT,
            confidence REAL, summary TEXT, acknowledged INTEGER DEFAULT 0,
            alerted INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS alert_log (
            id {serial} {pk}, message_id INTEGER NOT NULL, business_id TEXT NOT NULL,
            alert_type TEXT NOT NULL, sent_at TEXT NOT NULL)""")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_phone)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_msg_biz ON messages(business_id, tier, acknowledged)")
        # Migration: add alert_phones column if missing
        try:
            _execute(conn, "ALTER TABLE businesses ADD COLUMN alert_phones TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass


def create_business(biz_id, name, owner_phone, twilio_number, extra_phones=""):
    now = datetime.now(timezone.utc).isoformat()
    all_phones = owner_phone
    if extra_phones:
        all_phones = ",".join([owner_phone] + [p.strip() for p in extra_phones.split(",") if p.strip()])
    try:
        with get_db() as conn:
            _execute(conn, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,twilio_number,created_at) VALUES (?,?,?,?,?,?)"),
                     (biz_id, name, owner_phone, all_phones, twilio_number, now))
        return True
    except Exception:
        return False


def get_alert_phones(biz):
    """Get list of all phone numbers that should receive alerts."""
    phones_str = biz.get("alert_phones") or biz.get("owner_phone") or ""
    phones = [p.strip() for p in phones_str.split(",") if p.strip()]
    # Always include owner_phone
    owner = biz.get("owner_phone", "")
    if owner and owner not in phones:
        phones.insert(0, owner)
    return phones


def get_business_by_twilio(twilio_number):
    clean = _normalize_phone(twilio_number)
    with get_db() as conn:
        row = _fetchone(conn, _q("SELECT * FROM businesses WHERE twilio_number = ?"), (clean,))
        if row:
            return row
        for r in _fetchall(conn, "SELECT * FROM businesses"):
            if _normalize_phone(r["twilio_number"])[-10:] == clean[-10:]:
                return r
    return None


def get_business_by_owner(owner_phone):
    """Check if this phone number belongs to any business (owner or alert contact)."""
    clean = _normalize_phone(owner_phone)
    with get_db() as conn:
        # Check owner_phone directly
        row = _fetchone(conn, _q("SELECT * FROM businesses WHERE owner_phone = ?"), (clean,))
        if row:
            return row
        # Check all businesses for this number in alert_phones or owner_phone
        for r in _fetchall(conn, "SELECT * FROM businesses"):
            if _normalize_phone(r["owner_phone"])[-10:] == clean[-10:]:
                return r
            for p in (r.get("alert_phones") or "").split(","):
                if p.strip() and _normalize_phone(p.strip())[-10:] == clean[-10:]:
                    return r
    return None


def store_message(business_id, from_number, message_text, classification):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        q = _q("INSERT INTO messages (business_id,from_number,message_text,tier,category,sentiment,confidence,summary,created_at) VALUES (?,?,?,?,?,?,?,?,?)")
        params = (business_id, from_number, message_text, classification.get("tier"),
                  classification.get("category"), classification.get("sentiment"),
                  classification.get("confidence"), classification.get("summary", ""), now)
        if USE_POSTGRES:
            cur = _execute(conn, q + " RETURNING id", params)
            return cur.fetchone()[0]
        else:
            cur = _execute(conn, q, params)
            return cur.lastrowid


def log_alert(message_id, business_id, alert_type):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        _execute(conn, _q("INSERT INTO alert_log (message_id,business_id,alert_type,sent_at) VALUES (?,?,?,?)"),
                 (message_id, business_id, alert_type, now))


def mark_acknowledged(message_id):
    with get_db() as conn:
        _execute(conn, _q("UPDATE messages SET acknowledged=1 WHERE id=?"), (message_id,))


def mark_alerted(message_id):
    with get_db() as conn:
        _execute(conn, _q("UPDATE messages SET alerted=1 WHERE id=?"), (message_id,))


def get_latest_unacked(biz_id):
    with get_db() as conn:
        return _fetchone(conn, _q("SELECT * FROM messages WHERE business_id=? AND tier IN (1,2) AND acknowledged=0 ORDER BY created_at DESC LIMIT 1"), (biz_id,))


def get_message_by_id(msg_id):
    with get_db() as conn:
        return _fetchone(conn, _q("SELECT * FROM messages WHERE id=?"), (msg_id,))


def get_recent_flagged(biz_id, limit=5):
    with get_db() as conn:
        return _fetchall(conn, _q("SELECT * FROM messages WHERE business_id=? AND tier IN (1,2) ORDER BY created_at DESC LIMIT ?"), (biz_id, limit))


def get_recent_all(biz_id, limit=5):
    with get_db() as conn:
        return _fetchall(conn, _q("SELECT * FROM messages WHERE business_id=? ORDER BY created_at DESC LIMIT ?"), (biz_id, limit))


def get_recent_alert_count(biz_id, minutes=10):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with get_db() as conn:
        row = _fetchone(conn, _q("SELECT COUNT(*) as cnt FROM alert_log WHERE business_id=? AND sent_at>?"), (biz_id, cutoff))
        return row["cnt"] if row else 0


def is_alerts_silenced(biz):
    if biz.get("paused"):
        return True
    mu = biz.get("muted_until")
    if mu:
        try:
            if datetime.fromisoformat(mu) > datetime.now(timezone.utc):
                return True
        except Exception:
            pass
    return False


def set_muted_until(biz_id, until):
    with get_db() as conn:
        _execute(conn, _q("UPDATE businesses SET muted_until=? WHERE id=?"),
                 (until.isoformat() if until else None, biz_id))


def set_paused(biz_id, paused):
    with get_db() as conn:
        _execute(conn, _q("UPDATE businesses SET paused=? WHERE id=?"), (1 if paused else 0, biz_id))


def get_all_businesses():
    with get_db() as conn:
        return _fetchall(conn, "SELECT * FROM businesses")


def get_weekly_stats(biz_id):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with get_db() as conn:
        total = _fetchone(conn, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND created_at>?"), (biz_id, cutoff))["cnt"]
        flagged = _fetchone(conn, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND created_at>?"), (biz_id, cutoff))["cnt"]
        acked = _fetchone(conn, _q("SELECT COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND acknowledged=1 AND created_at>?"), (biz_id, cutoff))["cnt"]
        top = _fetchone(conn, _q("SELECT category,COUNT(*) as cnt FROM messages WHERE business_id=? AND tier IN (1,2) AND created_at>? GROUP BY category ORDER BY cnt DESC LIMIT 1"), (biz_id, cutoff))
        return {"total_messages": total, "flagged_issues": flagged, "acknowledged": acked, "top_category": top["category"] if top else "none"}


# ---------------------------------------------------------------------------
# SMS — Twilio
# ---------------------------------------------------------------------------
_twilio_client = None
_twilio_from = ""


def init_sms():
    global _twilio_client, _twilio_from
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    _twilio_from = os.getenv("TWILIO_PHONE_NUMBER", "")
    if sid and token:
        from twilio.rest import Client
        _twilio_client = Client(sid, token)
        logger.info("Twilio ready")
    else:
        logger.warning("Twilio not configured — dry-run mode")


def send_sms(to, body, from_number=""):
    sender = from_number or _twilio_from
    if not _twilio_client:
        logger.info(f"[DRY-RUN] {sender} → {to}: {body}")
        return True
    try:
        _twilio_client.messages.create(body=body, from_=sender, to=to)
        return True
    except Exception as e:
        logger.error(f"SMS failed to {to}: {e}")
        return False


def buy_twilio_number(area_code="", webhook_url=""):
    """Buy a local Twilio number and set its webhook. Returns the number or None."""
    if not _twilio_client:
        logger.warning("[DRY-RUN] Would buy a Twilio number")
        return "+15550000000"
    try:
        # Search for available local numbers
        kwargs = {"limit": 1, "sms_enabled": True, "voice_enabled": False}
        if area_code:
            kwargs["area_code"] = area_code
        available = _twilio_client.available_phone_numbers("US").local.list(**kwargs)
        if not available:
            # Try without area code constraint
            del kwargs["area_code"]
            available = _twilio_client.available_phone_numbers("US").local.list(**kwargs)
        if not available:
            logger.error("No Twilio numbers available")
            return None

        # Buy it
        number = _twilio_client.incoming_phone_numbers.create(
            phone_number=available[0].phone_number,
            sms_url=webhook_url or f"https://sms-alerts.vercel.app/sms/incoming",
            sms_method="POST",
        )
        logger.info(f"Bought Twilio number: {number.phone_number}")
        return number.phone_number
    except Exception as e:
        logger.error(f"Failed to buy Twilio number: {e}")
        return None


# ---------------------------------------------------------------------------
# AI Classifier
# ---------------------------------------------------------------------------
_ai_client = None

CLASSIFICATION_PROMPT = """You are a business issue classifier. Analyze customer SMS messages and return structured JSON.

Tiers:
- Tier 1: Emergency — violence, injury, fire, medical emergency, active danger
- Tier 2: Business-Critical — operations broken, no staff, equipment failure, health hazard, extreme waits
- Tier 3: Reputation Risk — unhappy customer, complaint, bad experience
- Tier 4: Routine — general inquiry, positive feedback, neutral

Extract: category (cleanliness/staffing/equipment/wait_time/safety/other), sentiment (negative/neutral/positive), confidence (0.0-1.0), summary (5-10 words), auto_reply (see tone rules below).

AUTO-REPLY TONE RULES (critical):
- Tier 1 (Emergency): Urgent, direct. ALWAYS tell the customer to call 911 if it's a real emergency. Never claim that emergency services have been contacted. Example: "If this is an emergency, please call 911 immediately. We have notified the business owner."
- Tier 2 (Business-Critical): Professional, serious. Confirm the specific issue type. Say management has been notified. No exclamation marks. Example: "We've flagged this as an equipment issue and notified management. Thank you for letting us know."
- Tier 3 (Reputation Risk): Empathetic, professional. Acknowledge their frustration without being defensive. No exclamation marks. Example: "We're sorry to hear about your experience. Your feedback has been shared with management."
- Tier 4 (Routine/Positive): Warm, friendly, use exclamation marks. Show genuine appreciation. Example: "Thanks so much for the kind words! We'll make sure the team hears this."

Keep auto_reply under 160 characters. Never ask follow-up questions.

Respond ONLY with JSON: {"tier":<int>,"category":"<str>","sentiment":"<str>","confidence":<float>,"summary":"<str>","auto_reply":"<str>"}"""


def init_classifier():
    global _ai_client
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        from anthropic import Anthropic
        _ai_client = Anthropic(api_key=key)
    else:
        logger.warning("No ANTHROPIC_API_KEY — using fallback classifier")


def classify_message(text):
    if _ai_client:
        try:
            resp = _ai_client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=300,
                system=CLASSIFICATION_PROMPT,
                messages=[{"role": "user", "content": f'Classify this customer SMS:\n\n"{text}"'}])
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            r = json.loads(raw)
            r["tier"] = max(1, min(4, int(r.get("tier", 4))))
            r["confidence"] = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
            r.setdefault("category", "other")
            r.setdefault("sentiment", "neutral")
            r.setdefault("summary", text[:50])
            r.setdefault("auto_reply", "Thanks for reaching out. We've received your message.")
            return r
        except Exception as e:
            logger.error(f"AI classification failed: {e}")

    return _classify_fallback(text)


def _classify_fallback(text):
    t = text.lower()
    emergency = ["fire","help","emergency","injury","hurt","bleeding","attack","weapon","gun",
                 "violence","ambulance","911","collapsed","unconscious","not breathing",
                 "heart attack","seizure","overdose","stabbed","shot","flood","gas leak"]
    if any(w in t for w in emergency):
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.8,
                "summary":"Possible emergency reported",
                "auto_reply":"If this is an emergency, please call 911 immediately. We have notified the business owner."}

    crit = {"cleanliness": (["dirty","disgusting","filthy","mess","bathroom","gross","unsanitary"],
                            "We've flagged this as a cleanliness issue and notified management. Thank you for letting us know."),
            "staffing": (["no one","nobody","empty","no staff","where is everyone","closed"],
                         "We've flagged this as a staffing issue and notified management. We apologize for the inconvenience."),
            "equipment": (["broken","machine","not working","out of order","malfunction"],
                          "We've flagged this as an equipment issue and notified management. Thank you for letting us know."),
            "wait_time": (["waited","waiting","slow","forever","leaving","20 minutes","30 minutes"],
                          "We're sorry about the wait. Management has been notified about the delay.")}
    for cat, (words, reply) in crit.items():
        if any(w in t for w in words):
            return {"tier":2,"category":cat,"sentiment":"negative","confidence":0.85,
                    "summary":f"{cat.replace('_',' ').title()} issue reported","auto_reply":reply}

    neg = ["bad","terrible","awful","rude","worst","hate","angry","disappointed","unhappy","never coming back"]
    if any(w in t for w in neg):
        return {"tier":3,"category":"other","sentiment":"negative","confidence":0.6,
                "summary":"Unhappy customer feedback",
                "auto_reply":"We're sorry to hear about your experience. Your feedback has been shared with management."}

    return {"tier":4,"category":"other","sentiment":"neutral","confidence":0.5,
            "summary":"General message received",
            "auto_reply":"Thanks so much for reaching out! We've noted your message."}


# ---------------------------------------------------------------------------
# Owner commands
# ---------------------------------------------------------------------------
_owner_context = {}


def set_context(biz_id, msg_id):
    _owner_context[biz_id] = msg_id


def _fmt_ts(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %I:%M%p").replace(" 0", " ")
    except Exception:
        return iso[:16]


def handle_owner_command(text, business):
    biz_id = business["id"]
    raw = text.strip()
    cmd = raw.upper()

    # --- Acknowledgment: 👍, OK, GOT IT, DONE, ON IT, ACK ---
    ack_words = {"OK", "GOT IT", "DONE", "ON IT", "ACK"}
    is_thumbs_up = any(c in raw for c in "\U0001f44d\U0001f44d\U0001f3fb\U0001f44d\U0001f3fc\U0001f44d\U0001f3fd\U0001f44d\U0001f3fe\U0001f44d\U0001f3ff")
    # iMessage reactions sent as SMS: "Liked ...", "Loved ...", "Thumbed up ..."
    is_reaction_ack = any(cmd.startswith(w) for w in ["LIKED", "LOVED", "THUMBED UP"])

    if cmd in ack_words or is_thumbs_up or is_reaction_ack:
        msg = get_message_by_id(_owner_context.get(biz_id, 0)) if biz_id in _owner_context else None
        if not msg: msg = get_latest_unacked(biz_id)
        if not msg: return "No active alerts to acknowledge."
        if msg["acknowledged"]: return f"Alert #{msg['id']} already acknowledged."
        mark_acknowledged(msg["id"])
        return f"✅ Alert #{msg['id']} acknowledged."

    if cmd == "HELP":
        return ("Commands:\nDETAILS — View latest alert\n👍 or OK — Acknowledge alert\n"
                "LIST — Last 5 flagged issues\nLIST ALL — Last 5 messages (all types)\n"
                "STATUS — Alert status\nMUTE 2H — Silence for 2 hours\n"
                "PAUSE — Stop all alerts\nRESUME — Resume alerts\nHELP — This message")

    if cmd == "DETAILS":
        msg = get_message_by_id(_owner_context.get(biz_id, 0)) if biz_id in _owner_context else None
        if not msg: msg = get_latest_unacked(biz_id)
        if not msg: return "No active alerts."
        set_context(biz_id, msg["id"])
        ack = "✅ Acknowledged" if msg["acknowledged"] else "⏳ Pending"
        return (f"Alert #{msg['id']} — {ack}\nTime: {_fmt_ts(msg['created_at'])}\n"
                f"Category: {msg['category']}\n"
                f"From: {msg['from_number']}\n"
                f"Message: \"{msg['message_text']}\"\n"
                f"Reply 👍 or OK to acknowledge.")

    if cmd == "LIST ALL":
        msgs = get_recent_all(biz_id, 5)
        if not msgs: return "No messages yet."
        tier_icons = {1: "🚨", 2: "⚠️", 3: "😐", 4: "💬"}
        lines = ["Last 5 messages:\n"]
        for m in msgs:
            icon = tier_icons.get(m["tier"], "💬")
            ack = " ✅" if m["acknowledged"] else ""
            lines.append(f"{icon} #{m['id']} — {m['summary']} ({_fmt_ts(m['created_at'])}){ack}")
        return "\n".join(lines)

    if cmd == "LIST":
        msgs = get_recent_flagged(biz_id, 5)
        if not msgs: return "No flagged issues."
        lines = ["Last 5 flagged issues:\n"]
        for m in msgs:
            a = "✅" if m["acknowledged"] else "⚠️"
            lines.append(f"{a} #{m['id']} — {m['summary']} ({_fmt_ts(m['created_at'])})")
        return "\n".join(lines)

    if cmd == "STATUS":
        name = business.get("name") or biz_id
        if business.get("paused"): return f"📴 Alerts PAUSED for {name}.\nReply RESUME to turn back on."
        mu = business.get("muted_until")
        if mu:
            try:
                until = datetime.fromisoformat(mu)
                if until > datetime.now(timezone.utc):
                    mins = int((until - datetime.now(timezone.utc)).total_seconds() / 60)
                    t = f"{mins//60}h {mins%60}m" if mins >= 60 else f"{mins}m"
                    return f"🔇 Alerts muted for {t} more.\nReply RESUME to unmute."
            except Exception: pass
        return f"🔔 Alerts are ON for {name}."

    if cmd == "PAUSE":
        set_paused(biz_id, True)
        return "📴 Alerts PAUSED. Reply RESUME to turn back on."

    if cmd == "RESUME":
        set_paused(biz_id, False); set_muted_until(biz_id, None)
        return "🔔 Alerts resumed."

    if cmd.startswith("MUTE"):
        m = re.match(r"MUTE\s+(\d+)\s*(H|HR|HRS|HOUR|HOURS|M|MIN|MINS|MINUTE|MINUTES)?", cmd)
        if not m:
            set_muted_until(biz_id, datetime.now(timezone.utc) + timedelta(hours=1))
            return "🔇 Alerts muted for 1 hour. Reply RESUME to unmute."
        amt = int(m.group(1)); unit = (m.group(2) or "H")[0]
        if unit == "M":
            amt = max(1, min(1440, amt))
            set_muted_until(biz_id, datetime.now(timezone.utc) + timedelta(minutes=amt))
            return f"🔇 Alerts muted for {amt} minute{'s' if amt!=1 else ''}. Reply RESUME to unmute."
        else:
            amt = max(1, min(72, amt))
            set_muted_until(biz_id, datetime.now(timezone.utc) + timedelta(hours=amt))
            return f"🔇 Alerts muted for {amt} hour{'s' if amt!=1 else ''}. Reply RESUME to unmute."

    # iMessage reactions that aren't acknowledgments
    if any(cmd.startswith(w) for w in ["EMPHASIZED", "QUESTIONED", "LAUGHED AT", "DISLIKED"]):
        return ""  # Silently ignore non-ack reactions

    return f"Unknown command: \"{raw[:20]}\"\nReply HELP for commands."


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------
def send_all_digests():
    businesses = get_all_businesses()
    sent = 0
    for biz in businesses:
        stats = get_weekly_stats(biz["id"])
        name = biz.get("name") or biz["id"]
        total, flagged, acked = stats["total_messages"], stats["flagged_issues"], stats["acknowledged"]
        if total == 0:
            msg = f"📊 Weekly digest for {name}:\nQuiet week — 0 messages received."
        else:
            lines = [f"📊 Weekly digest for {name}:", f"{total} message{'s' if total!=1 else ''} received"]
            if flagged > 0:
                lines.append(f"{flagged} flagged, {acked} acknowledged")
                lines.append(f"Top category: {stats['top_category'].replace('_',' ')}")
                if acked < flagged: lines.append(f"⚠️ {flagged-acked} unacknowledged — reply LIST")
            else:
                lines.append("No issues flagged — all clear!")
            msg = "\n".join(lines)
        if send_sms(biz["owner_phone"], msg, from_number=biz["twilio_number"]): sent += 1
    return sent


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="SMS Alert System", version="2.0.0")

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 10
_initialized = False

_ENV_OWNER = os.getenv("OWNER_PHONE_NUMBER", "")
_ENV_TWILIO = os.getenv("TWILIO_PHONE_NUMBER", "")
_ENV_NAME = os.getenv("BUSINESS_NAME", "MyBusiness")
_ADMIN_KEY = os.getenv("ADMIN_KEY", "changeme")


def _ensure_init():
    global _initialized
    if _initialized: return
    init_db(); init_classifier(); init_sms()
    if _ENV_OWNER and _ENV_TWILIO:
        if create_business("default", _ENV_NAME, _ENV_OWNER, _ENV_TWILIO):
            logger.info(f"Registered business '{_ENV_NAME}'")
    _initialized = True


@app.get("/")
def root():
    _ensure_init()
    return Response(content=SIGNUP_HTML, media_type="text/html")


@app.get("/health")
def health():
    _ensure_init()
    return {"status": "ok"}


@app.post("/digest")
def digest():
    _ensure_init()
    return {"digests_sent": send_all_digests()}


# ---------------------------------------------------------------------------
# Admin — add a new business customer
# ---------------------------------------------------------------------------
WELCOME_MSG = """Welcome to {name} SMS Alerts! 🎉

Your customers can now text {twilio} with feedback and you'll get alerts here when something needs attention.

Commands you can text back:
DETAILS — See the full message
👍 or OK — Acknowledge alert
LIST — Last 5 flagged issues
LIST ALL — Last 5 messages (all types)
STATUS — Alert status
MUTE 2H — Silence for 2 hours
PAUSE — Stop alerts until RESUME
RESUME — Turn alerts back on
HELP — Get this list anytime

Emergencies always get through, even when muted."""


@app.get("/admin/add")
def admin_add(
    key: str = Query(...),
    name: str = Query(...),
    owner: str = Query(...),
    twilio: str = Query(...),
    biz_id: str = Query(""),
):
    _ensure_init()

    if key != _ADMIN_KEY:
        return {"error": "Invalid admin key"}

    owner = owner.strip()
    twilio = twilio.strip()
    name = name.strip()

    if not owner.startswith("+") or not twilio.startswith("+"):
        return {"error": "Phone numbers must start with + (e.g. +17275551234)"}

    if not biz_id:
        biz_id = name.lower().replace(" ", "-").replace("'", "")[:30]

    ok = create_business(biz_id, name, owner, twilio)
    if not ok:
        return {"error": f"Business '{biz_id}' already exists or Twilio number already in use"}

    return {
        "success": True,
        "business_id": biz_id,
        "name": name,
        "owner_phone": owner,
        "twilio_number": twilio,
        "next_step": f"Set webhook on {twilio} to https://sms-alerts.vercel.app/sms/incoming",
        "send_welcome": f"Visit /admin/welcome?key={key}&biz_id={biz_id} to send the welcome text",
    }


@app.get("/admin/welcome")
def admin_welcome(
    key: str = Query(...),
    biz_id: str = Query(...),
):
    _ensure_init()

    if key != _ADMIN_KEY:
        return {"error": "Invalid admin key"}

    with get_db() as conn:
        biz = _fetchone(conn, _q("SELECT * FROM businesses WHERE id = ?"), (biz_id,))

    if not biz:
        return {"error": f"Business '{biz_id}' not found"}

    msg = WELCOME_MSG.format(name=biz["name"], twilio=biz["twilio_number"])
    ok = send_sms(biz["owner_phone"], msg, from_number=biz["twilio_number"])

    if ok:
        return {"success": True, "sent_to": biz["owner_phone"], "message_preview": msg[:100] + "..."}
    else:
        return {"error": "Failed to send welcome SMS"}


@app.get("/admin/list")
def admin_list(key: str = Query(...)):
    _ensure_init()

    if key != _ADMIN_KEY:
        return {"error": "Invalid admin key"}

    businesses = get_all_businesses()
    return {
        "count": len(businesses),
        "businesses": [
            {"id": b["id"], "name": b["name"], "owner": b["owner_phone"],
             "twilio": b["twilio_number"], "paused": bool(b.get("paused"))}
            for b in businesses
        ],
    }


@app.get("/admin/stats")
def admin_stats(key: str = Query(...), biz_id: str = Query(...)):
    _ensure_init()
    if key != _ADMIN_KEY:
        return {"error": "Invalid admin key"}
    stats = get_weekly_stats(biz_id)
    with get_db() as conn:
        recent = _fetchall(conn, _q("SELECT id,tier,category,summary,acknowledged,created_at FROM messages WHERE business_id=? ORDER BY created_at DESC LIMIT 10"), (biz_id,))
    return {"stats": stats, "recent_messages": recent}


@app.get("/admin/remove")
def admin_remove(key: str = Query(...), biz_id: str = Query(...)):
    _ensure_init()
    if key != _ADMIN_KEY:
        return {"error": "Invalid admin key"}
    with get_db() as conn:
        _execute(conn, _q("DELETE FROM businesses WHERE id=?"), (biz_id,))
    return {"success": True, "removed": biz_id}


@app.get("/admin")
def admin_ui(key: str = Query("")):
    _ensure_init()
    if key != _ADMIN_KEY:
        return Response(content="""<!DOCTYPE html><html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#fafaf8">
<div style="text-align:center"><h2 style="margin:0 0 16px">SMS Alert Admin</h2>
<form style="display:flex;gap:8px" onsubmit="location.href='/admin?key='+document.getElementById('k').value;return false">
<input id="k" type="password" placeholder="Admin key" style="padding:10px 14px;border:1px solid #ddd;border-radius:6px;font-size:15px;width:220px">
<button style="padding:10px 20px;background:#111;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer">Enter</button>
</form></div></body></html>""", media_type="text/html")

    businesses = get_all_businesses()
    biz_rows = ""
    for b in businesses:
        s = get_weekly_stats(b["id"])
        paused = "Paused" if b.get("paused") else "Active"
        biz_rows += f"""<tr>
<td style="padding:12px 16px;font-weight:600">{b["name"]}</td>
<td style="padding:12px 16px;font-family:monospace;font-size:13px">{b["twilio_number"]}</td>
<td style="padding:12px 16px;font-family:monospace;font-size:13px">{b["owner_phone"]}</td>
<td style="padding:12px 16px;text-align:center">{s["total_messages"]}</td>
<td style="padding:12px 16px;text-align:center">{s["flagged_issues"]}</td>
<td style="padding:12px 16px;text-align:center">{paused}</td>
<td style="padding:12px 16px">
<a href="/admin/welcome?key={key}&biz_id={b["id"]}" style="color:#2563eb;text-decoration:none;font-size:13px;margin-right:12px">Resend welcome</a>
<a href="#" onclick="if(confirm('Remove {b["name"]}?'))location.href='/admin/remove?key={key}&biz_id={b["id"]}';return false" style="color:#dc2626;text-decoration:none;font-size:13px">Remove</a>
</td></tr>"""

    if not biz_rows:
        biz_rows = '<tr><td colspan="7" style="padding:24px;text-align:center;color:#999">No businesses yet. Add your first one below.</td></tr>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SMS Alert Admin</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px;background:#fafaf8;color:#1a1a1a;line-height:1.5">
<div style="max-width:960px;margin:0 auto">

<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:32px">
<div><h1 style="margin:0;font-size:24px;font-weight:700">SMS Alert Admin</h1>
<p style="margin:4px 0 0;color:#888;font-size:14px">{len(businesses)} business{"es" if len(businesses)!=1 else ""} registered</p></div>
</div>

<div style="background:#fff;border:1px solid #e5e5e0;border-radius:10px;overflow:hidden;margin-bottom:32px">
<table style="width:100%;border-collapse:collapse;font-size:14px">
<thead><tr style="background:#f5f5f0;border-bottom:1px solid #e5e5e0">
<th style="padding:10px 16px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Business</th>
<th style="padding:10px 16px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Twilio #</th>
<th style="padding:10px 16px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Owner</th>
<th style="padding:10px 16px;text-align:center;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Msgs (7d)</th>
<th style="padding:10px 16px;text-align:center;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Flagged</th>
<th style="padding:10px 16px;text-align:center;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Status</th>
<th style="padding:10px 16px;text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#888">Actions</th>
</tr></thead>
<tbody>{biz_rows}</tbody>
</table>
</div>

<div style="background:#fff;border:1px solid #e5e5e0;border-radius:10px;padding:24px;margin-bottom:32px">
<h2 style="margin:0 0 20px;font-size:18px;font-weight:600">Add new business</h2>
<div id="result" style="display:none;padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px"></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
<div>
<label style="display:block;font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px">Business name</label>
<input id="f-name" type="text" placeholder="Joe's Coffee" style="width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:15px;box-sizing:border-box">
</div>
<div>
<label style="display:block;font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px">Owner phone</label>
<input id="f-owner" type="tel" placeholder="+17275551234" style="width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:15px;box-sizing:border-box">
</div>
<div>
<label style="display:block;font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px">Twilio number (bought for this customer)</label>
<input id="f-twilio" type="tel" placeholder="+17275559999" style="width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:6px;font-size:15px;box-sizing:border-box">
</div>
<div style="display:flex;align-items:end">
<button onclick="addBiz()" style="padding:10px 24px;background:#111;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer;width:100%">Add business</button>
</div>
</div>
<p style="margin:12px 0 0;font-size:13px;color:#999">Phone numbers must include +1 country code. The welcome text with commands is sent automatically.</p>
</div>

<div style="background:#fff;border:1px solid #e5e5e0;border-radius:10px;padding:24px">
<h2 style="margin:0 0 12px;font-size:18px;font-weight:600">Webhook reminder</h2>
<p style="font-size:14px;color:#666;margin:0">Every Twilio number needs its webhook set to:</p>
<p style="font-family:monospace;font-size:14px;background:#f5f5f0;padding:10px 14px;border-radius:6px;margin:8px 0 0">https://sms-alerts.vercel.app/sms/incoming</p>
</div>

</div>
<script>
async function addBiz() {{
    const name = document.getElementById('f-name').value.trim();
    const owner = document.getElementById('f-owner').value.trim();
    const twilio = document.getElementById('f-twilio').value.trim();
    const res = document.getElementById('result');
    if (!name || !owner || !twilio) {{ res.style.display='block'; res.style.background='#fef2f2'; res.style.color='#991b1b'; res.textContent='Please fill in all fields.'; return; }}
    try {{
        const r = await fetch('/admin/add?key={key}&name='+encodeURIComponent(name)+'&owner='+encodeURIComponent(owner)+'&twilio='+encodeURIComponent(twilio));
        const d = await r.json();
        if (d.success) {{ res.style.display='block'; res.style.background='#f0fdf4'; res.style.color='#166534'; res.textContent='Added '+name+'! Welcome text sent. Refreshing...'; setTimeout(()=>location.reload(),1500); }}
        else {{ res.style.display='block'; res.style.background='#fef2f2'; res.style.color='#991b1b'; res.textContent=d.error; }}
    }} catch(e) {{ res.style.display='block'; res.style.background='#fef2f2'; res.style.color='#991b1b'; res.textContent='Something went wrong: '+e.message; }}
}}
</script>
</body></html>"""
    return Response(content=html, media_type="text/html")


async def incoming_sms(From: str = Form(...), Body: str = Form(...), To: str = Form("")):
    _ensure_init()
    sender, body, twilio_num = From.strip(), Body.strip(), To.strip()
    logger.info(f"SMS from {sender}: {body[:80]}")

    # Owner?
    owner_biz = get_business_by_owner(sender)
    if owner_biz:
        return _twiml(handle_owner_command(body, owner_biz))

    # Customer
    biz = get_business_by_twilio(twilio_num)
    if not biz:
        return _twiml("Thanks for reaching out. We've received your message.")

    c = classify_message(body)
    msg_id = store_message(biz["id"], sender, body, c)
    tier, conf, summary = c["tier"], c["confidence"], c.get("summary", "Issue reported")

    # Alert all contacts
    alert_phones = get_alert_phones(biz)
    if alert_phones and not (is_alerts_silenced(biz) and tier != 1):
        if get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW) < RATE_LIMIT_MAX:
            alert = None
            if tier == 1: alert = "🚨 URGENT: Possible emergency reported\nReply: DETAILS"
            elif tier == 2 and conf > 0.7: alert = f"⚠️ Issue reported: {summary}\nReply 👍 or OK to acknowledge"
            if alert:
                sent_any = False
                for phone in alert_phones:
                    if send_sms(phone, alert, from_number=biz["twilio_number"]):
                        sent_any = True
                if sent_any:
                    mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")
                    set_context(biz["id"], msg_id)

    return _twiml(c.get("auto_reply", "Thanks for reaching out. We've received your message."))


def _twiml(msg):
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
           + msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
           + '</Message></Response>')
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Self-serve signup
# ---------------------------------------------------------------------------
SIGNUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotline — Stop losing customers to fixable problems</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',system-ui,sans-serif;background:#09090b;color:#fafafa;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:#f97316;text-decoration:none}
.hero{padding:60px 24px 40px;text-align:center;max-width:640px;margin:0 auto}
.logo{font-size:15px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#f97316;margin-bottom:32px;display:inline-block}
.logo span{background:#f97316;color:#09090b;padding:3px 8px;border-radius:4px;margin-right:6px}
h1{font-size:clamp(32px,6vw,48px);font-weight:700;line-height:1.15;margin-bottom:16px;letter-spacing:-0.02em}
h1 em{font-style:normal;color:#f97316}
.sub{font-size:18px;color:#a1a1aa;max-width:480px;margin:0 auto 40px;line-height:1.5}
.card{background:#18181b;border:1px solid #27272a;border-radius:16px;padding:32px;max-width:440px;margin:0 auto 48px;text-align:left}
.trial{background:rgba(249,115,22,0.1);border:1px solid rgba(249,115,22,0.25);color:#fb923c;padding:10px 16px;border-radius:8px;font-size:14px;font-weight:500;margin-bottom:20px;text-align:center}
label{display:block;font-size:13px;font-weight:500;color:#71717a;margin-bottom:4px;margin-top:14px}
label:first-of-type{margin-top:0}
input[type=text],input[type=tel]{width:100%;padding:12px 14px;background:#09090b;border:1px solid #3f3f46;border-radius:8px;font-size:16px;color:#fafafa;font-family:inherit;transition:border-color 0.2s}
input::placeholder{color:#52525b}
input:focus{outline:none;border-color:#f97316}
.btn{width:100%;padding:14px;background:#f97316;color:#09090b;border:none;border-radius:8px;font-size:16px;font-weight:700;cursor:pointer;margin-top:20px;font-family:inherit;transition:background 0.2s}
.btn:hover{background:#ea580c}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.result{padding:14px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;line-height:1.5;display:none}
.ok{background:rgba(34,197,94,0.1);color:#4ade80;border:1px solid rgba(34,197,94,0.25)}
.err{background:rgba(239,68,68,0.1);color:#f87171;border:1px solid rgba(239,68,68,0.25)}
.spinner{display:inline-block;width:16px;height:16px;border:2.5px solid #09090b;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.proof{text-align:center;max-width:640px;margin:0 auto;padding:0 24px 48px}
.proof h2{font-size:14px;font-weight:500;color:#71717a;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:24px}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:48px}
.step{background:#18181b;border:1px solid #27272a;border-radius:12px;padding:20px;text-align:center}
.step-num{width:32px;height:32px;border-radius:50%;background:rgba(249,115,22,0.15);color:#f97316;font-weight:700;font-size:14px;display:inline-flex;align-items:center;justify-content:center;margin-bottom:10px}
.step h3{font-size:15px;font-weight:600;margin-bottom:4px}
.step p{font-size:13px;color:#a1a1aa;line-height:1.4}
.features{display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:520px;margin:0 auto 48px;padding:0 24px}
.feat{background:#18181b;border:1px solid #27272a;border-radius:10px;padding:16px 18px}
.feat strong{font-size:14px;display:block;margin-bottom:2px}
.feat p{font-size:13px;color:#71717a;margin:0;line-height:1.4}
.sms-demo{max-width:360px;margin:0 auto 48px;padding:0 24px}
.sms-demo h2{font-size:14px;font-weight:500;color:#71717a;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:16px;text-align:center}
.bubble{padding:10px 14px;border-radius:16px;font-size:14px;margin-bottom:8px;max-width:85%;line-height:1.4;animation:fadeUp 0.4s ease both}
.bubble.in{background:#27272a;color:#e4e4e7;border-bottom-left-radius:4px}
.bubble.out{background:#f97316;color:#09090b;margin-left:auto;border-bottom-right-radius:4px}
.bubble.alert{background:rgba(249,115,22,0.1);border:1px solid rgba(249,115,22,0.25);color:#fb923c;border-bottom-left-radius:4px}
.bubble .label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;opacity:0.6;margin-bottom:4px}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.bubble:nth-child(2){animation-delay:0.15s}.bubble:nth-child(3){animation-delay:0.3s}.bubble:nth-child(4){animation-delay:0.45s}.bubble:nth-child(5){animation-delay:0.6s}
footer{text-align:center;padding:32px 24px;color:#52525b;font-size:13px;border-top:1px solid #1e1e22}
@media(max-width:600px){.steps{grid-template-columns:1fr}.features{grid-template-columns:1fr}}
</style></head><body>

<div class="hero">
<div class="logo"><span>H</span> HOTLINE</div>
<h1>Stop losing customers to <em>fixable problems</em></h1>
<p class="sub">Your customers text you when something's wrong. AI filters the noise and alerts you instantly. No app to install — just SMS.</p>
<a href="/demo" style="display:inline-block;padding:10px 22px;border:1px solid rgba(249,115,22,0.4);border-radius:8px;color:#fb923c;font-size:14px;font-weight:500;margin-bottom:8px;transition:background 0.2s">Try the live demo &rarr;</a>
</div>

<div class="card">
<div class="trial">14-day free trial &middot; No credit card required</div>
<div class="result" id="result"></div>
<label>Business name</label>
<input type="text" id="f-name" placeholder="Joe's Coffee">
<label>Your cell phone (where you'll get alerts)</label>
<input type="tel" id="f-phone" placeholder="(727) 555-1234">
<label>Preferred area code (optional)</label>
<input type="text" id="f-area" placeholder="727" maxlength="3" style="width:100px">
<button class="btn" id="f-btn" onclick="signup()">Get my number &rarr;</button>
</div>

<div class="proof">
<h2>How it works</h2>
<div class="steps">
<div class="step"><div class="step-num">1</div><h3>Sign up</h3><p>Get a unique phone number for your business in seconds</p></div>
<div class="step"><div class="step-num">2</div><h3>Display it</h3><p>Put the number on a sign, sticker, or receipt in your location</p></div>
<div class="step"><div class="step-num">3</div><h3>Get alerts</h3><p>AI reads every text and alerts you when something needs attention</p></div>
</div>
</div>

<div class="sms-demo">
<h2>See it in action</h2>
<div class="bubble in"><div class="label">Customer texts</div>Bathroom is disgusting and nobody is at the front desk</div>
<div class="bubble out"><div class="label">Auto-reply to customer</div>We've flagged this as a cleanliness issue and notified the manager. Thank you.</div>
<div class="bubble alert"><div class="label">You get an alert</div>&#9888;&#65039; Issue reported: Cleanliness and staffing issue<br>Reply &#128077; or OK to acknowledge</div>
<div class="bubble in" style="background:#18181b;border:1px solid #27272a"><div class="label">You reply</div>ACK</div>
<div class="bubble out" style="background:#27272a;color:#e4e4e7"><div class="label">System confirms</div>&#9989; Alert #14 marked as acknowledged.</div>
</div>

<div class="features">
<div class="feat"><strong>AI-powered filtering</strong><p>Only alerts on real issues. Positive feedback and spam stay quiet.</p></div>
<div class="feat"><strong>Manage by text</strong><p>DETAILS, ACK, LIST, MUTE, PAUSE — all via SMS from your phone.</p></div>
<div class="feat"><strong>Weekly digest</strong><p>Every Sunday: message count, top issues, and what needs follow-up.</p></div>
<div class="feat"><strong>Mute when busy</strong><p>Text MUTE 2H before a rush. Emergencies always get through.</p></div>
</div>

<footer>Hotline &middot; AI-powered customer alerts for small businesses</footer>

<script>
async function signup() {
    const name = document.getElementById('f-name').value.trim();
    let phone = document.getElementById('f-phone').value.trim().replace(/[\\s\\-\\(\\)]/g,'');
    const area = document.getElementById('f-area').value.trim();
    const res = document.getElementById('result');
    const btn = document.getElementById('f-btn');

    if (!phone.startsWith('+')) {
        if (phone.startsWith('1') && phone.length === 11) phone = '+' + phone;
        else if (phone.length === 10) phone = '+1' + phone;
        else { res.className='result err'; res.style.display='block'; res.textContent='Please enter a valid US phone number.'; return; }
    }
    if (!name) { res.className='result err'; res.style.display='block'; res.textContent='Please enter your business name.'; return; }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Setting up your number...';
    res.style.display = 'none';

    try {
        const r = await fetch('/signup/create', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({name, phone, area_code: area})
        });
        const d = await r.json();
        if (d.success) {
            res.className='result ok';
            res.innerHTML = '<strong>You are live!</strong><br><br>Your business number: <strong>'+d.twilio_number+'</strong><br><br>A welcome text with your commands has been sent to '+d.owner_phone+'. Display your new number in your business and customers can start texting right away.';
            res.style.display='block';
            btn.textContent='Done!';
        } else {
            res.className='result err';
            res.textContent=d.error || 'Something went wrong. Please try again.';
            res.style.display='block';
            btn.disabled=false;
            btn.innerHTML='Get my number &rarr;';
        }
    } catch(e) {
        res.className='result err';
        res.textContent='Connection error. Please try again.';
        res.style.display='block';
        btn.disabled=false;
        btn.innerHTML='Get my number &rarr;';
    }
}
</script>
</body></html>"""


@app.get("/signup")
def signup_page():
    _ensure_init()
    return Response(content=SIGNUP_HTML, media_type="text/html")


# ---------------------------------------------------------------------------
# Live demo — no SMS, just AI classification
# ---------------------------------------------------------------------------
@app.post("/demo/classify")
async def demo_classify(request_data: dict = None):
    _ensure_init()
    text = (request_data or {}).get("message", "").strip()
    if not text:
        return {"error": "No message provided"}
    if len(text) > 500:
        return {"error": "Message too long"}
    c = classify_message(text)
    return {
        "tier": c["tier"],
        "category": c["category"],
        "sentiment": c["sentiment"],
        "confidence": c["confidence"],
        "summary": c["summary"],
        "auto_reply": c["auto_reply"],
        "tier_label": {1: "Emergency", 2: "Business-Critical", 3: "Reputation Risk", 4: "Routine"}.get(c["tier"], "Unknown"),
        "would_alert": c["tier"] == 1 or (c["tier"] == 2 and c["confidence"] > 0.7),
    }


DEMO_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hotline — Live Demo</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',system-ui,sans-serif;background:#09090b;color:#fafafa;-webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px}
.top{text-align:center;margin-bottom:28px}
.logo{font-size:13px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#f97316;margin-bottom:8px}
.logo span{background:#f97316;color:#09090b;padding:2px 6px;border-radius:3px;margin-right:4px}
h1{font-size:22px;font-weight:600;margin-bottom:4px}
.sub{font-size:14px;color:#71717a}
.phones{display:flex;gap:20px;margin-bottom:20px;align-items:stretch}
.phone{flex:1;background:#18181b;border:1px solid #27272a;border-radius:20px;display:flex;flex-direction:column;overflow:hidden;min-height:460px}
.phone-header{padding:14px 16px 10px;border-bottom:1px solid #27272a;text-align:center}
.phone-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em}
.phone-label.customer{color:#3b82f6}
.phone-label.owner{color:#f97316}
.phone-sub{font-size:11px;color:#52525b;margin-top:2px}
.msgs{flex:1;padding:14px;overflow-y:auto;min-height:280px}
.bubble{padding:10px 14px;border-radius:16px;font-size:13px;margin-bottom:8px;max-width:88%;line-height:1.45;animation:fadeUp 0.3s ease both}
.bubble.in{background:#27272a;color:#e4e4e7;border-bottom-left-radius:4px}
.bubble.out-blue{background:#3b82f6;color:#fff;margin-left:auto;border-bottom-right-radius:4px}
.bubble.out-orange{background:#f97316;color:#09090b;margin-left:auto;border-bottom-right-radius:4px;font-weight:500}
.bubble.alert{background:rgba(249,115,22,0.1);border:1px solid rgba(249,115,22,0.25);color:#fb923c;border-bottom-left-radius:4px}
.bubble.system{background:rgba(113,113,122,0.1);color:#71717a;font-size:12px;text-align:center;max-width:100%;border-radius:8px}
.bubble.cmd{background:#27272a;color:#e4e4e7;margin-left:auto;border-bottom-right-radius:4px;font-family:monospace;font-weight:500}
.bubble.resp{background:rgba(113,113,122,0.15);color:#d4d4d8;border-bottom-left-radius:4px;font-size:12px;white-space:pre-line;line-height:1.5}
.bubble .lbl{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;opacity:0.5;margin-bottom:3px}
.meta{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
.tag{font-size:10px;padding:2px 7px;border-radius:4px;font-weight:500}
.tag.t1{background:rgba(239,68,68,0.15);color:#f87171}
.tag.t2{background:rgba(249,115,22,0.15);color:#fb923c}
.tag.t3{background:rgba(250,204,21,0.15);color:#fbbf24}
.tag.t4{background:rgba(113,113,122,0.15);color:#a1a1aa}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.input-area{padding:10px 14px 14px;border-top:1px solid #27272a}
.input-row{display:flex;gap:6px}
.input-row input{flex:1;padding:10px 12px;background:#09090b;border:1px solid #3f3f46;border-radius:8px;font-size:14px;color:#fafafa;font-family:inherit}
.input-row input::placeholder{color:#52525b}
.input-row input:focus{outline:none;border-color:#f97316}
.input-row button{padding:10px 14px;background:#f97316;color:#09090b;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap}
.input-row button.blue{background:#3b82f6;color:#fff}
.input-row button:disabled{opacity:0.3;cursor:not-allowed}
.owner-input{display:none}
.owner-cmds{display:none;padding:4px 14px 6px;gap:5px;flex-wrap:wrap}
.cmd-btn{font-size:11px;padding:5px 10px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#a1a1aa;cursor:pointer;font-family:monospace;font-weight:600}
.cmd-btn:hover{border-color:#f97316;color:#fafafa}
.examples{margin-bottom:20px}
.examples p{font-size:12px;color:#52525b;margin-bottom:6px;text-align:center}
.ex-row{display:flex;flex-wrap:wrap;gap:6px;justify-content:center}
.ex{font-size:12px;padding:6px 10px;background:#18181b;border:1px solid #27272a;border-radius:6px;color:#a1a1aa;cursor:pointer;transition:border-color 0.2s}
.ex:hover{border-color:#3b82f6;color:#fafafa}
.cta{text-align:center;margin-top:20px}
.cta a{display:inline-block;padding:12px 28px;background:#f97316;color:#09090b;border-radius:8px;font-weight:700;font-size:15px;text-decoration:none}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:4px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:640px){.phones{flex-direction:column}.phone{min-height:360px}}
</style></head><body>
<div class="wrap">
<div class="top">
<div class="logo"><span>H</span> HOTLINE</div>
<h1>Try it live</h1>
<p class="sub">Type a message as a customer and watch both sides in real time</p>
</div>

<div class="examples">
<p>Try an example:</p>
<div class="ex-row">
<div class="ex" onclick="tryEx(this)">Bathroom is disgusting</div>
<div class="ex" onclick="tryEx(this)">No one is at the front desk</div>
<div class="ex" onclick="tryEx(this)">Great coffee today!</div>
<div class="ex" onclick="tryEx(this)">Waited 30 minutes and leaving</div>
<div class="ex" onclick="tryEx(this)">There's a fire in the back</div>
<div class="ex" onclick="tryEx(this)">Your staff was so friendly</div>
</div>
</div>

<div class="phones">
<div class="phone" id="p-cust">
<div class="phone-header"><div class="phone-label customer">Customer</div><div class="phone-sub">Texts your business number</div></div>
<div class="msgs" id="m-cust"><div class="bubble system">Customer messages appear here</div></div>
<div class="input-area"><div class="input-row">
<input type="text" id="cust-input" placeholder="Type a customer message..." onkeydown="if(event.key==='Enter')sendDemo()">
<button class="blue" id="cust-btn" onclick="sendDemo()">Send</button>
</div></div>
</div>

<div class="phone" id="p-owner">
<div class="phone-header"><div class="phone-label owner">Owner</div><div class="phone-sub">Receives alerts on their phone</div></div>
<div class="msgs" id="m-owner"><div class="bubble system">Owner alerts appear here</div></div>
<div class="owner-cmds" id="owner-cmds">
<div class="cmd-btn" onclick="ownerCmd('DETAILS')">DETAILS</div>
<div class="cmd-btn" onclick="ownerCmd('OK')">OK</div>
<div class="cmd-btn" onclick="ownerCmd('HELP')">HELP</div>
</div>
<div class="input-area owner-input" id="owner-input"><div class="input-row">
<input type="text" id="owner-inp" placeholder="Type a command..." onkeydown="if(event.key==='Enter')ownerCmd(this.value)">
<button onclick="ownerCmd(document.getElementById('owner-inp').value)">Send</button>
</div></div>
</div>
</div>

<div class="cta">
<a href="/signup">Get Hotline for your business &rarr;</a>
</div>
</div>

<script>
let lastData = null;
let acked = false;
const mc = document.getElementById('m-cust');
const mo = document.getElementById('m-owner');

function addB(container, cls, label, text, extra) {
    const d = document.createElement('div');
    d.className = 'bubble ' + cls;
    let h = '';
    if (label) h += '<div class="lbl">' + label + '</div>';
    h += text;
    if (extra) h += extra;
    d.innerHTML = h;
    container.appendChild(d);
    container.scrollTop = container.scrollHeight;
    return d;
}

function tryEx(el) {
    document.getElementById('cust-input').value = el.textContent;
    sendDemo();
}

function showOwnerInput() {
    document.getElementById('owner-cmds').style.display = 'flex';
    document.getElementById('owner-input').style.display = 'block';
}
function hideOwnerInput() {
    document.getElementById('owner-cmds').style.display = 'none';
    document.getElementById('owner-input').style.display = 'none';
}

function ownerCmd(raw) {
    const cmd = (raw || '').trim().toUpperCase();
    document.getElementById('owner-inp').value = '';
    if (!cmd) return;

    addB(mo, 'cmd', '', raw.trim());

    if (cmd === 'HELP') {
        addB(mo, 'resp', '', 'Commands:\\nDETAILS \\u2014 View latest alert\\n\\ud83d\\udc4d or OK \\u2014 Acknowledge alert\\nLIST \\u2014 Last 5 flagged issues\\nLIST ALL \\u2014 Last 5 messages (all types)\\nSTATUS \\u2014 Alert status\\nMUTE 2H \\u2014 Silence for 2 hours\\nPAUSE \\u2014 Stop all alerts\\nRESUME \\u2014 Resume alerts\\nHELP \\u2014 This message');
        return;
    }

    if (!lastData) {
        addB(mo, 'resp', '', 'No active alerts.');
        return;
    }

    if (cmd === 'DETAILS') {
        const d = lastData;
        const now = new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
        const ackLabel = acked ? '\\u2705 Acknowledged' : '\\u23f3 Pending';
        addB(mo, 'resp', '', 'Alert \\u2014 ' + ackLabel + '\\nTime: ' + now + '\\nCategory: ' + d.category.replace('_',' ') + '\\nFrom: (555) 867-5309\\nMessage: "' + d.original_message + '"\\nReply \\ud83d\\udc4d or OK to acknowledge.');
        return;
    }

    if (['OK','GOT IT','DONE','ON IT','ACK'].includes(cmd) || raw.includes('\\ud83d\\udc4d')) {
        if (acked) {
            addB(mo, 'resp', '', 'Already acknowledged.');
        } else {
            acked = true;
            addB(mo, 'resp', '', '\\u2705 Alert acknowledged.');
        }
        return;
    }

    addB(mo, 'resp', '', 'Unknown command: "' + cmd + '"\\nReply HELP for commands.');
}

async function sendDemo() {
    const inp = document.getElementById('cust-input');
    const btn = document.getElementById('cust-btn');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    btn.disabled = true;
    hideOwnerInput();
    acked = false;

    addB(mc, 'out-blue', '', text);
    addB(mo, 'system', '', '<span class="spinner"></span> Processing...');

    try {
        const r = await fetch('/demo/classify', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({message: text})
        });
        const d = await r.json();
        d.original_message = text;
        lastData = d;

        mo.lastChild.remove();

        await new Promise(r => setTimeout(r, 300));
        addB(mc, 'in', 'Auto-reply', d.auto_reply);

        await new Promise(r => setTimeout(r, 400));

        const tierCls = 't' + d.tier;
        const tags = '<div class="meta">'
            + '<span class="tag ' + tierCls + '">' + d.tier_label + '</span>'
            + '<span class="tag ' + tierCls + '">' + d.category.replace('_',' ') + '</span>'
            + '</div>';

        if (d.would_alert) {
            const alertText = d.tier === 1
                ? '\\ud83d\\udea8 URGENT: Possible emergency reported\\nReply: DETAILS'
                : '\\u26a0\\ufe0f Issue reported: ' + d.summary + '\\nReply \\ud83d\\udc4d or OK to acknowledge';
            addB(mo, 'alert', 'Alert', alertText + tags);
            showOwnerInput();
        } else {
            addB(mo, 'system', '', 'No alert \\u2014 ' + d.tier_label.toLowerCase() + tags);
        }
    } catch(e) {
        mo.lastChild.remove();
        addB(mo, 'system', '', 'Demo error. Try again.');
    }
    btn.disabled = false;
    inp.focus();
}
</script>
</body></html>"""


@app.get("/demo")
def demo_page():
    _ensure_init()
    return Response(content=DEMO_HTML, media_type="text/html")


@app.post("/signup/create")
async def signup_create(request_data: dict = None):
    _ensure_init()

    if not request_data:
        return {"error": "Missing request data"}

    name = (request_data.get("name") or "").strip()
    phone = (request_data.get("phone") or "").strip()
    area_code = (request_data.get("area_code") or "").strip()

    if not name:
        return {"error": "Business name is required"}
    if not phone or not phone.startswith("+"):
        return {"error": "Valid phone number with country code is required"}

    # Generate a business ID
    biz_id = name.lower().replace(" ", "-").replace("'", "").replace('"', "")[:30]
    biz_id = re.sub(r"[^a-z0-9\-]", "", biz_id)
    # Check if it already exists, if so append a number
    existing = None
    with get_db() as conn:
        existing = _fetchone(conn, _q("SELECT id FROM businesses WHERE id=?"), (biz_id,))
    if existing:
        biz_id = biz_id[:25] + "-" + datetime.now(timezone.utc).strftime("%H%M%S")

    # Buy a Twilio number
    webhook = "https://sms-alerts.vercel.app/sms/incoming"
    twilio_number = buy_twilio_number(area_code=area_code, webhook_url=webhook)
    if not twilio_number:
        return {"error": "Could not provision a phone number. Please try again or contact support."}

    # Register the business
    ok = create_business(biz_id, name, phone, twilio_number)
    if not ok:
        return {"error": "Could not create business. Phone number may already be registered."}

    # Send welcome text
    msg = WELCOME_MSG.format(name=name, twilio=twilio_number)
    send_sms(phone, msg, from_number=twilio_number)

    logger.info(f"Self-serve signup: {name} ({biz_id}) → {twilio_number} → {phone}")

    return {
        "success": True,
        "business_id": biz_id,
        "name": name,
        "owner_phone": phone,
        "twilio_number": twilio_number,
    }

