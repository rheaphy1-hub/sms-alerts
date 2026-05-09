"""
Hotline — SMS Alert System. Single-file Vercel deployment.
"""
import os, re, json, logging
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from fastapi import FastAPI, Form, Response, Query
from fastapi.staticfiles import StaticFiles

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
    with get_db() as c:
        _execute(c, f"""CREATE TABLE IF NOT EXISTS businesses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', owner_phone TEXT NOT NULL,
            alert_phones TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
            digest_freq TEXT NOT NULL DEFAULT 'weekly', alert_tier3 INTEGER DEFAULT 0,
            website_url TEXT NOT NULL DEFAULT '', website_info TEXT NOT NULL DEFAULT '',
            twilio_number TEXT NOT NULL UNIQUE, muted_until TEXT, paused INTEGER DEFAULT 0,
            created_at TEXT NOT NULL)""")
        _execute(c, f"""CREATE TABLE IF NOT EXISTS messages (
            id {s} {pk}, business_id TEXT NOT NULL, from_number TEXT NOT NULL,
            message_text TEXT NOT NULL, tier INTEGER, category TEXT, sentiment TEXT,
            confidence REAL, summary TEXT, acknowledged INTEGER DEFAULT 0,
            alerted INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
        _execute(c, f"""CREATE TABLE IF NOT EXISTS alert_log (
            id {s} {pk}, message_id INTEGER NOT NULL, business_id TEXT NOT NULL,
            alert_type TEXT NOT NULL, sent_at TEXT NOT NULL)""")
        _execute(c, "CREATE INDEX IF NOT EXISTS idx_biz_owner ON businesses(owner_phone)")
        _execute(c, "CREATE INDEX IF NOT EXISTS idx_msg_biz ON messages(business_id, tier, acknowledged)")
        for col, default in [("alert_phones","''"),("email","''"),("digest_freq","'weekly'"),
                             ("alert_tier3","0"),("website_url","''"),("website_info","''")]:
            try: _execute(c, f"ALTER TABLE businesses ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            except: pass

def create_business(biz_id, name, owner_phone, twilio_number, extra_phones="", email="", website_url=""):
    now = datetime.now(timezone.utc).isoformat()
    all_phones = ",".join([owner_phone] + [p.strip() for p in extra_phones.split(",") if p.strip()]) if extra_phones else owner_phone
    website_info = scrape_website_info(website_url) if website_url else ""
    try:
        with get_db() as c:
            _execute(c, _q("INSERT INTO businesses (id,name,owner_phone,alert_phones,email,website_url,website_info,twilio_number,created_at) VALUES (?,?,?,?,?,?,?,?,?)"),
                     (biz_id, name, owner_phone, all_phones, email or "", website_url or "", website_info, twilio_number, now))
        return True
    except: return False

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

def init_sms():
    global _twilio_client, _twilio_from
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    _twilio_from = os.getenv("TWILIO_PHONE_NUMBER", "")
    if sid and token:
        from twilio.rest import Client; _twilio_client = Client(sid, token); logger.info("Twilio ready")
    else: logger.warning("Twilio not configured")

def send_sms(to, body, from_number=""):
    sender = from_number or _twilio_from
    if not _twilio_client: logger.info(f"[DRY-RUN] {sender} -> {to}: {body}"); return True
    try: _twilio_client.messages.create(body=body, from_=sender, to=to); return True
    except Exception as e: logger.error(f"SMS failed to {to}: {e}"); return False

def buy_twilio_number(area_code="", webhook_url=""):
    if not _twilio_client: return "+15550000000"
    try:
        kw = {"limit":1,"sms_enabled":True,"voice_enabled":False}
        if area_code: kw["area_code"] = area_code
        avail = _twilio_client.available_phone_numbers("US").local.list(**kw)
        if not avail and "area_code" in kw: del kw["area_code"]; avail = _twilio_client.available_phone_numbers("US").local.list(**kw)
        if not avail: return None
        num = _twilio_client.incoming_phone_numbers.create(phone_number=avail[0].phone_number, sms_url=webhook_url or "https://sms-alerts.vercel.app/sms/incoming", sms_method="POST")
        return num.phone_number
    except Exception as e: logger.error(f"Buy number failed: {e}"); return None


# --- Email (SendGrid) ---
SENDGRID_KEY = (os.getenv("SENDGRID_API_KEY") or "").strip()
DIGEST_FROM_EMAIL = os.getenv("DIGEST_FROM_EMAIL", "alerts@hotline.so")

def send_email(to_email, subject, html_body):
    if not SENDGRID_KEY: logger.info(f"[DRY-RUN] Email to {to_email}: {subject}"); return True
    try:
        import urllib.request
        data = json.dumps({"personalizations":[{"to":[{"email":to_email}]}],"from":{"email":DIGEST_FROM_EMAIL,"name":"Hotline"},"subject":subject,"content":[{"type":"text/html","value":html_body}]}).encode()
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=data, headers={"Authorization":f"Bearer {SENDGRID_KEY}","Content-Type":"application/json"}, method="POST")
        urllib.request.urlopen(req); return True
    except Exception as e: logger.error(f"Email failed: {e}"); return False


# --- AI Classifier ---
_ai_client = None

CLASSIFICATION_PROMPT = """You are a business issue classifier for an SMS alert system called Hotline. Analyze customer messages and return structured JSON.

TIER DEFINITIONS:
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, flooding, gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, water damage in progress (burst pipe, overflowing toilet/sink). Flooding IS always Tier 1 (slip hazards, electrical risk, property damage).
  NOT Tier 1: Figurative language. "fire her", "dumpster fire", "killing it", "blowing up", "on fire today", "she got fired" — these are complaints or compliments, never emergencies.
- Tier 2: Business-Critical (Orange Alert) — Operations broken, customers being lost right now. No staff present, equipment broken/out of order, supply outages (no toilet paper, soap, napkins), extreme wait times (20+ min, threatening to leave), access blocked (can't get in door), health/hygiene issues (disgusting bathroom, unsanitary).
- Tier 3: Reputation Risk (Yellow) — Customer unhappy, no operational failure. Rude staff, music too loud, temperature complaints, general disappointment, "never coming back."
- Tier 4: Routine (Gray) — No action needed. Positive feedback, compliments, general questions (hours, location, menu), neutral messages.

Categories: cleanliness, staffing, equipment, wait_time, safety, supply, inquiry, other
- "inquiry" = any question about the business (hours, directions, menu, policies, parking, accessibility)
- "supply" = out of something (toilet paper, soap, napkins, cups)
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

EDGE CASES:
- "Music is too loud" = Tier 3 (preference, not operational). Acknowledge, don't promise change.
- "Can't get in the front door" = Tier 2 (access blocked = operational).
- "What time do you close?" = Tier 4, inquiry. Don't answer. Forward.
- "You should fire her" = Tier 3, staffing. Employment complaint, NOT emergency.
- "Bathroom is flooding!" = Tier 1, safety. Always emergency.
- "Out of toilet paper" = Tier 2, supply.

{website_context}

Respond ONLY with JSON: {{"tier":<int>,"category":"<str>","sentiment":"<str>","confidence":<float>,"summary":"<str>","auto_reply":"<str>"}}"""

def init_classifier():
    global _ai_client
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        from anthropic import Anthropic; _ai_client = Anthropic(api_key=key)
    else: logger.warning("No ANTHROPIC_API_KEY")

def classify_message(text, website_info=""):
    ctx = f"Business website info (use ONLY for answering basic questions like hours/address): {website_info}" if website_info else "No business website info available. Do NOT guess answers to customer questions."
    prompt = CLASSIFICATION_PROMPT.replace("{website_context}", ctx)
    if _ai_client:
        try:
            resp = _ai_client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300, system=prompt,
                messages=[{"role":"user","content":f'Classify this customer SMS:\n\n"{text}"'}])
            raw = resp.content[0].text.strip()
            if raw.startswith("```"): raw = raw.split("\n",1)[1].rsplit("```",1)[0].strip()
            r = json.loads(raw)
            r["tier"] = max(1,min(4,int(r.get("tier",4))))
            r["confidence"] = max(0.0,min(1.0,float(r.get("confidence",0.5))))
            for k,v in [("category","other"),("sentiment","neutral"),("summary",text[:50]),("auto_reply","Thanks so much for reaching out! We've noted your message.")]:
                r.setdefault(k,v)
            return r
        except Exception as e: logger.error(f"AI classify failed: {e}")
    return _classify_fallback(text)

def _classify_fallback(text):
    t = text.lower()
    # Check for figurative "fire" (fire her, fire him, dumpster fire, etc)
    fire_is_literal = "fire" in t and not any(p in t for p in ["fire her","fire him","fire them","fire that","fire the ","fire this","dumpster fire","on fire with","fired"])
    emergency = ["emergency","injury","hurt","bleeding","attack","weapon","gun","violence","ambulance","911",
                 "collapsed","unconscious","not breathing","heart attack","seizure","overdose","stabbed","shot",
                 "flood","flooding","gas leak","smoke","sparks","electrical","water leak","burst pipe"]
    if fire_is_literal: emergency.append("fire")
    if any(f" {w} " in f" {t} " or t.startswith(w+" ") or t.endswith(" "+w) or t==w for w in emergency):
        return {"tier":1,"category":"safety","sentiment":"negative","confidence":0.8,"summary":"Possible emergency reported",
                "auto_reply":"If this is an emergency, please call 911 immediately. We have notified the business owner."}
    question_words = ["what time","when do","where is","where are","do you have","is there","how do i","how much","can i","are you open"]
    if any(w in t for w in question_words) or t.endswith("?"):
        return {"tier":4,"category":"inquiry","sentiment":"neutral","confidence":0.7,"summary":"Customer inquiry",
                "auto_reply":"Great question! We've forwarded this to management and someone will get back to you shortly."}
    crit = {"cleanliness":(["dirty","disgusting","filthy","mess","bathroom","gross","unsanitary"],
            "We've flagged this as a cleanliness issue and notified management. Thank you for letting us know."),
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
            "auto_reply":"Thanks so much for reaching out! We've noted your message."}


# --- Owner commands ---
_owner_context = {}
_owner_reply_mode = {}  # biz_id -> message_id (waiting for reply text)

def set_context(bid, mid): _owner_context[bid] = mid

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

    # Check if we're in reply mode
    if bid in _owner_reply_mode:
        msg_id = _owner_reply_mode.pop(bid)
        msg = get_message_by_id(msg_id)
        if msg:
            send_sms(msg["from_number"], raw, from_number=business.get("twilio_number",""))
            return f"Reply sent to {_fmt_phone_short(msg['from_number'])}."
        return "Could not find the original message."

    # Ack
    ack_words = {"OK","GOT IT","DONE","ON IT","ACK","YES"}
    is_thumbs = "\U0001f44d" in raw
    is_reaction_ack = any(cmd.startswith(w) for w in ["LIKED","LOVED","THUMBED UP"])
    if cmd in ack_words or is_thumbs or is_reaction_ack:
        msg = get_message_by_id(_owner_context.get(bid,0)) if bid in _owner_context else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg: return "No active alerts to acknowledge."
        if msg["acknowledged"]: return f"Alert #{msg['id']} already acknowledged."
        mark_acknowledged(msg["id"])
        others = [p for p in get_alert_phones(business) if _normalize_phone(p)[-10:] != _normalize_phone(sender_phone)[-10:]]
        short = _fmt_phone_short(sender_phone) if sender_phone else "someone"
        for p in others: send_sms(p, f"\u2705 Alert #{msg['id']} acknowledged by {short}.", from_number=business.get("twilio_number",""))
        return f"\u2705 Alert #{msg['id']} acknowledged."

    if cmd == "HELP":
        return ("Commands:\nDETAILS \u2014 View latest alert\nOK \u2014 Acknowledge alert\n"
                "REPLY \u2014 Reply to customer\nLIST \u2014 Flagged issues\nLIST ALL \u2014 All messages\n"
                "STATUS \u2014 Alert status\nALERTS ALL \u2014 Get Tier 2+3 alerts\nALERTS CRITICAL \u2014 Tier 2 only\n"
                "MUTE 2H \u2014 Silence alerts\nPAUSE / RESUME\n"
                "DIGEST DAILY / WEEKLY\nHELP \u2014 This message")

    if cmd == "REPLY":
        msg = get_message_by_id(_owner_context.get(bid,0)) if bid in _owner_context else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg: return "No active message to reply to."
        _owner_reply_mode[bid] = msg["id"]
        return f"What would you like to reply to {_fmt_phone_short(msg['from_number'])}? Type your message now."

    if cmd == "DETAILS":
        msg = get_message_by_id(_owner_context.get(bid,0)) if bid in _owner_context else None
        if not msg: msg = get_latest_unacked(bid)
        if not msg: return "No active alerts."
        set_context(bid, msg["id"])
        ack = "\u2705 Acknowledged" if msg["acknowledged"] else "\u23f3 Pending"
        return (f"Alert #{msg['id']} \u2014 {ack}\nTime: {_fmt_ts(msg['created_at'])}\n"
                f"Category: {msg['category']}\nFrom: {_fmt_phone_short(msg['from_number'])}\n"
                f"Message: \"{msg['message_text']}\"\nReply OK to acknowledge or REPLY to respond.")

    if cmd == "LIST ALL":
        msgs = get_recent_all(bid, 5)
        if not msgs: return "No messages yet."
        icons = {1:"\U0001f6a8",2:"\u26a0\ufe0f",3:"\U0001f614",4:"\U0001f4ac"}
        lines = ["Last 5 messages:\n"]
        for m in msgs: lines.append(f"{icons.get(m['tier'],'\U0001f4ac')} #{m['id']} \u2014 {m['summary']} ({_fmt_ts(m['created_at'])}){' \u2705' if m['acknowledged'] else ''}")
        return "\n".join(lines)

    if cmd == "LIST":
        msgs = get_recent_flagged(bid, 5)
        if not msgs: return "No flagged issues."
        lines = ["Last 5 flagged:\n"]
        for m in msgs: lines.append(f"{'\u2705' if m['acknowledged'] else '\u26a0\ufe0f'} #{m['id']} \u2014 {m['summary']} ({_fmt_ts(m['created_at'])})")
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
        t3 = "on" if business.get("alert_tier3") else "off"
        return f"\U0001f514 Alerts ON for {name}.\nTier 3 alerts: {t3} (text ALERTS ALL or ALERTS CRITICAL)"

    if cmd == "ALERTS ALL": set_alert_tier3(bid, True); return "You'll now receive Tier 3 (reputation risk) alerts too."
    if cmd == "ALERTS CRITICAL": set_alert_tier3(bid, False); return "Tier 3 alerts off. You'll only get critical (Tier 2) and emergency alerts."
    if cmd == "PAUSE": set_paused(bid, True); return "\U0001f4f4 Alerts PAUSED. Reply RESUME to turn back on."
    if cmd == "RESUME": set_paused(bid, False); set_muted_until(bid, None); return "\U0001f514 Alerts resumed."
    if cmd == "DIGEST DAILY": set_digest_freq(bid, "daily"); return "\U0001f4e7 Digest set to daily."
    if cmd == "DIGEST WEEKLY": set_digest_freq(bid, "weekly"); return "\U0001f4e7 Digest set to weekly."

    if cmd.startswith("MUTE"):
        m = re.match(r"MUTE\s+(\d+)\s*(H|HR|HRS|HOUR|HOURS|M|MIN|MINS|MINUTE|MINUTES)?", cmd)
        if not m: set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=1)); return "\U0001f507 Muted 1 hour. Reply RESUME to unmute."
        amt = int(m.group(1)); unit = (m.group(2) or "H")[0]
        if unit=="M": amt=max(1,min(1440,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(minutes=amt)); return f"\U0001f507 Muted {amt}m."
        else: amt=max(1,min(72,amt)); set_muted_until(bid, datetime.now(timezone.utc)+timedelta(hours=amt)); return f"\U0001f507 Muted {amt}h."

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
import os as _os2
if _os2.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
elif _os2.path.exists("api/static"):
    app.mount("/static", StaticFiles(directory="api/static"), name="static")
RATE_LIMIT_MAX = 5; RATE_LIMIT_WINDOW = 10; _initialized = False
_ENV_OWNER = os.getenv("OWNER_PHONE_NUMBER",""); _ENV_TWILIO = os.getenv("TWILIO_PHONE_NUMBER","")
_ENV_NAME = os.getenv("BUSINESS_NAME","MyBusiness"); _ADMIN_KEY = os.getenv("ADMIN_KEY","changeme")

def _ensure_init():
    global _initialized
    if _initialized: return
    init_db(); init_classifier(); init_sms()
    if _ENV_OWNER and _ENV_TWILIO:
        if create_business("default", _ENV_NAME, _ENV_OWNER, _ENV_TWILIO): logger.info(f"Registered '{_ENV_NAME}'")
    _initialized = True

WELCOME_MSG = """Welcome to {name} on Hotline!

Your customers can now text {twilio} with feedback.

Commands:
DETAILS \u2014 View alert
OK \u2014 Acknowledge
REPLY \u2014 Reply to customer
LIST \u2014 Flagged issues
LIST ALL \u2014 All messages
STATUS \u2014 Alert status
ALERTS ALL \u2014 Include reputation alerts
MUTE 2H \u2014 Silence alerts
PAUSE / RESUME
HELP \u2014 Full command list

Emergencies always get through."""


# --- Routes ---
@app.get("/")
def root():
    _ensure_init(); return Response(content=DEMO_HTML, media_type="text/html")

@app.get("/health")
def health(): _ensure_init(); return {"status":"ok"}

@app.post("/digest")
def digest_endpoint(freq: str = Query("weekly")): _ensure_init(); return {"digests_sent": send_all_digests(force_freq=freq)}

# Admin routes
@app.get("/admin/add")
def admin_add(key:str=Query(...),name:str=Query(...),owner:str=Query(...),twilio:str=Query(...),biz_id:str=Query(""),extra_phones:str=Query(""),email:str=Query(""),website:str=Query("")):
    _ensure_init()
    if key!=_ADMIN_KEY: return {"error":"Invalid key"}
    owner,twilio,name = owner.strip(),twilio.strip(),name.strip()
    if not owner.startswith("+") or not twilio.startswith("+"): return {"error":"Phone numbers must start with +"}
    if not biz_id: biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    ok = create_business(biz_id,name,owner,twilio,extra_phones=extra_phones,email=email,website_url=website)
    if not ok: return {"error":"Already exists or number in use"}
    msg = WELCOME_MSG.format(name=name,twilio=twilio)
    for p in get_alert_phones({"owner_phone":owner,"alert_phones":f"{owner},{extra_phones}" if extra_phones else owner}): send_sms(p,msg,from_number=twilio)
    return {"success":True,"business_id":biz_id,"name":name}

@app.get("/admin/welcome")
def admin_welcome(key:str=Query(...),biz_id:str=Query(...)):
    _ensure_init()
    if key!=_ADMIN_KEY: return {"error":"Invalid key"}
    with get_db() as c: biz = _fetchone(c, _q("SELECT * FROM businesses WHERE id=?"), (biz_id,))
    if not biz: return {"error":"Not found"}
    msg = WELCOME_MSG.format(name=biz["name"],twilio=biz["twilio_number"])
    for p in get_alert_phones(biz): send_sms(p,msg,from_number=biz["twilio_number"])
    return {"success":True}

@app.get("/admin/list")
def admin_list(key:str=Query(...)):
    _ensure_init()
    if key!=_ADMIN_KEY: return {"error":"Invalid key"}
    return {"businesses":[{"id":b["id"],"name":b["name"],"owner":b["owner_phone"],"twilio":b["twilio_number"]} for b in get_all_businesses()]}

@app.get("/admin/remove")
def admin_remove(key:str=Query(...),biz_id:str=Query(...)):
    _ensure_init()
    if key!=_ADMIN_KEY: return {"error":"Invalid key"}
    with get_db() as c: _execute(c,_q("DELETE FROM businesses WHERE id=?"), (biz_id,))
    return {"success":True}

@app.get("/admin")
def admin_ui(key:str=Query("")):
    _ensure_init()
    if key!=_ADMIN_KEY:
        return Response(content='<!DOCTYPE html><html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f8f8f6"><div style="text-align:center"><h2>Hotline Admin</h2><form style="display:flex;gap:8px" onsubmit="location.href=\'/admin?key=\'+document.getElementById(\'k\').value;return false"><input id="k" type="password" placeholder="Admin key" style="padding:10px 14px;border:1px solid #ddd;border-radius:6px;font-size:15px;width:220px"><button style="padding:10px 20px;background:#ea580c;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer">Enter</button></form></div></body></html>', media_type="text/html")
    businesses = get_all_businesses()
    rows = ""
    for b in businesses:
        s = get_stats(b["id"])
        rows += f'<tr><td style="padding:12px 16px;font-weight:600">{b["name"]}</td><td style="padding:12px 16px;font-family:monospace;font-size:13px">{b["twilio_number"]}</td><td style="padding:12px 16px;text-align:center">{s["total_messages"]}</td><td style="padding:12px 16px;text-align:center">{s["flagged_issues"]}</td><td style="padding:12px 16px"><a href="/admin/welcome?key={key}&biz_id={b["id"]}" style="color:#2563eb;font-size:13px;margin-right:12px">Resend</a><a href="#" onclick="if(confirm(\'Remove?\'))location.href=\'/admin/remove?key={key}&biz_id={b["id"]}\';return false" style="color:#dc2626;font-size:13px">Remove</a></td></tr>'
    if not rows: rows = '<tr><td colspan="5" style="padding:24px;text-align:center;color:#999">No businesses yet.</td></tr>'
    return Response(content=f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hotline Admin</title></head><body style="font-family:system-ui;margin:0;padding:24px;background:#f8f8f6"><div style="max-width:900px;margin:0 auto"><h1 style="font-size:24px;margin:0 0 24px">Hotline Admin</h1><div style="background:#fff;border:1px solid #e0e0dc;border-radius:10px;overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr style="background:#f5f5f0;border-bottom:1px solid #e0e0dc"><th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Business</th><th style="padding:10px 16px;text-align:left;font-size:12px;text-transform:uppercase;color:#888">Number</th><th style="padding:10px 16px;text-align:center;font-size:12px;text-transform:uppercase;color:#888">Msgs</th><th style="padding:10px 16px;text-align:center;font-size:12px;text-transform:uppercase;color:#888">Flagged</th><th style="padding:10px 16px;font-size:12px;text-transform:uppercase;color:#888">Actions</th></tr></thead><tbody>{rows}</tbody></table></div></div></body></html>', media_type="text/html")


# --- SMS Incoming ---
@app.post("/sms/incoming")
async def incoming_sms(From:str=Form(...), Body:str=Form(...), To:str=Form("")):
    _ensure_init()
    sender, body, twilio_num = From.strip(), Body.strip(), To.strip()
    logger.info(f"SMS from {sender}: {body[:80]}")

    owner_biz = get_business_by_owner(sender)
    if owner_biz:
        resp = handle_owner_command(body, owner_biz, sender_phone=sender)
        if not resp: return _twiml("")
        return _twiml(resp)

    biz = get_business_by_twilio(twilio_num)
    if not biz: return _twiml("Thanks so much for reaching out! We've noted your message.")

    website_info = biz.get("website_info","")
    c = classify_message(body, website_info=website_info)
    msg_id = store_message(biz["id"], sender, body, c)
    tier, conf, summary = c["tier"], c["confidence"], c.get("summary","Issue reported")
    cat = c.get("category","other")

    alert_phones = get_alert_phones(biz)
    should_alert_t3 = biz.get("alert_tier3") and tier == 3 and conf > 0.5
    should_alert = tier == 1 or (tier == 2 and conf > 0.7) or should_alert_t3

    if alert_phones and should_alert and not (is_alerts_silenced(biz) and tier != 1):
        if get_recent_alert_count(biz["id"], RATE_LIMIT_WINDOW) < RATE_LIMIT_MAX:
            if tier == 1: alert = "\U0001f6a8 URGENT: Possible emergency reported\nReply: DETAILS"
            elif cat == "inquiry": alert = f"\u2753 Customer question: {summary}\nReply REPLY to respond"
            else: alert = f"\u26a0\ufe0f Issue reported: {summary}\nReply OK to acknowledge"
            for p in alert_phones: send_sms(p, alert, from_number=biz["twilio_number"])
            mark_alerted(msg_id); log_alert(msg_id, biz["id"], f"tier_{tier}")
            set_context(biz["id"], msg_id)

    return _twiml(c.get("auto_reply","Thanks so much for reaching out! We've noted your message."))

def _twiml(msg):
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response><Message>'+msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")+'</Message></Response>', media_type="application/xml")



# --- Shared nav + styles ---
NAV_CSS = """
.nav{display:flex;justify-content:space-between;align-items:center;padding:12px 24px;max-width:960px;margin:0 auto}
.nav .logo{font-size:13px;font-weight:700;letter-spacing:0.15em;text-transform:uppercase;color:#ea580c;text-decoration:none}
.nav .logo span{background:#ea580c;color:#fff;padding:2px 6px;border-radius:3px;margin-right:4px}
.nav-links{display:flex;gap:20px;align-items:center}
.nav-links a{font-size:14px;color:#666;text-decoration:none;font-weight:500}
.nav-links a:hover{color:#1a1a1a}
.nav-links .signup-btn{background:#ea580c;color:#fff;padding:8px 16px;border-radius:6px;font-weight:600}
.nav-links .signup-btn:hover{background:#dc2626;color:#fff}
.hamburger{display:none;cursor:pointer;font-size:22px;color:#666}
@media(max-width:600px){.nav-links{display:none;position:absolute;top:48px;right:16px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:12px;flex-direction:column;gap:10px;box-shadow:0 4px 12px rgba(0,0,0,0.08);z-index:10}.nav-links.open{display:flex}.hamburger{display:block}}
"""

NAV_HTML = """<nav class="nav"><a href="/" class="logo"><span>H</span> HOTLINE</a>
<div class="hamburger" onclick="document.querySelector('.nav-links').classList.toggle('open')">&#9776;</div>
<div class="nav-links"><a href="/">Demo</a><a href="/industries">Who We Support</a><a href="/signup" class="signup-btn">Sign Up</a></div></nav>"""


# --- Demo page (homepage) ---
DEMO_PROMPT = """You are simulating a business's customer feedback SMS system for a live demo called Hotline.

TIER DEFINITIONS:
- Tier 1: Emergency (Red Alert) — Physical danger to people or property. Literal fire, flooding, gas leak, smoke, sparks, electrical hazard, injury, someone hurt/collapsed/unconscious, violence, threats, weapons, water damage in progress. Flooding IS always Tier 1 (slip hazards, electrical risk, property damage).
  NOT Tier 1: Figurative language. "fire her", "dumpster fire", "killing it", "blowing up", "on fire today", "she got fired" — complaints or compliments, never emergencies.
- Tier 2: Business-Critical — Operations broken. No staff, equipment broken, supply outages (no toilet paper, soap), extreme waits (20+ min), access blocked (can't get in door), health/hygiene issues.
- Tier 3: Reputation Risk — Customer unhappy, no operational failure. Rude staff, music too loud, temperature, disappointment.
- Tier 4: Routine — Positive feedback, compliments, questions, neutral.

Categories: cleanliness, staffing, equipment, wait_time, safety, supply, inquiry, other

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
- "Music is too loud" = Tier 3. Acknowledge, don't promise change.
- "Can't get in the front door" = Tier 2 (access blocked).
- "What time do you close?" = Tier 4, inquiry. Don't answer.
- "You should fire her" = Tier 3, staffing complaint. NOT emergency.
- "Bathroom is flooding!" = Tier 1, safety. ALWAYS emergency.
- "Out of toilet paper" = Tier 2, supply.

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
            resp = _ai_client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=300, system=DEMO_PROMPT, messages=[{"role":"user","content":user_msg}])
            raw = resp.content[0].text.strip()
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
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:#333;color:#fff;padding:10px 20px;border-radius:8px;font-size:13px;opacity:0;transition:all 0.4s;pointer-events:none;z-index:100;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
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
.features{display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:520px;margin:0 auto 32px;padding:0 20px}.feat{background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,0.04)}.feat strong{font-size:14px;display:block;margin-bottom:2px;color:#1a1a1a}.feat p{font-size:13px;color:#888;margin:0;line-height:1.4}
footer{text-align:center;padding:32px 24px;color:#aaa;font-size:13px;border-top:1px solid #e0e0dc}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:spin 0.6s linear infinite;vertical-align:middle;margin-right:4px}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:700px){.phones{flex-direction:column;align-items:center}.device{width:100%;max-width:360px}.features{grid-template-columns:1fr}}
.howitworks{max-width:640px;margin:0 auto;padding:0 20px 28px}
.hiw-steps{display:flex;flex-direction:column;gap:14px}
.hiw-step{display:flex;align-items:flex-start;gap:14px;background:#fff;border:1px solid #e0e0dc;border-radius:10px;padding:16px 18px}
.hiw-num{width:28px;height:28px;border-radius:50%;background:#fff7ed;color:#ea580c;font-weight:700;font-size:13px;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}
.hiw-step strong{font-size:14px;display:block;margin-bottom:2px}
.hiw-step p{font-size:13px;color:#888;margin:0;line-height:1.4}
.sign-graphic{text-align:center;padding:0 20px 24px;max-width:400px;margin:0 auto}
.sign-graphic img{width:100%;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.10)}
.sign-caption{font-size:12px;color:#aaa;margin-top:10px}
</style></head><body>
""" + NAV_HTML + """
<div class="top">
<h1>The bathroom is flooding.<br><em>You're across town.</em></h1>
<p class="sub">One text from a customer. One alert to your phone. Crisis handled before anyone leaves a review.</p>
<p style="font-size:13px;color:#aaa;margin-bottom:4px">No app. No software. No training required.</p>
</div>
<div class="howitworks"><div class="hiw-steps">
<div class="hiw-step"><div class="hiw-num">1</div><div><strong>Display your number</strong><p>Post it in your business \u2014 bathroom, counter, receipt. Customers scan the QR code or text directly when something's wrong.</p></div></div>
<div class="hiw-step"><div class="hiw-num">2</div><div><strong>AI reads every message</strong><p>Emergencies, operational issues, complaints, and compliments \u2014 each one triaged instantly.</p></div></div>
<div class="hiw-step"><div class="hiw-num">3</div><div><strong>You get alerted by text</strong><p>Only for things that matter. Reply OK to acknowledge, REPLY to respond directly to the customer.</p></div></div>
</div></div>
<div class="sign-graphic">
<img src="/static/sign.png" alt="Hotline sign mounted in a bathroom" />
<p class="sign-caption">We'll help you set up your sign. Customers scan, you get alerted.</p>
</div>
<div class="examples"><p>See it in action \u2014 try a real scenario:</p><div class="ex-row">
<div class="ex" onclick="tryEx(this)">Bathroom is disgusting</div>
<div class="ex" onclick="tryEx(this)">No one is at the front desk</div>
<div class="ex" onclick="tryEx(this)">Great coffee today!</div>
<div class="ex" onclick="tryEx(this)">Bathroom is flooding!</div>
<div class="ex" onclick="tryEx(this)">We're out of toilet paper!</div>
<div class="ex" onclick="tryEx(this)">The music is way too loud</div>
<div class="ex" onclick="tryEx(this)">What time do you close?</div>
<div class="ex" onclick="tryEx(this)">Terrible service, very rude</div>
<div class="ex" onclick="tryEx(this)">We can't get in the front door</div>
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
<div class="pref-bar"><span class="pref-label">Owner notification preference:</span>
<button class="filter-btn" id="filt-crit" onclick="setFilter('critical')">Critical only</button>
<button class="filter-btn" id="filt-all" onclick="setFilter('all')">All messages</button>
</div>
<div class="toast" id="toast">Owner sets their notification preferences via text</div>
<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>
<div class="features">
<div class="feat"><strong>Only alerts what matters</strong><p>AI filters the noise. You hear about flooding, not feedback about the music.</p></div>
<div class="feat"><strong>Manage everything by text</strong><p>DETAILS, OK, REPLY, MUTE \u2014 no app, no dashboard, no new habits.</p></div>
</div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses</footer>
<script>
let lastData=null,acked=false,replyMode=false,history=[],demoCount=0,maxDemo=10,filterMode='critical';
const mc=document.getElementById('m-cust'),mo=document.getElementById('m-owner');
function addB(c,cls,label,text,tier){const d=document.createElement('div');d.className='bubble '+cls;if(tier)d.setAttribute('data-tier',tier);let h='';if(label)h+='<div class="lbl">'+label+'</div>';h+=text;d.innerHTML=h;c.appendChild(d);c.scrollTop=c.scrollHeight;applyFilter();return d}
function tryEx(el){document.getElementById('cust-input').value=el.textContent;sendDemo()}
function showOwnerInput(){document.getElementById('owner-cmds').style.display='flex';document.getElementById('owner-input').style.display='block'}
function hideOwnerInput(){document.getElementById('owner-cmds').style.display='none';document.getElementById('owner-input').style.display='none'}
function setFilter(mode){filterMode=mode;document.getElementById('filt-all').className='filter-btn'+(mode==='all'?' active':'');document.getElementById('filt-crit').className='filter-btn'+(mode==='critical'?' active':'');applyFilter();showToast('Owner sets their notification preferences via text')}
function applyFilter(){mo.querySelectorAll('.bubble[data-tier]').forEach(function(b){var t=parseInt(b.getAttribute('data-tier'));b.style.display=(filterMode==='all'||t<=2)?'':'none'})}
function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2500)}
(function(){document.getElementById('filt-crit').classList.add('active');showToast('Owner sets their notification preferences via text')})();function ownerCmd(raw){const cmd=(raw||'').trim().toUpperCase();const inp=document.getElementById('owner-inp');inp.value='';if(!cmd)return;
if(replyMode){replyMode=false;addB(mo,'cmd','',raw.trim());addB(mo,'resp','','Reply sent to (555) 867-5309.');addB(mc,'in','Reply from owner',raw.trim());inp.placeholder='Type a command...';return}
addB(mo,'cmd','',raw.trim());
if(!lastData){addB(mo,'resp','','No active alerts.');return}
if(cmd==='DETAILS'){const d=lastData;const now=new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});const ackLabel=acked?'\\u2705 Acknowledged':'\\u23f3 Pending';addB(mo,'resp','','Alert \\u2014 '+ackLabel+'\\nTime: '+now+'\\nCategory: '+d.category.replace('_',' ')+'\\nFrom: (555) 867-5309\\nMessage: "'+d.original_message+'"\\nReply OK or REPLY to respond.');return}
if(cmd==='REPLY'){replyMode=true;addB(mo,'resp','','What would you like to reply to (555) 867-5309? Type your message now.');inp.placeholder='Type your reply...';inp.focus();return}
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
<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#9749;</span><h3 style="display:inline">Restaurants & Cafes</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"There's no one at the register and the bathroom is flooded."</em> You're across town. Without Hotline, this becomes a 1-star review. With it, you know in seconds.<div class="tag-row"><span class="tag-sm">flooding</span><span class="tag-sm">unstaffed</span><span class="tag-sm">food safety</span><span class="tag-sm">walk-outs</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128722;</span><h3 style="display:inline">Retail Stores</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"I've been waiting 15 minutes and there's nobody on the floor."</em> That customer is about to walk out and never come back. Hotline puts that message on your phone immediately.<div class="tag-row"><span class="tag-sm">no staff</span><span class="tag-sm">theft risk</span><span class="tag-sm">customer walkouts</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127947;</span><h3 style="display:inline">Gyms & Fitness Studios</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The cable machine snapped and almost hit someone."</em> Equipment failure is a liability nightmare. Hotline makes sure you hear about it before your insurance company does.<div class="tag-row"><span class="tag-sm">equipment failure</span><span class="tag-sm">injury risk</span><span class="tag-sm">locker room issues</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#9986;</span><h3 style="display:inline">Salons & Barbershops</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"I waited 40 minutes past my appointment and just left."</em> You lost the appointment revenue, the rebooking, and the referrals. Hotline catches it while you can still save the relationship.<div class="tag-row"><span class="tag-sm">long waits</span><span class="tag-sm">no-show staff</span><span class="tag-sm">unhappy clients</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#129499;</span><h3 style="display:inline">Laundromats</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"Machine #4 is leaking water all over the floor."</em> You might not visit for hours. By then, it's water damage and a slip-and-fall. Hotline tells you when it starts, not after.<div class="tag-row"><span class="tag-sm">equipment leaks</span><span class="tag-sm">safety hazards</span><span class="tag-sm">out of supplies</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128295;</span><h3 style="display:inline">Auto Repair Shops</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"I was told 2 hours. It's been 5. Nobody's told me anything."</em> That customer is writing a review right now. Hotline gives you the chance to respond before they hit publish.<div class="tag-row"><span class="tag-sm">broken promises</span><span class="tag-sm">communication gaps</span><span class="tag-sm">angry customers</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127976;</span><h3 style="display:inline">Hotels & Airbnbs</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The room smells like smoke and the AC doesn't work."</em> That guest is about to request a refund and leave a review that kills your next 50 bookings. Hotline gets you there first.<div class="tag-row"><span class="tag-sm">room complaints</span><span class="tag-sm">refund risk</span><span class="tag-sm">review prevention</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#127973;</span><h3 style="display:inline">Medical & Dental Offices</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"I've been in the waiting room for over an hour and nobody's updated me."</em> Patients don't complain to your face. They leave quietly and post online. Hotline gives them a private channel to reach you.<div class="tag-row"><span class="tag-sm">wait times</span><span class="tag-sm">front desk gaps</span><span class="tag-sm">patient retention</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128187;</span><h3 style="display:inline">Coworking Spaces</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The internet has been down for 30 minutes and nobody seems to know."</em> Members pay for productivity. When the space fails, they leave. Hotline keeps you ahead of it.<div class="tag-row"><span class="tag-sm">internet outages</span><span class="tag-sm">facilities</span><span class="tag-sm">member retention</span></div></div></div>

<div class="card" onclick="this.classList.toggle('open')"><div class="card-top"><div><span class="icon">&#128663;</span><h3 style="display:inline">Car Washes</h3></div><span class="arrow">&#9654;</span></div>
<div class="card-body"><em>"The dryer scratched my paint."</em> That's a damage claim waiting to happen. Hotline makes sure you know about it while the customer is still on-site, not when the lawyer calls.<div class="tag-row"><span class="tag-sm">damage claims</span><span class="tag-sm">equipment issues</span><span class="tag-sm">quality complaints</span></div></div></div>
</div>

<div class="cta"><a href="/signup">Get Hotline for your business &rarr;</a></div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses</footer>
</body></html>"""

@app.get("/industries")
def industries_page(): _ensure_init(); return Response(content=INDUSTRIES_HTML, media_type="text/html")


# --- Signup page ---
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
@media(max-width:500px){.steps{grid-template-columns:1fr}}
</style></head><body>
""" + NAV_HTML + """
<div class="wrap">
<h1>Get your business number</h1>
<p class="sub">No app. No software. No training required. Get your number in 30 seconds and start receiving alerts immediately.</p>
<div class="card">
<div class="trial">14-day free trial &middot; No credit card required</div>
<div class="result" id="result"></div>
<label>Business name</label><input type="text" id="f-name" placeholder="Joe's Coffee">
<label>Your cell phone</label><input type="tel" id="f-phone" placeholder="(727) 555-1234">
<label>Partner or manager phone (optional)</label><input type="tel" id="f-phone2" placeholder="(727) 555-5678">
<label>Email (for digest reports)</label><input type="email" id="f-email" placeholder="you@example.com">
<label>Business website (optional)</label><input type="url" id="f-url" placeholder="https://joescoffee.com">
<label>Preferred area code (optional)</label><input type="text" id="f-area" placeholder="727" maxlength="3" style="width:100px">
<button class="btn" id="f-btn" onclick="signup()">Get my number &rarr;</button>
</div>
<div class="steps">
<div class="step"><div class="step-num">1</div><h3>Sign up</h3><p>Get your unique number in seconds</p></div>
<div class="step"><div class="step-num">2</div><h3>Display it</h3><p>Post a sign, sticker, or add to receipts</p></div>
<div class="step"><div class="step-num">3</div><h3>Get alerts</h3><p>AI reads every text and alerts you instantly</p></div>
</div>
</div>
<footer>Hotline &middot; AI-powered customer alerts for small businesses</footer>
<script>
async function signup(){const name=document.getElementById('f-name').value.trim();let phone=document.getElementById('f-phone').value.trim().replace(/[\\s\\-\\(\\)]/g,'');let phone2=document.getElementById('f-phone2').value.trim().replace(/[\\s\\-\\(\\)]/g,'');const email=document.getElementById('f-email').value.trim();const url=document.getElementById('f-url').value.trim();const area=document.getElementById('f-area').value.trim();const res=document.getElementById('result');const btn=document.getElementById('f-btn');
if(!phone.startsWith('+')){if(phone.startsWith('1')&&phone.length===11)phone='+'+phone;else if(phone.length===10)phone='+1'+phone;else{res.className='result err';res.style.display='block';res.textContent='Please enter a valid US phone number.';return}}
if(phone2&&!phone2.startsWith('+')){if(phone2.startsWith('1')&&phone2.length===11)phone2='+'+phone2;else if(phone2.length===10)phone2='+1'+phone2}
if(!name){res.className='result err';res.style.display='block';res.textContent='Please enter your business name.';return}
btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Setting up...';res.style.display='none';
try{const r=await fetch('/signup/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,phone2,email,website_url:url,area_code:area})});const d=await r.json();
if(d.success){res.className='result ok';res.innerHTML='<strong>You are live!</strong><br><br>Your number: <strong>'+d.twilio_number+'</strong><br><br>Welcome text sent. Display this number in your business and customers can start texting.';res.style.display='block';btn.textContent='Done!'}
else{res.className='result err';res.textContent=d.error||'Something went wrong.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my number &rarr;'}}
catch(e){res.className='result err';res.textContent='Connection error.';res.style.display='block';btn.disabled=false;btn.innerHTML='Get my number &rarr;'}}
</script></body></html>"""

@app.get("/signup")
def signup_page(): _ensure_init(); return Response(content=SIGNUP_HTML, media_type="text/html")

@app.post("/signup/create")
async def signup_create(request_data:dict=None):
    _ensure_init()
    if not request_data: return {"error":"Missing data"}
    name = (request_data.get("name") or "").strip()
    phone = (request_data.get("phone") or "").strip()
    phone2 = (request_data.get("phone2") or "").strip()
    email = (request_data.get("email") or "").strip()
    website_url = (request_data.get("website_url") or "").strip()
    area_code = (request_data.get("area_code") or "").strip()
    if not name: return {"error":"Business name required"}
    if not phone or not phone.startswith("+"): return {"error":"Valid phone with country code required"}
    biz_id = re.sub(r"[^a-z0-9\-]","",name.lower().replace(" ","-").replace("'",""))[:30]
    with get_db() as c:
        if _fetchone(c,_q("SELECT id FROM businesses WHERE id=?"), (biz_id,)):
            biz_id = biz_id[:25]+"-"+datetime.now(timezone.utc).strftime("%H%M%S")
    twilio_number = buy_twilio_number(area_code=area_code, webhook_url="https://sms-alerts.vercel.app/sms/incoming")
    if not twilio_number: return {"error":"Could not provision number. Try again."}
    extra = phone2 if phone2 and phone2.startswith("+") else ""
    ok = create_business(biz_id, name, phone, twilio_number, extra_phones=extra, email=email, website_url=website_url)
    if not ok: return {"error":"Could not create business."}
    msg = WELCOME_MSG.format(name=name, twilio=twilio_number)
    send_sms(phone, msg, from_number=twilio_number)
    if extra: send_sms(extra, msg, from_number=twilio_number)
    logger.info(f"Signup: {name} ({biz_id}) -> {twilio_number}")
    return {"success":True,"business_id":biz_id,"name":name,"owner_phone":phone,"twilio_number":twilio_number}
