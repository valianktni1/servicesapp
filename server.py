"""Workshop Desk HTTP service and tenant database."""
import argparse
import getpass
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("WORKSHOP_DB", "/data/workshop.sqlite3"))
ORIGIN = os.environ.get("PUBLIC_ORIGIN", "https://servicesapp.ivorydigital.uk").rstrip("/")
ADMIN_EMAIL = "admin@ivorydigital.uk"
DAY = 86400
MAX_STATE = 8 * 1024 * 1024
HASH_SLOTS = threading.BoundedSemaphore(2)
LONDON = ZoneInfo("Europe/London")
EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?")
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
SERVICES = {"MOT test": 60, "Interim service": 90, "Full service": 120,
            "MOT + full service": 180, "Diagnostics": 60, "Repair enquiry": 60}
CLOSED = {"Cancelled", "No-show"}
BANK_HOLIDAYS = set((
    "2026-01-01 2026-04-03 2026-04-06 2026-05-04 2026-05-25 2026-08-31 2026-12-25 2026-12-28 "
    "2027-01-01 2027-03-26 2027-03-29 2027-05-03 2027-05-31 2027-08-30 2027-12-27 2027-12-28 "
    "2028-01-03 2028-04-14 2028-04-17 2028-05-01 2028-05-29 2028-08-28 2028-12-25 2028-12-26"
).split())
DEFAULT_HOURS = [None, *[["08:30", "17:30"]] * 5, None]
SCHEMA = """
CREATE TABLE IF NOT EXISTS garages (
 slug TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
 expires INTEGER, trial_days INTEGER NOT NULL DEFAULT 14, suspended INTEGER NOT NULL DEFAULT 0,
 joined INTEGER, revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
 email TEXT PRIMARY KEY, password TEXT NOT NULL, garage TEXT REFERENCES garages(slug)
);
CREATE TABLE IF NOT EXISTS sessions (
 token TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
 token TEXT PRIMARY KEY, garage TEXT NOT NULL UNIQUE REFERENCES garages(slug), expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, garage TEXT NOT NULL REFERENCES garages(slug), amount INTEGER NOT NULL,
 paid_on TEXT NOT NULL, through TEXT NOT NULL, reference TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
 token TEXT PRIMARY KEY, garage TEXT NOT NULL REFERENCES garages(slug),
 job TEXT NOT NULL, version INTEGER NOT NULL, expires INTEGER NOT NULL, UNIQUE(garage, job, version)
);
CREATE TABLE IF NOT EXISTS attempts (
 bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL
);
"""

class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


@contextmanager
def database(write=False):
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialise():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    with HASH_SLOTS:
        hashed = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                                maxmem=192 * 1024 * 1024)
    return salt.hex() + ":" + hashed.hex()


def password_ok(password):
    return isinstance(password, str) and 8 <= len(password) <= 128 and any(
        not ch.isalnum() and not ch.isspace() for ch in password
    )


def text_field(data, key, limit=200, optional=False):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise Problem(400, "Check the " + key.replace("_", " ") + " field.")
    return value.strip()


def email_field(data):
    email = text_field(data, "email").casefold()
    if not EMAIL.fullmatch(email):
        raise Problem(400, "Enter a valid email address.")
    return email


def date_field(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        result = date.fromisoformat(value)
        if not 2000 <= result.year <= 2100:
            raise ValueError()
        return result
    except ValueError:
        raise Problem(400, "Enter a valid date.") from None


def end_of_day(value):
    return int(datetime.combine(date_field(value) + timedelta(days=1), datetime.min.time(), LONDON).timestamp())


def access(garage):
    if garage["suspended"]:
        return "Suspended"
    if not garage["joined"]:
        return "Awaiting setup"
    if garage["expires"] is not None and garage["expires"] <= time.time():
        return "Expired"
    if garage["mode"] == "paid" and garage["expires"] is None:
        return "Payment required"
    return "Active"


def row_for(conn, slug):
    row = conn.execute("SELECT * FROM garages WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise Problem(404, "Garage not found.")
    return row


def workspace(garage):
    return {**json.loads(garage["state"]), "name": garage["name"], "slug": garage["slug"], "revision": garage["revision"]}


def store(conn, garage, model):
    model.pop("name", None)
    model.pop("revision", None)
    model.pop("slug", None)
    conn.execute("UPDATE garages SET state=?,revision=revision+1 WHERE slug=?",
                 (json.dumps(model, allow_nan=False), garage["slug"]))


def invite(conn, slug):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT OR REPLACE INTO invites VALUES(?,?,?)",
                 (digest(token), slug, int(time.time()) + 7 * DAY))
    return ORIGIN + "/setup#" + token


def payment_amount(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount <= 100000 or amount != amount.quantize(Decimal(".01")):
            raise InvalidOperation()
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise Problem(400, "Enter a valid payment amount in pounds.") from None


def public_garage(conn, slug):
    garage = row_for(conn, slug)
    if access(garage) != "Active":
        raise Problem(403, "This garage’s online booking is currently unavailable.")
    return garage


def public_prices(model):
    clean = {}
    prices = model.get("prices", {})
    if isinstance(prices, dict):
        for service, price in prices.items():
            if service in SERVICES and isinstance(price, dict) and price.get("mode") in ("Fixed", "From", "Quote required"):
                amount = price.get("amount")
                if type(amount) is int and 0 <= amount <= 99999999 or price["mode"] == "Quote required":
                    clean[service] = {"mode": price["mode"], "amount": amount if type(amount) is int else None}
    return clean


def time_pair(value):
    if isinstance(value, list) and len(value) == 2 and all(isinstance(t, str) and CLOCK.fullmatch(t) for t in value) and value[0] < value[1]:
        return value
    return None


def opening(model, day):
    key = day.isoformat()
    overrides = model.get("overrides", {})
    if key in overrides:
        return time_pair(overrides[key])
    if key in BANK_HOLIDAYS or key in model.get("blocks", []) or key[5:] in ("12-25", "12-26"):
        return None
    return time_pair(model.get("hours", DEFAULT_HOURS)[(day.weekday() + 1) % 7])


def minutes(clock):
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


def available(model, day, service):
    if service not in SERVICES:
        raise Problem(400, "Choose a valid service.")
    now = datetime.now(LONDON)
    hours = opening(model, day)
    if not hours or not now.date() <= day <= now.date() + timedelta(days=90):
        return {}
    length = SERVICES[service]
    bays = ["MOT bay"] if service.startswith("MOT") else ["Workshop 1", "Workshop 2"]
    slots = {}
    for start in range(minutes(hours[0]), minutes(hours[1]) - length + 1, 30):
        clock = f"{start // 60:02}:{start % 60:02}"
        if day == now.date() and clock <= now.strftime("%H:%M"):
            continue
        for bay in bays:
            if not any(job["date"] == day.isoformat() and job["bay"] == bay and job["status"] not in CLOSED
                       and minutes(job["time"]) < start + length and start < minutes(job["time"]) + job["length"]
                       for job in model.get("jobs", [])):
                slots[clock] = bay
                break
    return slots


def first_booking_date(model):
    today = datetime.now(LONDON).date()
    for offset in range(91):
        day = today + timedelta(days=offset)
        if opening(model, day):
            return day.isoformat()
    return None


def validate_state(model):
    for key, typ in (("jobs", list), ("blocks", list), ("hours", list), ("overrides", dict),
                     ("expenses", list), ("suppliers", list), ("prices", dict)):
        if not isinstance(model.get(key), typ):
            raise Problem(400, "The garage data format is invalid.")
    if len(model["hours"]) != 7:
        raise Problem(400, "Set opening hours for all seven days.")
    seen = set()
    for job in model["jobs"]:
        if not isinstance(job, dict):
            raise Problem(400, "Invalid booking.")
        job_id = job.get("id")
        clock = job.get("time")
        length = job.get("length")
        if not isinstance(job_id, str) or not re.fullmatch(r"WD-[A-Z0-9]+", job_id) or job_id in seen:
            raise Problem(400, "Invalid booking reference.")
        seen.add(job_id)
        date_field(job.get("date"))
        if not isinstance(clock, str) or not CLOCK.fullmatch(clock) or type(length) is not int or not 1 <= length <= 1440:
            raise Problem(400, "Invalid booking time.")
        if job.get("bay") not in ("MOT bay", "Workshop 1", "Workshop 2") or not isinstance(job.get("status"), str):
            raise Problem(400, "Invalid booking status or workshop bay.")
        if any(not isinstance(job.get(key), str) for key in ("reg", "name", "vehicle", "email", "phone", "service")):
            raise Problem(400, "Invalid vehicle or customer details.")


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkshopDesk"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, fmt, *args):
        # Request paths can carry private links. Keep them out of logs.
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def body(self):
        if self.headers.get("Origin") != ORIGIN:
            raise Problem(403, "Open Workshop Desk at its configured web address.")
        if self.headers.get_content_type() != "application/json":
            raise Problem(415, "Expected JSON.")
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 9 or int(length) > MAX_STATE:
            raise Problem(413, "Request is too large.")
        try:
            def invalid_constant(_):
                raise ValueError()
            value = json.loads(self.rfile.read(int(length)), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise Problem(400, "Invalid request.") from None
        if not isinstance(value, dict):
            raise Problem(400, "Invalid request.")
        return value

    def send(self, status, payload, content_type="application/json", cookie=None, nonce=None):
        if content_type == "application/json":
            payload = json.dumps(payload, allow_nan=False)
        content = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        scripts = "'nonce-" + nonce + "'" if nonce else "'none'"
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src " + scripts +
                         "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def session_token(self):
        try:
            cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie["wd_session"].value if "wd_session" in cookie else ""
            return digest(value) if TOKEN.fullmatch(value) else ""
        except cookies.CookieError:
            return ""

    def cookie(self, token="", max_age=0):
        secure = "; Secure" if ORIGIN.startswith("https://") else ""
        return f"wd_session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"

    def actor(self, conn, admin=False):
        user = conn.execute("""SELECT users.* FROM users JOIN sessions USING(email)
            WHERE token=? AND sessions.expires>?""", (self.session_token(), int(time.time()))).fetchone()
        if not user:
            raise Problem(401, "Sign in to continue.")
        if (user["garage"] is None) != admin:
            raise Problem(403, "This account cannot open that page.")
        return user

    def staff(self, conn):
        garage = row_for(conn, self.actor(conn)["garage"])
        if access(garage) != "Active":
            raise Problem(403, "Garage access is " + access(garage).lower() + ". Contact Ivory Digital.")
        return garage

    def throttle(self, *limits):
        now = int(time.time())
        with database(True) as conn:
            conn.execute("DELETE FROM attempts WHERE expires<=?", (now,))
            for bucket, limit in limits:
                row = conn.execute("SELECT count FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and row["count"] >= limit:
                    raise Problem(429, "Too many attempts. Try again in 10 minutes.")
            for bucket, _ in limits:
                conn.execute("""INSERT INTO attempts VALUES(?,1,?)
                    ON CONFLICT(bucket) DO UPDATE SET count=count+1""", (bucket, now + 600))

    def dispatch(self):
        try:
            url = urlsplit(self.path)
            route = url.path
            if self.command == "GET" and route == "/health":
                with database() as conn:
                    conn.execute("SELECT 1 FROM garages LIMIT 1").fetchone()
                self.send(200, {"ok": True})
                return
            if not route.startswith("/api/"):
                self.page(route)
                return
            data = self.body() if self.command == "POST" else {}
            result, cookie = self.api(route[5:].split("/"), data, parse_qs(url.query))
            self.send(200, result, cookie=cookie)
        except Problem as exc:
            self.send(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self.send(500, {"error": "Workshop Desk could not complete the request."})

    def page(self, route):
        if self.command != "GET":
            raise Problem(405, "Method not allowed.")
        if route in ("/", "/admin", "/setup", "/approval"):
            file = "account.html"
        elif route == "/staff":
            with database() as conn:
                try:
                    self.staff(conn)
                except Problem as exc:
                    if exc.status not in (401, 403):
                        raise
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
            file = "index.html"
        elif re.fullmatch(r"/book/[a-z0-9-]+", route):
            file = "index.html"
        else:
            raise Problem(404, "Page not found.")
        nonce = secrets.token_urlsafe(24)
        content = (ROOT / file).read_text(encoding="utf-8").replace("<script>", f'<script nonce="{nonce}">')
        self.send(200, content, "text/html", nonce=nonce)

    def api(self, route, data, query):
        post = self.command == "POST"
        if route == ["login"] and post:
            return self.login(data)
        if route == ["logout"] and post:
            with database(True) as conn:
                conn.execute("DELETE FROM sessions WHERE token=?", (self.session_token(),))
            return {}, self.cookie()
        if route == ["setup"] and post:
            return self.setup_garage(data), None
        if route == ["account"] and not post:
            with database() as conn:
                user = self.actor(conn)
                garage = row_for(conn, user["garage"])
                return {"name": garage["name"], "access": access(garage)}, None
        if route[0] == "admin":
            with database(post) as conn:
                self.actor(conn, admin=True)
                return self.admin(conn, route[1:], data, post), None
        if route[0] == "staff":
            with database(post) as conn:
                garage = self.staff(conn)
                if route == ["staff"] and not post:
                    return workspace(garage), None
                if route == ["staff"] and post:
                    if type(data.get("revision")) is not int or data["revision"] != garage["revision"]:
                        raise Problem(409, "The diary changed elsewhere. Reload the page before saving again.")
                    validate_state(data)
                    store(conn, garage, data)
                    return {"revision": garage["revision"] + 1}, None
                if route == ["staff", "approval"] and post:
                    return self.create_approval(conn, garage, data), None
        if route[0] == "book" and len(route) == 3:
            if post:
                self.throttle(("book:" + route[1], 120))
            with database(post) as conn:
                garage = public_garage(conn, route[1])
                model = workspace(garage)
                if route[2] == "garage" and not post:
                    return {"name": garage["name"], "prices": public_prices(model),
                            "firstDate": first_booking_date(model)}, None
                if route[2] == "slots" and not post:
                    day = date_field(query.get("date", [""])[0])
                    return {"times": list(available(model, day, query.get("service", [""])[0]))}, None
                if route[2] == "job" and post:
                    return self.book(conn, garage, model, data), None
        if route == ["approval"] and post:
            return self.approval(data), None
        raise Problem(404, "Page not found.")

    def login(self, data):
        email = email_field(data)
        admin = data.get("admin") is True
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 128:
            raise Problem(400, "Check your email and password.")
        self.throttle(("login:" + email, 10), ("login:all", 180))
        with database() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        hashed = user["password"] if user else None
        salt = bytes.fromhex(hashed.split(":")[0]) if hashed else b"\x00" * 16
        match = secrets.compare_digest(password_hash(password, salt), hashed or "")
        if not match or (user["garage"] is None) != admin:
            raise Problem(401, "Check your email and password.")
        token = secrets.token_urlsafe(32)
        with database(True) as conn:
            current = conn.execute("SELECT password FROM users WHERE email=?", (email,)).fetchone()
            if not current or current["password"] != hashed:
                raise Problem(401, "Sign in again.")
            conn.execute("DELETE FROM sessions WHERE expires<=?", (int(time.time()),))
            conn.execute("DELETE FROM attempts WHERE bucket=?", ("login:" + email,))
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), email, int(time.time()) + DAY))
        return {}, self.cookie(token, DAY)

    def setup_garage(self, data):
        token = text_field(data, "token", 43)
        email = email_field(data)
        password = data.get("password")
        if not TOKEN.fullmatch(token) or email == ADMIN_EMAIL:
            raise Problem(400, "Check the setup link and email address.")
        if not password_ok(password):
            raise Problem(400, "Use 8 to 128 characters with at least one special character.")
        self.throttle(("setup:" + digest(token), 10), ("setup:all", 120))
        with database() as conn:
            found = conn.execute("SELECT garage FROM invites WHERE token=? AND expires>?",
                                 (digest(token), int(time.time()))).fetchone()
        if not found:
            raise Problem(410, "This setup link has expired or already been used.")
        hashed = password_hash(password)
        now = int(time.time())
        with database(True) as conn:
            found = conn.execute("SELECT garage FROM invites WHERE token=? AND expires>?",
                                 (digest(token), now)).fetchone()
            if not found:
                raise Problem(410, "This setup link has expired or already been used.")
            garage = row_for(conn, found["garage"])
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise Problem(409, "This email is already linked to an account.")
            expires = now + garage["trial_days"] * DAY if garage["mode"] == "trial" else garage["expires"]
            conn.execute("INSERT INTO users VALUES(?,?,?)", (email, hashed, garage["slug"]))
            conn.execute("UPDATE garages SET joined=?,expires=? WHERE slug=?", (now, expires, garage["slug"]))
            conn.execute("DELETE FROM invites WHERE garage=?", (garage["slug"],))
        return {}

    def admin(self, conn, route, data, post):
        if not route and not post:
            return {"garages": [{
                **{key: row[key] for key in ("slug", "name", "mode", "expires", "suspended", "joined", "trial_days", "email")},
                "access": access(row),
                "payments": [dict(p) for p in conn.execute(
                    "SELECT amount,paid_on,through,reference FROM payments WHERE garage=? ORDER BY paid_on DESC,id DESC", (row["slug"],))]
            } for row in conn.execute("SELECT garages.*,users.email FROM garages LEFT JOIN users ON users.garage=garages.slug ORDER BY name")]}
        if not post or len(route) != 1:
            raise Problem(404, "Page not found.")
        action = route[0]
        slug = text_field(data, "slug", 60)
        if not SLUG.fullmatch(slug):
            raise Problem(400, "Use lowercase letters, numbers and hyphens in the garage code.")
        expiry = data.get("expiry")
        if action == "create":
            name = text_field(data, "name", 100)
            mode = data.get("mode")
            if mode not in ("trial", "paid", "complimentary"):
                raise Problem(400, "Choose a garage access type.")
            if conn.execute("SELECT 1 FROM garages WHERE slug=?", (slug,)).fetchone():
                raise Problem(409, "This garage code is already in use.")
            expires = end_of_day(expiry) if expiry and mode == "complimentary" else None
            conn.execute("INSERT INTO garages (slug,name,mode,expires) VALUES(?,?,?,?)", (slug, name, mode, expires))
            return {"link": invite(conn, slug)}
        garage = row_for(conn, slug)
        if action == "invite":
            if garage["joined"]:
                raise Problem(409, "This garage owner has already set up their account.")
            return {"link": invite(conn, slug)}
        if action in ("suspend", "restore"):
            conn.execute("UPDATE garages SET suspended=? WHERE slug=?", (int(action == "suspend"), slug))
        elif action == "extend":
            if garage["mode"] != "trial":
                raise Problem(400, "Only trials can be extended.")
            days = data.get("days")
            if type(days) is not int or not 1 <= days <= 365:
                raise Problem(400, "Enter between 1 and 365 extra days.")
            if garage["joined"]:
                expires = max(int(time.time()), garage["expires"] or 0) + days * DAY
                conn.execute("UPDATE garages SET expires=? WHERE slug=?", (expires, slug))
            else:
                conn.execute("UPDATE garages SET trial_days=trial_days+? WHERE slug=?", (days, slug))
        elif action == "complimentary":
            conn.execute("UPDATE garages SET mode='complimentary',expires=?,suspended=0 WHERE slug=?",
                         (end_of_day(expiry) if expiry else None, slug))
        elif action == "payment":
            paid_on = date_field(data.get("paid_on"))
            through = date_field(data.get("through"))
            if through < paid_on:
                raise Problem(400, "Paid-through date must be on or after the payment date.")
            amount = payment_amount(data.get("amount"))
            reference = text_field(data, "reference", 200, optional=True)
            expires = end_of_day(through.isoformat())
            if garage["mode"] == "paid":
                expires = max(expires, garage["expires"] or 0)
            conn.execute("INSERT INTO payments (garage,amount,paid_on,through,reference) VALUES(?,?,?,?,?)",
                         (slug, amount, paid_on.isoformat(), through.isoformat(), reference))
            conn.execute("UPDATE garages SET mode='paid',expires=? WHERE slug=?", (expires, slug))
        else:
            raise Problem(404, "Page not found.")
        return {}

    def book(self, conn, garage, model, data):
        day = date_field(data.get("date"))
        service = text_field(data, "service", 50)
        clock = text_field(data, "time", 5)
        bay = available(model, day, service).get(clock)
        if not bay:
            raise Problem(409, "That time is no longer available. Go back and choose another.")
        reg = text_field(data, "reg", 12).upper().replace(" ", "")
        phone = text_field(data, "phone", 30)
        if not re.fullmatch(r"[A-Z0-9]{2,8}", reg) or not re.fullmatch(r"[+0-9().\s-]+", phone) or len(re.sub(r"\D", "", phone)) < 7:
            raise Problem(400, "Check your vehicle registration and phone number.")
        job = {"id": "WD-" + secrets.token_hex(6).upper(), "date": day.isoformat(), "service": service,
               "time": clock, "bay": bay, "length": SERVICES[service], "status": "Requested", "source": "Online",
               "reg": reg, "phone": phone, "email": email_field(data), "name": text_field(data, "name", 100),
               "vehicle": text_field(data, "vehicle", 100), "notes": text_field(data, "notes", 1000, optional=True),
               "staffNotes": ""}
        model.setdefault("jobs", []).append(job)
        store(conn, garage, model)
        return {"id": job["id"]}

    def estimate(self, model, job_id, version):
        job = next((job for job in model.get("jobs", []) if job["id"] == job_id), None)
        work = job.get("work", {}) if job else {}
        versions = work.get("versions", [])
        estimate = versions[-1] if versions else None
        if not estimate or estimate.get("version") != version or estimate.get("decision") or work.get("invoice"):
            raise Problem(410, "This estimate is no longer awaiting a decision.")
        return job, estimate

    def create_approval(self, conn, garage, data):
        job_id = text_field(data, "job", 50)
        version = data.get("version")
        if type(version) is not int:
            raise Problem(400, "Invalid estimate version.")
        self.estimate(workspace(garage), job_id, version)
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                     (digest(token), garage["slug"], job_id, version, int(time.time()) + 14 * DAY))
        return {"link": ORIGIN + "/approval#" + token}

    def approval(self, data):
        token = text_field(data, "token", 43)
        if not TOKEN.fullmatch(token):
            raise Problem(410, "This approval link is unavailable.")
        decision = data.get("decision")
        if decision is not None and decision not in ("Approved", "Declined"):
            raise Problem(400, "Choose approve or decline.")
        with database(decision is not None) as conn:
            row = conn.execute("SELECT * FROM approvals WHERE token=? AND expires>?",
                               (digest(token), int(time.time()))).fetchone()
            if not row:
                raise Problem(410, "This approval link has expired or been replaced.")
            garage = public_garage(conn, row["garage"])
            model = workspace(garage)
            job, estimate = self.estimate(model, row["job"], row["version"])
            lines = [{key: line[key] for key in ("description", "qty", "price")} for line in estimate["lines"]]
            total = sum(line["qty"] * line["price"] for line in lines)
            if decision is not None:
                name = text_field(data, "name", 100)
                estimate["decision"] = {"name": name, "method": "Customer approval link", "result": decision,
                                        "date": datetime.now(LONDON).strftime("%d/%m/%Y %H:%M")}
                if decision == "Approved":
                    job["total"] = total
                    if job["status"] == "Awaiting approval":
                        job["status"] = "In progress"
                conn.execute("DELETE FROM approvals WHERE token=?", (digest(token),))
                store(conn, garage, model)
            return {"garage": garage["name"], "vehicle": job["vehicle"], "reg": job["reg"],
                    "version": row["version"], "lines": lines, "total": total}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "set-admin-password"))
    args = parser.parse_args()
    os.umask(0o077)
    initialise()
    if args.command == "set-admin-password":
        password = getpass.getpass(f"Password for {ADMIN_EMAIL}: ")
        if not password_ok(password) or password != getpass.getpass("Confirm password: "):
            parser.error("Use a matching password with 8 to 128 characters and at least one special character.")
        hashed = password_hash(password)
        with database(True) as conn:
            conn.execute("""INSERT INTO users VALUES(?,?,NULL)
                ON CONFLICT(email) DO UPDATE SET password=excluded.password""", (ADMIN_EMAIL, hashed))
            conn.execute("DELETE FROM sessions WHERE email=?", (ADMIN_EMAIL,))
        print("Superadmin password set.")
    else:
        if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::\d+)?|http://127\.0\.0\.1:\d+", ORIGIN):
            parser.error("Set PUBLIC_ORIGIN to the HTTPS site URL.")
        ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
