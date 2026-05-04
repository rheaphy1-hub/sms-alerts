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

from fastapi import FastAPI, Form, Response

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
            owner_phone TEXT NOT NULL, twilio_number TEXT NOT NULL UNIQUE,
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


def create_business(biz_id, name, owner_phone, twilio_number):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            _execute(conn, _q("INSERT INTO businesses (id,name,owner_phone,twilio_number,created_at) VALUES (?,?,?,?,?)"),
                     (biz_id, name, owner_phone, twilio_number, now))
        return True
    except Exception:
        return False


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
    clean = _normalize_phone(owner_phone)
    with get_db() as conn:
        row = _fetchone(conn, _q("SELECT * FROM businesses WHERE owner_phone = ?"), (clean,))
        if row:
            return row
        for r in _fetchall(conn, "SELECT * FROM businesses"):
            if _normalize_phone(r["owner_phone"])[-10:] == clean[-10:]:
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

Extract: category (cleanliness/staffing/equipment/wait_time/safety/other), sentiment (negative/neutral/positive), confidence (0.0-1.0), summary (5-10 words), auto_reply (1-2 sentences acknowledging the specific issue, under 160 chars. For tier 1-2 confirm issue type and say manager notified. For tier 3 empathize. For tier 4 respond naturally. Never ask questions.)

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
                "auto_reply":"We've received your message and are notifying the manager immediately."}

    crit = {"cleanliness": (["dirty","disgusting","filthy","mess","bathroom","gross","unsanitary"],
                            "We've flagged this as a cleanliness issue and notified the manager. Thank you."),
            "staffing": (["no one","nobody","empty","no staff","where is everyone","closed"],
                         "We've flagged this as a staffing issue and notified the manager. Sorry about that."),
            "equipment": (["broken","machine","not working","out of order","malfunction"],
                          "We've flagged this as an equipment issue and notified the manager. Thank you."),
            "wait_time": (["waited","waiting","slow","forever","leaving","20 minutes","30 minutes"],
                          "We're sorry about the wait. We've notified the manager about the delay.")}
    for cat, (words, reply) in crit.items():
        if any(w in t for w in words):
            return {"tier":2,"category":cat,"sentiment":"negative","confidence":0.85,
                    "summary":f"{cat.replace('_',' ').title()} issue reported","auto_reply":reply}

    neg = ["bad","terrible","awful","rude","worst","hate","angry","disappointed","unhappy","never coming back"]
    if any(w in t for w in neg):
        return {"tier":3,"category":"other","sentiment":"negative","confidence":0.6,
                "summary":"Unhappy customer feedback",
                "auto_reply":"We're sorry to hear about your experience. Your feedback has been noted."}

    return {"tier":4,"category":"other","sentiment":"neutral","confidence":0.5,
            "summary":"General message received",
            "auto_reply":"Thanks for reaching out. We've received your message."}


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
    cmd = text.strip().upper()

    if cmd == "HELP":
        return ("Commands:\nDETAILS — View latest alert\nACK — Acknowledge alert\n"
                "LIST — Last 5 flagged issues\nSTATUS — Alert status\n"
                "MUTE 2H — Silence for 2 hours\nPAUSE — Stop all alerts\n"
                "RESUME — Resume alerts\nHELP — This message")

    if cmd == "DETAILS":
        msg = get_message_by_id(_owner_context.get(biz_id, 0)) if biz_id in _owner_context else None
        if not msg: msg = get_latest_unacked(biz_id)
        if not msg: return "No active alerts."
        set_context(biz_id, msg["id"])
        ack = "✅ Acknowledged" if msg["acknowledged"] else "⏳ Pending"
        return (f"Alert #{msg['id']} — {ack}\nTime: {_fmt_ts(msg['created_at'])}\n"
                f"Category: {msg['category']}\nTier: {msg['tier']} | Confidence: {msg['confidence']:.0%}\n"
                f"Message: \"{msg['message_text']}\"\nReply ACK to acknowledge.")

    if cmd == "ACK":
        msg = get_message_by_id(_owner_context.get(biz_id, 0)) if biz_id in _owner_context else None
        if not msg: msg = get_latest_unacked(biz_id)
        if not msg: return "No active alerts to acknowledge."
        if msg["acknowledged"]: return f"Alert #{msg['id']} already acknowledged."
        mark_acknowledged(msg["id"])
        return f"✅ Alert #{msg['id']} marked as acknowledged."

    if cmd == "LIST":
        msgs = get_recent_flagged(biz_id, 5)
        if not msgs: return "No flagged issues."
        lines = ["Last 5 flagged issues:\n"]
        for m in msgs:
            a = "✅" if m["acknowledged"] else "⚠️"
            lines.append(f"{a} #{m['id']} T{m['tier']} — {m['summary']} ({_fmt_ts(m['created_at'])})")
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

    return f"Unknown command: \"{text.strip()[:20]}\"\nReply HELP for commands."


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
    return {"service": "SMS Alert System", "status": "ok"}


@app.get("/health")
def health():
    _ensure_init()
    return {"status": "ok"}


@app.post("/digest")
def digest():
    _ensure_init()
    return {"digests_sent": send_all_digests()}


@app.post("/sms/incoming")
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

    # Alert owner
    owner_phone = biz["owner_phone"]
    if owner_phone and not (is_alerts_silenced(biz) and tier != 1):
        if get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW) < RATE_LIMIT_MAX:
            alert = None
            if tier == 1: alert = "🚨 URGENT: Possible emergency reported\nReply: DETAILS"
            elif tier == 2 and conf > 0.7: alert = f"⚠️ Issue detected: {summary}\nReply: DETAILS or ACK"
            if alert:
                if send_sms(owner_phone, alert, from_number=biz["twilio_number"]):
                    mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")
                    set_context(biz["id"], msg_id)

    return _twiml(c.get("auto_reply", "Thanks for reaching out. We've received your message."))


def _twiml(msg):
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
           + msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
           + '</Message></Response>')
    return Response(content=xml, media_type="application/xml")
