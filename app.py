import os
import re
import csv
import io
import base64
import secrets
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage
import calendar as calendar_module
import sqlite3
from datetime import datetime, timedelta, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------------------------------------------------------
# Password-reset delivery (both optional; resets fall back to an admin doing
# it from the Users page until one of these is configured).
#
# EMAIL - set these environment variables, e.g. for a Gmail account:
#   PHX_SMTP_HOST=smtp.gmail.com
#   PHX_SMTP_USER=youraccount@gmail.com
#   PHX_SMTP_PASSWORD=<a Gmail "App Password" - google.com/apppasswords>
#   (PHX_SMTP_PORT defaults to 587, PHX_SMTP_FROM defaults to the user)
#
# TEXT MESSAGES - set these from a Twilio account (twilio.com):
#   PHX_TWILIO_SID=ACxxxxxxxx        (Account SID)
#   PHX_TWILIO_TOKEN=xxxxxxxx        (Auth Token)
#   PHX_TWILIO_FROM=+15551234567     (your Twilio phone number)
# ---------------------------------------------------------------------------
SMTP_HOST = os.environ.get("PHX_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("PHX_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("PHX_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("PHX_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("PHX_SMTP_FROM", "") or SMTP_USER

TWILIO_SID = os.environ.get("PHX_TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("PHX_TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("PHX_TWILIO_FROM", "")

RESET_CODE_MINUTES = 15


def email_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def sms_configured():
    return bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM)


def send_reset_email(to_addr, code):
    msg = EmailMessage()
    msg["Subject"] = "Swarm Baseball password reset code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(
        f"Your Swarm Baseball password reset code is: {code}\n\n"
        f"Enter it on the reset page within {RESET_CODE_MINUTES} minutes. "
        "If you didn't request this, you can ignore this email."
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False


def send_reset_sms(to_phone, code):
    to_number = f"+1{to_phone}" if len(to_phone) == 10 else f"+{to_phone}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "To": to_number,
        "From": TWILIO_FROM,
        "Body": f"Swarm Baseball password reset code: {code} (expires in {RESET_CODE_MINUTES} min)",
    }).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pvtracker.db")
VIDEO_DIR = os.path.join(BASE_DIR, "static", "uploads", "videos")
PHOTO_DIR = os.path.join(BASE_DIR, "static", "uploads", "photos")

ALLOWED_VIDEO_EXT = {"mp4", "mov", "m4v", "webm", "avi"}
ALLOWED_PHOTO_EXT = {"png", "jpg", "jpeg", "gif"}
ALLOWED_CSV_EXT = {"csv"}

# Preset session types offered in the Category dropdown on both the CSV and
# video upload forms. "Other" reveals a free-text box for anything else.
CATEGORY_OPTIONS = ["Bullpen", "Pulldown", "Game", "Live ABs", "Flat Ground", "Practice", "Other"]

# Session types whose velo stats get charted on the player page. Each chart
# shows one line per session type (e.g. the fastball chart compares Bullpen,
# Live ABs, and Game velo). "Live BP" is kept for data imported before that
# category was renamed to "Live ABs".
VELOCITY_CHART_CATEGORIES = ["Bullpen", "Live ABs", "Live BP", "Game", "Pulldown"]

# Preferred display order for categories that exist; anything else found in
# the data is appended after these, alphabetically. Used by the player
# page's per-category spreadsheet tables.
CATEGORY_SORT_ORDER = ["Bullpen", "Pulldown", "Game", "Live ABs", "Live BP", "Flat Ground", "Practice", "General"]


def _category_sort_key(cat):
    try:
        return (0, CATEGORY_SORT_ORDER.index(cat))
    except ValueError:
        return (1, cat.lower())

# Values that count as "no data" in a CSV cell and get skipped rather than
# imported as a stat.
BLANK_VALUES = {"", "null", "n/a", "na", "-", "--", "none", "nan"}

# Common pitch-type abbreviations, used only for the sample CSVs / docs so
# Reed knows what column names the importer will recognize as pitch velo.
PITCH_TYPES = [
    ("FB", "Four-Seam Fastball"),
    ("SI", "Sinker / Two-Seam"),
    ("CT", "Cutter"),
    ("SL", "Slider"),
    ("SWP", "Sweeper"),
    ("CB", "Curveball"),
    ("CH", "Changeup"),
    ("SPL", "Splitter"),
]

# Extra per-player text fields: player contact info and recruiting profile
# links (Perfect Game / Prep Baseball Report). Shared by the DB migration and
# the add/edit player forms. Other people connected to the player (coaches,
# trainers, parents, ...) live in the player_contacts table instead, so a
# player can have any number of them.
PLAYER_CONTACT_FIELDS = ["phone", "email", "pg_url", "pbr_url"]

app = Flask(__name__)
app.secret_key = os.environ.get("PHX_SECRET_KEY", "phoenix-pitching-lab-tracker")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB max upload (videos)
app.permanent_session_lifetime = timedelta(days=30)

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)


# ---------- Database helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            jersey_number TEXT,
            position TEXT,
            grad_year TEXT,
            photo_filename TEXT,
            notes TEXT,
            group_number TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stat_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            category TEXT,
            stat_name TEXT NOT NULL,
            stat_value REAL NOT NULL,
            source_file TEXT,
            imported_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            title TEXT,
            category TEXT,
            notes TEXT,
            filename TEXT NOT NULL,
            uploaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            video_id INTEGER,
            commenter_name TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE,
            FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS throwing_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            phone TEXT UNIQUE,
            password_hash TEXT,
            is_admin INTEGER DEFAULT 0,
            is_owner INTEGER DEFAULT 0,
            reset_code TEXT,
            reset_expires TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS invite_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            created_by INTEGER,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trackman_pitches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            category TEXT,
            pitch_no INTEGER,
            pitch_type TEXT,
            pitch_call TEXT,
            rel_speed REAL,
            spin_rate REAL,
            spin_axis TEXT,
            ivb REAL,
            hb REAL,
            rel_height REAL,
            rel_side REAL,
            extension REAL,
            vaa REAL,
            loc_height REAL,
            loc_side REAL,
            exit_speed REAL,
            launch_angle REAL,
            source_file TEXT,
            imported_at TEXT,
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS player_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            relationship TEXT,
            name TEXT,
            phone TEXT,
            email TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()

    # Migration: add group_number to a players table that existed before this column did.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "group_number" not in existing_cols:
        conn.execute("ALTER TABLE players ADD COLUMN group_number TEXT")
        conn.commit()

    # Migration: players can now be assigned to a team.
    if "team_id" not in existing_cols:
        conn.execute("ALTER TABLE players ADD COLUMN team_id INTEGER REFERENCES teams(id)")
        conn.commit()

    # Migration: player contact info and recruiting profile links.
    for col in PLAYER_CONTACT_FIELDS:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE players ADD COLUMN {col} TEXT")
    conn.commit()

    # Migration: an earlier version stored exactly two coach contacts as
    # coach1_*/coach2_* columns on players. Move any data there into the
    # player_contacts table (as "Coach" contacts), then blank the old columns
    # so this only ever runs once per row. The dead columns themselves are
    # left in place (dropping columns is messy in SQLite) but are never
    # read or written again.
    pcols = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "coach1_name" in pcols:
        rows = conn.execute(
            """SELECT id, coach1_name, coach1_phone, coach1_email,
                      coach2_name, coach2_phone, coach2_email
               FROM players
               WHERE IFNULL(coach1_name,'') != '' OR IFNULL(coach1_phone,'') != '' OR IFNULL(coach1_email,'') != ''
                  OR IFNULL(coach2_name,'') != '' OR IFNULL(coach2_phone,'') != '' OR IFNULL(coach2_email,'') != ''"""
        ).fetchall()
        for r in rows:
            for i in (1, 2):
                nm, ph, em = r[f"coach{i}_name"], r[f"coach{i}_phone"], r[f"coach{i}_email"]
                if (nm or "").strip() or (ph or "").strip() or (em or "").strip():
                    conn.execute(
                        "INSERT INTO player_contacts (player_id, relationship, name, phone, email) VALUES (?, 'Coach', ?, ?, ?)",
                        (r["id"], nm or "", ph or "", em or ""),
                    )
        if rows:
            conn.execute(
                """UPDATE players SET coach1_name=NULL, coach1_phone=NULL, coach1_email=NULL,
                   coach2_name=NULL, coach2_phone=NULL, coach2_email=NULL"""
            )
        conn.commit()

    # Migration: this site used to have a per-group throwing calendar
    # (group_number + fixed activity dropdown). It's now a single shared
    # calendar with one freeform message per entry, so an old-shaped table
    # gets rebuilt with the new columns. Any old throwing-calendar entries
    # are dropped in the process (they belonged to a feature that no longer
    # exists) rather than converted, since there's no meaningful message to
    # backfill them with.
    throwing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(throwing_entries)")}
    if "group_number" in throwing_cols or "activity" in throwing_cols:
        conn.executescript(
            """
            DROP TABLE throwing_entries;
            CREATE TABLE throwing_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()

    # Migration: self-service password reset codes on a users table that
    # existed before those columns did.
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for col in ("reset_code", "reset_expires"):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    conn.commit()

    # Migration: site-owner tier. Exactly one owner; if the column is new,
    # the earliest admin (the person who ran first-time setup) becomes owner.
    if "is_owner" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_owner INTEGER DEFAULT 0")
        conn.commit()
    if conn.execute("SELECT COUNT(*) FROM users WHERE is_owner = 1").fetchone()[0] == 0:
        first_admin = conn.execute(
            "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if first_admin:
            conn.execute("UPDATE users SET is_owner = 1 WHERE id = ?", (first_admin["id"],))
            conn.commit()

    # Migration: calendar entries now belong to a team (NULL = General calendar).
    throwing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(throwing_entries)")}
    if "team_id" not in throwing_cols:
        conn.execute("ALTER TABLE throwing_entries ADD COLUMN team_id INTEGER REFERENCES teams(id)")
        conn.commit()

    # Migration: events can carry an optional location and any other info.
    for col in ("location", "details"):
        if col not in throwing_cols:
            conn.execute(f"ALTER TABLE throwing_entries ADD COLUMN {col} TEXT")
    conn.commit()

    conn.close()


def allowed_file(filename, allowed_ext):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_ext


def is_blank(raw_val):
    """Treat empty cells and common null spellings (NULL, N/A, -, none, nan) as no data."""
    return (raw_val or "").strip().lower() in BLANK_VALUES


def normalize_col(name):
    """Loosen a column header for matching: lowercase, strip spaces/underscores/%/#."""
    return (name or "").lower().replace(" ", "").replace("_", "").replace("%", "").replace("#", "")


STRIKES_COL_NAMES = {"strikes", "strike", "numstrikes"}
PITCHES_COL_NAMES = {"pitches", "totalpitches", "pitchcount", "numpitches"}
STRIKE_PCT_COL_NAMES = {"strikepct", "strikepercent", "strikepercentage"}

IP_COL_NAMES = {"ip", "inningspitched", "innings"}

# Counting stats that should be TOTALED (not averaged) in the summary row of
# the player page's stat tables - the things a big-league stat line adds up.
# Names are matched after normalize_col() (lowercased, spaces/underscores
# stripped). Anything that looks like a rate (velo, %, ERA, K/7, avg) is
# always averaged instead, and innings pitched are summed in baseball
# notation (5.1 + 4.2 = 10.0, since .1/.2 are thirds of an inning).
CUMULATIVE_STAT_NAMES = {
    "ip", "inningspitched", "innings",
    "er", "earnedruns", "r", "runs",
    "h", "hits", "singles", "doubles", "triples", "hr", "homeruns",
    "k", "so", "strikeouts", "ks",
    "bb", "walks", "hbp", "hitbypitch",
    "pitches", "totalpitches", "pitchcount", "numpitches",
    "strikes", "balls", "outs",
    "bf", "battersfaced", "ab", "atbats",
    "wp", "wildpitches", "pickoffs",
    "g", "games", "appearances",
}

RATE_STAT_HINTS = ("velo", "%", "pct", "era", "avg", "rate", "k/7")

# ---- TrackMan import ----
# A TrackMan pitching export is one row PER PITCH. The importer rolls those
# up per pitcher per date into session stats. These map TrackMan's
# TaggedPitchType / AutoPitchType values onto the site's pitch abbreviations.
TRACKMAN_PITCH_TYPE_MAP = {
    "fastball": "FB", "fourseamfastball": "FB", "fourseam": "FB", "fourseamfb": "FB",
    "sinker": "SI", "twoseamfastball": "SI", "twoseam": "SI",
    "cutter": "CT",
    "slider": "SL",
    "sweeper": "SWP",
    "curveball": "CB", "knucklecurve": "CB",
    "changeup": "CH",
    "splitter": "SPL",
}

# PitchCall values that count as strikes (called, swinging, fouls, in play).
TRACKMAN_STRIKE_CALLS = {
    "strikecalled", "strikeswinging", "foulball",
    "foulballnotfieldable", "foulballfieldable", "foultip", "inplay",
}

# Session types offered on the TrackMan import form.
TRACKMAN_CATEGORY_OPTIONS = ["Bullpen", "Live ABs", "Game"]


def is_cumulative_stat(name):
    low = (name or "").lower()
    if any(h in low for h in RATE_STAT_HINTS):
        return False
    return normalize_col(name) in CUMULATIVE_STAT_NAMES


def avg_or_none(vals, nd=1):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), nd) if vals else None


def sum_innings(vals):
    """Total IP in baseball notation: .1 = one out, .2 = two outs."""
    total_thirds = 0
    for v in vals:
        whole = int(v)
        frac = round((v - whole) * 10)
        total_thirds += whole * 3 + (1 if frac == 1 else 2 if frac == 2 else 0)
    return round(total_thirds // 3 + (total_thirds % 3) / 10, 1)
ER_COL_NAMES = {"er", "earnedruns"}
ERA_COL_NAMES = {"era"}
K_COL_NAMES = {"k", "so", "strikeouts", "ks"}

# Innings a game is worth for these rate stats. High school baseball plays
# 7-inning games (not the MLB's 9), so ERA and K/7 are scaled off this instead.
INNINGS_PER_GAME = 7


def parse_innings_pitched(raw_val):
    """Baseball IP notation puts thirds of an inning after the decimal point
    (.1 = one out = 1/3 inning, .2 = two outs = 2/3 inning) - it is NOT a
    literal decimal fraction. "5.1" means 5 1/3 innings, not 5.1 innings."""
    value = float(raw_val.strip())
    whole = int(value)
    frac_digit = round((value - whole) * 10)
    if frac_digit == 1:
        return whole + 1 / 3
    if frac_digit == 2:
        return whole + 2 / 3
    return value


def parse_date(value):
    """Try a handful of common date formats, fall back to today's date."""
    value = (value or "").strip()
    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.today().strftime("%Y-%m-%d")


def format_comment_time(value):
    """SQLite datetime('now') gives UTC 'YYYY-MM-DD HH:MM:SS'; show something friendlier."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y %I:%M %p")
    except (ValueError, TypeError):
        return value


app.jinja_env.filters["friendly_time"] = format_comment_time

# "StrikeCalled" -> "Strike Called" for TrackMan pitch results.
app.jinja_env.filters["spaced"] = lambda v: re.sub(r"(?<!^)(?=[A-Z])", " ", v) if v else v


# ---------- Accounts & login ----------
# Access works on a whitelist: an admin adds someone's email and/or phone on
# the Users page. That person then signs in with either one; on their first
# visit (no password yet) they're prompted to create their own password.

def normalize_phone(raw):
    """Keep digits only so 555-123-4567, (555) 123-4567, and 5551234567 all
    match. A leading 1 on an 11-digit US number is dropped."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def find_user_by_identifier(conn, identifier):
    """Look a user up by email (case-insensitive) or phone number."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    user = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (ident.lower(),)).fetchone()
    if user:
        return user
    phone = normalize_phone(ident)
    if phone and len(phone) >= 7:
        return conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    return None


def any_users_exist(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@app.before_request
def require_login():
    open_endpoints = ("login", "static", "setup", "set_password", "logout",
                      "forgot_password", "forgot_verify", "join")
    if request.endpoint in open_endpoints:
        return None
    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the admin account. Disabled once any user exists."""
    conn = get_db()
    if any_users_exist(conn):
        conn.close()
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not email and not phone:
            flash("Enter an email or a phone number (or both).", "error")
        elif len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            cur = conn.execute(
                "INSERT INTO users (name, email, phone, password_hash, is_admin, is_owner) VALUES (?, ?, ?, ?, 1, 1)",
                (name, email or None, phone, generate_password_hash(password)),
            )
            conn.commit()
            session.permanent = True
            session["user_id"] = cur.lastrowid
            session["user_name"] = name
            session["is_admin"] = True
            session["is_owner"] = True
            conn.close()
            flash("Site owner account created. Welcome!", "success")
            return redirect(url_for("index"))
        conn.close()
        return redirect(url_for("setup"))

    conn.close()
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    if not any_users_exist(conn):
        conn.close()
        return redirect(url_for("setup"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = find_user_by_identifier(conn, identifier)
        conn.close()

        if not user:
            flash("That email or phone number isn't approved for this site. Ask your coach to add you.", "error")
            return redirect(url_for("login", next=request.form.get("next", "")))

        # Invited but hasn't created a password yet -> send them to do that.
        if not user["password_hash"]:
            session["pending_user_id"] = user["id"]
            return redirect(url_for("set_password", next=request.form.get("next", "")))

        if not check_password_hash(user["password_hash"], password):
            flash("Wrong password. Try again.", "error")
            return redirect(url_for("login", next=request.form.get("next", "")))

        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["is_admin"] = bool(user["is_admin"])
        session["is_owner"] = bool(user["is_owner"])
        next_url = request.form.get("next") or url_for("index")
        return redirect(next_url)

    conn.close()
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    pending_id = session.get("pending_user_id")
    if not pending_id:
        return redirect(url_for("login"))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (pending_id,)).fetchone()
    if not user or user["password_hash"]:
        conn.close()
        session.pop("pending_user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
            conn.close()
            return redirect(url_for("set_password", next=request.form.get("next", "")))
        if password != confirm:
            flash("Those passwords don't match.", "error")
            conn.close()
            return redirect(url_for("set_password", next=request.form.get("next", "")))

        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), pending_id))
        conn.commit()
        conn.close()

        session.pop("pending_user_id", None)
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["is_admin"] = bool(user["is_admin"])
        session["is_owner"] = bool(user["is_owner"])
        flash("Password set - you're in!", "success")
        return redirect(request.form.get("next") or url_for("index"))

    conn.close()
    return render_template("set_password.html", user=user, next=request.args.get("next", ""))


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        conn = get_db()
        user = find_user_by_identifier(conn, identifier)

        if not user:
            conn.close()
            flash("That email or phone number isn't on the approved list.", "error")
            return redirect(url_for("forgot_password"))

        if not user["password_hash"]:
            conn.close()
            flash("No password to reset - just sign in with your email or phone and you'll create one.", "success")
            return redirect(url_for("login"))

        code = f"{secrets.randbelow(1000000):06d}"
        expires = (datetime.now() + timedelta(minutes=RESET_CODE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE users SET reset_code = ?, reset_expires = ? WHERE id = ?", (code, expires, user["id"]))
        conn.commit()
        conn.close()

        # Send via whichever channel matches what they typed, falling back to
        # whatever else is configured and on file for them.
        prefer_email = "@" in identifier
        attempts = []
        if user["email"] and email_configured():
            attempts.append(("email", lambda: send_reset_email(user["email"], code)))
        if user["phone"] and sms_configured():
            attempts.append(("text", lambda: send_reset_sms(user["phone"], code)))
        if not prefer_email:
            attempts.reverse()

        for channel, attempt in attempts:
            if attempt():
                session["reset_user_id"] = user["id"]
                flash(f"A 6-digit code was sent by {channel}. Enter it below with your new password.", "success")
                return redirect(url_for("forgot_verify"))

        flash("Codes can't be sent right now - ask your coach/admin to reset your password from the Users page.", "error")
        return redirect(url_for("login"))

    return render_template("forgot.html")


@app.route("/forgot/verify", methods=["GET", "POST"])
def forgot_verify():
    user_id = session.get("reset_user_id")
    if not user_id:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not user or not user["reset_code"] or not user["reset_expires"] or now > user["reset_expires"]:
            conn.close()
            session.pop("reset_user_id", None)
            flash("That code expired. Request a new one.", "error")
            return redirect(url_for("forgot_password"))
        if code != user["reset_code"]:
            conn.close()
            flash("That code isn't right. Check the message and try again.", "error")
            return redirect(url_for("forgot_verify"))
        if len(password) < 6:
            conn.close()
            flash("Password needs at least 6 characters.", "error")
            return redirect(url_for("forgot_verify"))
        if password != confirm:
            conn.close()
            flash("Those passwords don't match.", "error")
            return redirect(url_for("forgot_verify"))

        conn.execute(
            "UPDATE users SET password_hash = ?, reset_code = NULL, reset_expires = NULL WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        conn.commit()
        conn.close()

        session.pop("reset_user_id", None)
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["is_admin"] = bool(user["is_admin"])
        session["is_owner"] = bool(user["is_owner"])
        flash("Password updated - you're signed in.", "success")
        return redirect(url_for("index"))

    return render_template("forgot_verify.html")


@app.route("/account", methods=["GET", "POST"])
def account():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        current = request.form.get("current_password", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not check_password_hash(user["password_hash"] or "", current):
            flash("Your current password isn't right.", "error")
        elif len(password) < 6:
            flash("New password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(password), user["id"]))
            conn.commit()
            conn.close()
            flash("Password changed.", "success")
            return redirect(url_for("account"))
        conn.close()
        return redirect(url_for("account"))

    conn.close()
    return render_template("account.html", user=user)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Routes: user management (admin only) ----------

def can_manage_user(target):
    """Tiered permissions: the owner can manage anyone but the owner;
    admins can only manage regular members."""
    if target is None or target["is_owner"]:
        return False
    if session.get("is_owner"):
        return True
    return not target["is_admin"]


@app.route("/users")
@admin_required
def users_page():
    conn = get_db()
    users = conn.execute(
        "SELECT * FROM users ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()
    invites = conn.execute("SELECT * FROM invite_links ORDER BY created_at DESC").fetchall()
    conn.close()
    owners = [u for u in users if u["is_owner"]]
    admins = [u for u in users if u["is_admin"] and not u["is_owner"]]
    members = [u for u in users if not u["is_admin"] and not u["is_owner"]]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "users.html", owners=owners, admins=admins, members=members,
        invites=invites, now=now,
    )


@app.route("/invites/create", methods=["POST"])
@admin_required
def create_invite():
    token = secrets.token_urlsafe(16)
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        "INSERT INTO invite_links (token, created_by, expires_at) VALUES (?, ?, ?)",
        (token, session["user_id"], expires),
    )
    conn.commit()
    conn.close()
    flash("Invite link created - it works for 7 days. Copy it and send it to whoever you want to let in.", "success")
    return redirect(url_for("users_page"))


@app.route("/invites/<int:invite_id>/delete", methods=["POST"])
@admin_required
def delete_invite(invite_id):
    conn = get_db()
    conn.execute("DELETE FROM invite_links WHERE id = ?", (invite_id,))
    conn.commit()
    conn.close()
    flash("Invite link deactivated.", "success")
    return redirect(url_for("users_page"))


@app.route("/join/<token>", methods=["GET", "POST"])
def join(token):
    conn = get_db()
    invite = conn.execute("SELECT * FROM invite_links WHERE token = ?", (token,)).fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not invite or (invite["expires_at"] and now > invite["expires_at"]):
        conn.close()
        return render_template("join.html", invalid=True, token=token)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not name:
            flash("Your name is required.", "error")
        elif not email and not phone:
            flash("Enter an email or a phone number (or both) - you'll sign in with it.", "error")
        elif len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO users (name, email, phone, password_hash, is_admin) VALUES (?, ?, ?, ?, 0)",
                    (name, email or None, phone, generate_password_hash(password)),
                )
                conn.commit()
                session.permanent = True
                session["user_id"] = cur.lastrowid
                session["user_name"] = name
                session["is_admin"] = False
                session["is_owner"] = False
                conn.close()
                flash(f"Welcome, {name}! Your account is ready.", "success")
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("An account with that email or phone already exists - try signing in instead.", "error")
        conn.close()
        return redirect(url_for("join", token=token))

    conn.close()
    return render_template("join.html", invalid=False, token=token)


@app.route("/users/add", methods=["POST"])
@admin_required
def add_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower() or None
    phone = normalize_phone(request.form.get("phone", ""))
    # Only the site owner can create admins; anyone an admin invites is a member.
    is_admin = 1 if (request.form.get("is_admin") and session.get("is_owner")) else 0

    if not email and not phone:
        flash("Enter an email or a phone number (or both) so they can sign in.", "error")
        return redirect(url_for("users_page"))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, phone, is_admin) VALUES (?, ?, ?, ?)",
            (name, email, phone, is_admin),
        )
        conn.commit()
        flash(f"Added {name or email or phone}. They can now sign in and create their password.", "success")
    except sqlite3.IntegrityError:
        flash("A user with that email or phone already exists.", "error")
    conn.close()
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You don't have permission to remove that account.", "error")
        return redirect(url_for("users_page"))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User removed - they can no longer sign in.", "success")
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You don't have permission to reset that account's password.", "error")
        return redirect(url_for("users_page"))
    conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("Password cleared - they'll create a new one next time they sign in.", "success")
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def change_user_role(user_id):
    """Owner only: promote a member to admin or demote an admin to member."""
    if not session.get("is_owner"):
        abort(403)
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You can't change that account's role.", "error")
        return redirect(url_for("users_page"))
    new_role = 0 if target["is_admin"] else 1
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash(f"{target['name'] or target['email'] or target['phone']} is now {'an admin' if new_role else 'a member'}.", "success")
    return redirect(url_for("users_page"))


# ---------- Routes: dashboard ----------

@app.route("/")
def index():
    # Optional filters: ?q= free-text search (matches player name, team name,
    # or grad year - so "2027 - Red" pulls up everyone on that team), and
    # ?team= a team id, or "none" for unassigned players.
    q = request.args.get("q", "").strip()
    team_filter = request.args.get("team", "").strip()

    sql = """
        SELECT p.*, t.name AS team_name,
               (SELECT COUNT(*) FROM videos v WHERE v.player_id = p.id) AS video_count,
               (SELECT COUNT(*) FROM stat_entries s WHERE s.player_id = p.id) AS stat_count,
               (SELECT MAX(entry_date) FROM stat_entries s WHERE s.player_id = p.id) AS last_stat_date,
               (SELECT MAX(entry_date) FROM videos v WHERE v.player_id = p.id) AS last_video_date
        FROM players p
        LEFT JOIN teams t ON t.id = p.team_id
        """
    where = []
    params = []
    if q:
        like = f"%{q}%"
        where.append("(p.name LIKE ? OR IFNULL(t.name, '') LIKE ? OR IFNULL(p.grad_year, '') LIKE ?)")
        params.extend([like, like, like])
    if team_filter == "none":
        where.append("p.team_id IS NULL")
    elif team_filter.isdigit():
        where.append("p.team_id = ?")
        params.append(int(team_filter))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.name COLLATE NOCASE ASC"

    conn = get_db()
    players = conn.execute(sql, params).fetchall()
    all_teams = conn.execute("SELECT * FROM teams ORDER BY name COLLATE NOCASE ASC").fetchall()
    conn.close()
    return render_template("index.html", players=players, teams=all_teams, q=q, team_filter=team_filter)


# ---------- Routes: teams ----------

@app.route("/teams")
def teams_page():
    conn = get_db()
    team_rows = conn.execute(
        """SELECT t.*, COUNT(p.id) AS player_count
           FROM teams t LEFT JOIN players p ON p.team_id = t.id
           GROUP BY t.id
           ORDER BY t.name COLLATE NOCASE ASC"""
    ).fetchall()
    unassigned_count = conn.execute("SELECT COUNT(*) FROM players WHERE team_id IS NULL").fetchone()[0]
    conn.close()
    return render_template("teams.html", teams=team_rows, unassigned_count=unassigned_count)


@app.route("/teams/add", methods=["POST"])
def add_team():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams_page"))
    conn = get_db()
    try:
        conn.execute("INSERT INTO teams (name) VALUES (?)", (name,))
        conn.commit()
        flash(f"Added team {name}.", "success")
    except sqlite3.IntegrityError:
        flash(f"A team named {name} already exists.", "error")
    conn.close()
    return redirect(url_for("teams_page"))


@app.route("/teams/<int:team_id>/rename", methods=["POST"])
def rename_team(team_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams_page"))
    conn = get_db()
    try:
        conn.execute("UPDATE teams SET name = ? WHERE id = ?", (name, team_id))
        conn.commit()
        flash("Team renamed.", "success")
    except sqlite3.IntegrityError:
        flash(f"A team named {name} already exists.", "error")
    conn.close()
    return redirect(url_for("teams_page"))


@app.route("/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id):
    conn = get_db()
    conn.execute("UPDATE players SET team_id = NULL WHERE team_id = ?", (team_id,))
    conn.execute("UPDATE throwing_entries SET team_id = NULL WHERE team_id = ?", (team_id,))
    conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    conn.commit()
    conn.close()
    flash("Team deleted. Its players are now unassigned and its calendar entries moved to the General calendar.", "success")
    return redirect(url_for("teams_page"))


# ---------- Routes: players ----------

def _team_id_from_form():
    """The team dropdown posts a team id, or empty string for 'No team'."""
    raw = request.form.get("team_id", "").strip()
    return int(raw) if raw.isdigit() else None


def _all_teams(conn):
    return conn.execute("SELECT * FROM teams ORDER BY name COLLATE NOCASE ASC").fetchall()


def _contacts_from_form():
    """The add/edit player forms post parallel lists of contact fields, one
    entry per contact row. Rows that are entirely empty are dropped."""
    rels = request.form.getlist("contact_relationship")
    names = request.form.getlist("contact_name")
    phones = request.form.getlist("contact_phone")
    emails = request.form.getlist("contact_email")
    contacts = []
    for rel, nm, ph, em in zip(rels, names, phones, emails):
        rel, nm, ph, em = rel.strip(), nm.strip(), ph.strip(), em.strip()
        if nm or ph or em:
            contacts.append((rel, nm, ph, em))
    return contacts


def _save_contacts(conn, player_id, contacts):
    """Replace a player's contact list with the rows from the form."""
    conn.execute("DELETE FROM player_contacts WHERE player_id = ?", (player_id,))
    for rel, nm, ph, em in contacts:
        conn.execute(
            "INSERT INTO player_contacts (player_id, relationship, name, phone, email) VALUES (?, ?, ?, ?, ?)",
            (player_id, rel, nm, ph, em),
        )


@app.route("/players/add", methods=["GET", "POST"])
def add_player():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
            return redirect(url_for("add_player"))

        jersey_number = request.form.get("jersey_number", "").strip()
        position = request.form.get("position", "").strip()
        grad_year = request.form.get("grad_year", "").strip()
        notes = request.form.get("notes", "").strip()
        team_id = _team_id_from_form()
        contact = {f: request.form.get(f, "").strip() for f in PLAYER_CONTACT_FIELDS}

        photo_filename = None
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT):
            safe_name = secure_filename(photo.filename)
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            photo.save(os.path.join(PHOTO_DIR, photo_filename))

        conn = get_db()
        contact_cols = ", ".join(PLAYER_CONTACT_FIELDS)
        contact_marks = ", ".join("?" for _ in PLAYER_CONTACT_FIELDS)
        cur = conn.execute(
            f"INSERT INTO players (name, jersey_number, position, grad_year, photo_filename, notes, team_id, {contact_cols}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, {contact_marks})",
            (name, jersey_number, position, grad_year, photo_filename, notes, team_id,
             *[contact[f] for f in PLAYER_CONTACT_FIELDS]),
        )
        _save_contacts(conn, cur.lastrowid, _contacts_from_form())
        conn.commit()
        conn.close()
        flash(f"Added {name} to the player page.", "success")
        return redirect(url_for("index"))

    conn = get_db()
    all_teams = _all_teams(conn)
    conn.close()
    return render_template("add_player.html", teams=all_teams, contacts=[])


@app.route("/players/<int:player_id>")
def player_detail(player_id):
    conn = get_db()
    player = conn.execute(
        "SELECT p.*, t.name AS team_name FROM players p LEFT JOIN teams t ON t.id = p.team_id WHERE p.id = ?",
        (player_id,),
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    # Optional ?date_from= / ?date_to= narrow every dated thing on the page
    # (charts, stat tables, videos) to that range. Missing ends are open.
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    date_conds = ""
    date_params = []
    if date_from:
        date_conds += " AND entry_date >= ?"
        date_params.append(date_from)
    if date_to:
        date_conds += " AND entry_date <= ?"
        date_params.append(date_to)

    stat_rows = conn.execute(
        f"SELECT entry_date, category, stat_name, stat_value FROM stat_entries WHERE player_id = ?{date_conds} ORDER BY entry_date ASC, id ASC",
        (player_id, *date_params),
    ).fetchall()

    videos = conn.execute(
        f"SELECT * FROM videos WHERE player_id = ?{date_conds} ORDER BY entry_date DESC, id DESC",
        (player_id, *date_params),
    ).fetchall()

    # Group same-day videos into one timeline entry so multiple clips from a
    # single session can be flipped through instead of listed as separate
    # rows. Videos are already ordered entry_date DESC, so consecutive rows
    # with the same date land in the same group automatically.
    video_groups = []
    for v in videos:
        if video_groups and video_groups[-1]["date"] == v["entry_date"]:
            video_groups[-1]["videos"].append(v)
        else:
            video_groups.append({"date": v["entry_date"], "videos": [v]})

    video_comment_rows = conn.execute(
        "SELECT * FROM comments WHERE player_id = ? AND video_id IS NOT NULL ORDER BY created_at ASC",
        (player_id,),
    ).fetchall()

    general_comments = conn.execute(
        "SELECT * FROM comments WHERE player_id = ? AND video_id IS NULL ORDER BY created_at ASC",
        (player_id,),
    ).fetchall()

    contacts = conn.execute(
        "SELECT * FROM player_contacts WHERE player_id = ? ORDER BY id ASC",
        (player_id,),
    ).fetchall()

    tm_rows = conn.execute(
        f"SELECT * FROM trackman_pitches WHERE player_id = ?{date_conds} ORDER BY entry_date DESC, id ASC",
        (player_id, *date_params),
    ).fetchall()

    conn.close()

    # TrackMan Reports: one entry per session (date + type), each with a
    # per-pitch-type summary and the full pitch-by-pitch detail.
    tm_by_session = {}
    for r in tm_rows:
        tm_by_session.setdefault((r["entry_date"], r["category"] or "General"), []).append(r)

    tm_sessions = []
    for (d, cat), rows in sorted(tm_by_session.items(), key=lambda kv: kv[0][0], reverse=True):
        types = {}
        for r in rows:
            types.setdefault(r["pitch_type"] or "?", []).append(r)
        type_rows = []
        for pt, rs in sorted(types.items(), key=lambda kv: -len(kv[1])):
            velos = [x["rel_speed"] for x in rs if x["rel_speed"] is not None]
            spins = [x["spin_rate"] for x in rs if x["spin_rate"] is not None]
            tilts = [x["spin_axis"] for x in rs if x["spin_axis"]]
            type_rows.append({
                "type": pt,
                "count": len(rs),
                "max_velo": round(max(velos), 1) if velos else None,
                "avg_velo": avg_or_none(velos),
                "avg_spin": avg_or_none(spins, 0),
                "tilt": max(set(tilts), key=tilts.count) if tilts else None,
                "ivb": avg_or_none([x["ivb"] for x in rs]),
                "hb": avg_or_none([x["hb"] for x in rs]),
                "ext": avg_or_none([x["extension"] for x in rs]),
                "rel_h": avg_or_none([x["rel_height"] for x in rs]),
                "rel_s": avg_or_none([x["rel_side"] for x in rs]),
                "vaa": avg_or_none([x["vaa"] for x in rs]),
            })
        strikes = sum(1 for r in rows if normalize_col(r["pitch_call"] or "") in TRACKMAN_STRIKE_CALLS)
        tm_sessions.append({
            "date": d, "category": cat, "pitch_count": len(rows),
            "strikes": strikes, "types": type_rows, "pitches": rows,
        })

    # Velocity stats (any stat name containing "velo") broken out by
    # stat_name -> category -> list of {date, value}, so the player page can
    # chart each pitch with one line per session type. "Top" is dropped from
    # stat names when grouping so "FB Top Velo" (bullpens) and "FB Velo"
    # (live ABs / games) land on the same fastball chart.
    velocity_by_stat = {}
    for row in stat_rows:
        if "velo" in row["stat_name"].lower() and row["category"] in VELOCITY_CHART_CATEGORIES:
            chart_name = " ".join(w for w in row["stat_name"].split() if w.lower() != "top")
            # Pulldown velo (Max Velo) is fastball velo, so it joins the
            # fastball chart as its own line instead of a separate chart.
            if row["category"] == "Pulldown":
                chart_name = "FB Velo"
            velocity_by_stat.setdefault(chart_name, {}).setdefault(row["category"], []).append(
                {"date": row["entry_date"], "value": row["stat_value"]}
            )

    # One spreadsheet-style pivot table per session type (Bullpen, Pulldown,
    # Game, plus anything else that's been imported): rows are dates,
    # columns are every stat recorded under that category, with a bold AVG
    # row at the bottom for each column - like a coach's spreadsheet.
    # Rows come pre-sorted (entry_date ASC, id ASC), so when two entries
    # collide on the same date/category/stat (e.g. a CSV re-imported by
    # mistake), the most-recently-imported value simply overwrites the cell.
    raw_by_category = {}
    for row in stat_rows:
        cat = row["category"] or "General"
        bucket = raw_by_category.setdefault(cat, {"stat_names": set(), "dates": set(), "cells": {}})
        bucket["stat_names"].add(row["stat_name"])
        bucket["dates"].add(row["entry_date"])
        bucket["cells"].setdefault(row["entry_date"], {})[row["stat_name"]] = row["stat_value"]

    category_tables = []
    for cat in sorted(raw_by_category.keys(), key=_category_sort_key):
        bucket = raw_by_category[cat]
        stat_names = sorted(bucket["stat_names"])
        dates = sorted(bucket["dates"])

        table_rows = []
        for d in dates:
            row_cells = bucket["cells"].get(d, {})
            table_rows.append({"date": d, "values": [row_cells.get(sn) for sn in stat_names]})

        # Summary row: counting stats (IP, H, K, BB, Pitches, ...) are
        # totaled like a season stat line; rates (velo, %, ERA) are averaged.
        averages = []
        for sn in stat_names:
            vals = [bucket["cells"][d][sn] for d in dates if sn in bucket["cells"].get(d, {})]
            if not vals:
                averages.append(None)
            elif normalize_col(sn) in IP_COL_NAMES:
                averages.append(sum_innings(vals))
            elif is_cumulative_stat(sn):
                averages.append(round(sum(vals), 2))
            else:
                averages.append(round(sum(vals) / len(vals), 2))

        category_tables.append(
            {"category": cat, "stat_names": stat_names, "rows": table_rows, "averages": averages}
        )

    comments_by_video = {}
    for c in video_comment_rows:
        comments_by_video.setdefault(c["video_id"], []).append(c)

    return render_template(
        "player.html",
        player=player,
        category_tables=category_tables,
        velocity_by_stat=velocity_by_stat,
        video_groups=video_groups,
        comments_by_video=comments_by_video,
        general_comments=general_comments,
        contacts=contacts,
        date_from=date_from,
        date_to=date_to,
        tm_sessions=tm_sessions,
    )


@app.route("/players/<int:player_id>/edit", methods=["GET", "POST"])
def edit_player(player_id):
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
            conn.close()
            return redirect(url_for("edit_player", player_id=player_id))

        jersey_number = request.form.get("jersey_number", "").strip()
        position = request.form.get("position", "").strip()
        grad_year = request.form.get("grad_year", "").strip()
        notes = request.form.get("notes", "").strip()
        team_id = _team_id_from_form()
        contact = {f: request.form.get(f, "").strip() for f in PLAYER_CONTACT_FIELDS}

        photo_filename = player["photo_filename"]
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT):
            safe_name = secure_filename(photo.filename)
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            photo.save(os.path.join(PHOTO_DIR, photo_filename))

        contact_sets = ", ".join(f"{f} = ?" for f in PLAYER_CONTACT_FIELDS)
        conn.execute(
            f"""UPDATE players SET name = ?, jersey_number = ?, position = ?, grad_year = ?,
               notes = ?, photo_filename = ?, team_id = ?, {contact_sets} WHERE id = ?""",
            (name, jersey_number, position, grad_year, notes, photo_filename, team_id,
             *[contact[f] for f in PLAYER_CONTACT_FIELDS], player_id),
        )
        _save_contacts(conn, player_id, _contacts_from_form())
        conn.commit()
        conn.close()
        flash(f"Updated {name}.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    all_teams = _all_teams(conn)
    contacts = conn.execute(
        "SELECT * FROM player_contacts WHERE player_id = ? ORDER BY id ASC", (player_id,)
    ).fetchall()
    conn.close()
    return render_template("edit_player.html", player=player, teams=all_teams, contacts=contacts)


@app.route("/players/<int:player_id>/delete", methods=["POST"])
@admin_required
def delete_player(player_id):
    conn = get_db()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    flash("Player removed.", "success")
    return redirect(url_for("index"))


# ---------- Routes: lesson calendar ----------

@app.route("/calendar")
def lesson_calendar():
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
    except ValueError:
        year, month = today.year, today.month
    # Clamp so navigation can't wander into invalid months.
    month = max(1, min(12, month))

    conn = get_db()

    # Every team has its own separate calendar; ?team=<id> picks which one.
    # No team param (or an invalid one) shows the General calendar, whose
    # entries belong to no team.
    team_param = request.args.get("team", "").strip()
    current_team = None
    if team_param.isdigit():
        current_team = conn.execute("SELECT * FROM teams WHERE id = ?", (int(team_param),)).fetchone()
    all_teams = _all_teams(conn)

    month_start = f"{year:04d}-{month:02d}-01"
    last_day = calendar_module.monthrange(year, month)[1]
    month_end = f"{year:04d}-{month:02d}-{last_day:02d}"

    # General entries (no team) appear on EVERY calendar; a team's calendar
    # shows its own entries plus the general ones.
    if current_team:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE entry_date BETWEEN ? AND ? AND (team_id = ? OR team_id IS NULL)
               ORDER BY entry_date ASC, id ASC""",
            (month_start, month_end, current_team["id"]),
        ).fetchall()
    else:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE entry_date BETWEEN ? AND ? AND team_id IS NULL
               ORDER BY entry_date ASC, id ASC""",
            (month_start, month_end),
        ).fetchall()
    conn.close()

    entries_by_date = {}
    for e in entries:
        entries_by_date.setdefault(e["entry_date"], []).append(e)

    cal = calendar_module.Calendar(firstweekday=6)  # weeks start Sunday
    weeks = cal.monthdatescalendar(year, month)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month % 12 + 1
    next_year = year + 1 if month == 12 else year

    return render_template(
        "calendar.html",
        weeks=weeks,
        entries_by_date=entries_by_date,
        year=year,
        month=month,
        month_name=calendar_module.month_name[month],
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        today_iso=today.strftime("%Y-%m-%d"),
        teams=all_teams,
        current_team=current_team,
    )


@app.route("/calendar/add", methods=["POST"])
def add_calendar_entry():
    entry_date = parse_date(request.form.get("entry_date"))
    message = request.form.get("message", "").strip()
    location = request.form.get("location", "").strip()
    details = request.form.get("details", "").strip()
    raw_team = request.form.get("team_id", "").strip()
    team_id = int(raw_team) if raw_team.isdigit() else None

    if not message:
        flash("Add a message for that lesson day (e.g. the player's name).", "error")
    else:
        conn = get_db()
        conn.execute(
            "INSERT INTO throwing_entries (entry_date, message, team_id, location, details) VALUES (?, ?, ?, ?, ?)",
            (entry_date, message, team_id, location, details),
        )
        conn.commit()
        conn.close()
        flash("Added to the calendar.", "success")

    year, month = entry_date.split("-")[0], entry_date.split("-")[1]
    return redirect(url_for("lesson_calendar", year=int(year), month=int(month), team=team_id))


@app.route("/calendar/<int:entry_id>/delete", methods=["POST"])
def delete_calendar_entry(entry_id):
    conn = get_db()
    entry = conn.execute("SELECT * FROM throwing_entries WHERE id = ?", (entry_id,)).fetchone()
    if entry:
        conn.execute("DELETE FROM throwing_entries WHERE id = ?", (entry_id,))
        conn.commit()
    conn.close()

    if entry:
        year, month = entry["entry_date"].split("-")[0], entry["entry_date"].split("-")[1]
        flash("Removed from the calendar.", "success")
        return redirect(url_for("lesson_calendar", year=int(year), month=int(month), team=entry["team_id"]))
    return redirect(url_for("lesson_calendar"))


# ---------- Routes: comments ----------

@app.route("/players/<int:player_id>/comments/add", methods=["POST"])
def add_player_comment(player_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
        abort(404)

    conn.execute(
        "INSERT INTO comments (player_id, video_id, commenter_name, body) VALUES (?, NULL, ?, ?)",
        (player_id, commenter_name, body),
    )
    conn.commit()
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + "#feedback")


@app.route("/videos/<int:video_id>/comments/add", methods=["POST"])
def add_video_comment(video_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        conn.close()
        abort(404)

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        conn.close()
        return redirect(url_for("player_detail", player_id=video["player_id"]) + f"#video-{video_id}")

    conn.execute(
        "INSERT INTO comments (player_id, video_id, commenter_name, body) VALUES (?, ?, ?, ?)",
        (video["player_id"], video_id, commenter_name, body),
    )
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


@app.route("/comments/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    conn = get_db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if not comment:
        conn.close()
        abort(404)

    player_id = comment["player_id"]
    video_id = comment["video_id"]

    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment deleted.", "success")

    if video_id:
        return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")
    return redirect(url_for("player_detail", player_id=player_id) + "#feedback")


# ---------- Routes: CSV stat upload ----------

@app.route("/upload/csv", methods=["GET", "POST"])
def upload_csv():
    conn = get_db()
    players = conn.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC").fetchall()

    if request.method == "POST":
        file = request.files.get("csv_file")
        category = request.form.get("category", "").strip()
        if category == "Other":
            category = request.form.get("category_other", "").strip()
        category = category or "General"

        if not file or not file.filename:
            flash("Please choose a CSV file to upload.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        if not allowed_file(file.filename, ALLOWED_CSV_EXT):
            flash("File must be a .csv", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        # Build a name -> id lookup (case-insensitive)
        name_to_id = {p["name"].strip().lower(): p["id"] for p in players}

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        reader = csv.DictReader(stream)

        if not reader.fieldnames:
            flash("Couldn't read any columns from that CSV.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        # Identify the player and date columns (case-insensitive match)
        fieldnames = reader.fieldnames
        lower_map = {f.lower().strip(): f for f in fieldnames}
        player_col = lower_map.get("player") or lower_map.get("name")
        date_col = lower_map.get("date")

        if not player_col:
            flash("CSV needs a 'Player' (or 'Name') column so rows can be matched to your roster.", "error")
            conn.close()
            return redirect(url_for("upload_csv"))

        stat_cols = [f for f in fieldnames if f not in (player_col, date_col)]

        # If the CSV has separate Strikes and (Total) Pitches columns but no
        # explicit Strike % column, compute Strike % per row automatically.
        strikes_col = next((c for c in stat_cols if normalize_col(c) in STRIKES_COL_NAMES), None)
        pitches_col = next((c for c in stat_cols if normalize_col(c) in PITCHES_COL_NAMES), None)
        has_explicit_strike_pct = any(normalize_col(c) in STRIKE_PCT_COL_NAMES for c in stat_cols)
        auto_strike_pct = bool(strikes_col and pitches_col and not has_explicit_strike_pct)

        # ERA and K/7 are scaled to a 7-inning high school game (not the
        # MLB's 9), so they're always computed from IP + ER / IP + K rather
        # than trusted from a pre-computed column, which might use the wrong
        # basis. If the CSV also has a raw ERA column, drop it in favor of
        # the one we compute so there's no conflicting duplicate.
        ip_col = next((c for c in stat_cols if normalize_col(c) in IP_COL_NAMES), None)
        er_col = next((c for c in stat_cols if normalize_col(c) in ER_COL_NAMES), None)
        era_col = next((c for c in stat_cols if normalize_col(c) in ERA_COL_NAMES), None)
        k_col = next((c for c in stat_cols if normalize_col(c) in K_COL_NAMES), None)

        auto_era = bool(ip_col and er_col)
        auto_k7 = bool(ip_col and k_col)

        if auto_era and era_col:
            stat_cols = [c for c in stat_cols if c != era_col]

        rows_imported = 0
        rows_skipped = 0
        unmatched_players = set()
        source_file = secure_filename(file.filename)
        # One shared timestamp for every row in this upload, computed once in
        # Python (not via SQL's per-row datetime('now')) so the whole batch
        # can be reliably grouped and deleted together later from Manage Uploads.
        import_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for row in reader:
            raw_name = (row.get(player_col) or "").strip()
            if not raw_name:
                continue
            player_id = name_to_id.get(raw_name.lower())
            if not player_id:
                unmatched_players.add(raw_name)
                rows_skipped += 1
                continue

            entry_date = parse_date(row.get(date_col)) if date_col else datetime.today().strftime("%Y-%m-%d")

            any_stat = False
            for col in stat_cols:
                raw_val = row.get(col)
                if is_blank(raw_val):
                    continue
                cleaned = raw_val.strip().replace("%", "").replace(",", "")
                try:
                    value = float(cleaned)
                except ValueError:
                    continue
                conn.execute(
                    """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (player_id, entry_date, category, col.strip(), value, source_file, import_timestamp),
                )
                any_stat = True

            if auto_strike_pct:
                strikes_raw = row.get(strikes_col)
                pitches_raw = row.get(pitches_col)
                if not is_blank(strikes_raw) and not is_blank(pitches_raw):
                    try:
                        strikes_val = float(strikes_raw.strip())
                        pitches_val = float(pitches_raw.strip())
                        if pitches_val > 0:
                            strike_pct = round(strikes_val / pitches_val * 100, 1)
                            conn.execute(
                                """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (player_id, entry_date, category, "Strike %", strike_pct, source_file, import_timestamp),
                            )
                            any_stat = True
                    except ValueError:
                        pass

            if auto_era or auto_k7:
                ip_raw = row.get(ip_col)
                if not is_blank(ip_raw):
                    try:
                        ip_val = parse_innings_pitched(ip_raw)
                    except ValueError:
                        ip_val = None

                    if ip_val and ip_val > 0:
                        if auto_era:
                            er_raw = row.get(er_col)
                            if not is_blank(er_raw):
                                try:
                                    er_val = float(er_raw.strip())
                                    era_val = round(er_val / ip_val * INNINGS_PER_GAME, 2)
                                    conn.execute(
                                        """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                        (player_id, entry_date, category, "ERA", era_val, source_file, import_timestamp),
                                    )
                                    any_stat = True
                                except ValueError:
                                    pass

                        if auto_k7:
                            k_raw = row.get(k_col)
                            if not is_blank(k_raw):
                                try:
                                    k_val = float(k_raw.strip())
                                    k7_val = round(k_val / ip_val * INNINGS_PER_GAME, 2)
                                    conn.execute(
                                        """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                        (player_id, entry_date, category, "K/7", k7_val, source_file, import_timestamp),
                                    )
                                    any_stat = True
                                except ValueError:
                                    pass

            if any_stat:
                rows_imported += 1

        conn.commit()
        conn.close()

        msg = f"Imported stats from {rows_imported} row(s)."
        if rows_skipped:
            msg += f" Skipped {rows_skipped} row(s) with unrecognized players: {', '.join(sorted(unmatched_players))}."
        flash(msg, "success" if rows_imported else "error")
        return redirect(url_for("upload_csv"))

    conn.close()
    return render_template(
        "upload_csv.html", players=players, category_options=CATEGORY_OPTIONS, pitch_types=PITCH_TYPES
    )


# ---------- Routes: TrackMan import ----------

@app.route("/upload/trackman", methods=["GET", "POST"])
def upload_trackman():
    conn = get_db()
    players = conn.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC").fetchall()

    if request.method == "POST":
        file = request.files.get("trackman_file")
        category = request.form.get("category", "").strip() or "Bullpen"
        if category not in TRACKMAN_CATEGORY_OPTIONS:
            category = "Bullpen"
        date_override = request.form.get("date_override", "").strip()

        if not file or not file.filename:
            flash("Please choose a TrackMan CSV file.", "error")
            conn.close()
            return redirect(url_for("upload_trackman"))
        if not allowed_file(file.filename, ALLOWED_CSV_EXT):
            flash("File must be a .csv (the raw TrackMan export).", "error")
            conn.close()
            return redirect(url_for("upload_trackman"))

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            flash("Couldn't read any columns from that file.", "error")
            conn.close()
            return redirect(url_for("upload_trackman"))

        # TrackMan column names vary slightly by version; match loosely.
        norm_map = {normalize_col(f): f for f in reader.fieldnames}
        pitcher_col = norm_map.get("pitcher") or norm_map.get("pitchername")
        date_col = norm_map.get("date") or norm_map.get("gamedate") or norm_map.get("utcdate")
        type_col = norm_map.get("taggedpitchtype") or norm_map.get("autopitchtype") or norm_map.get("pitchtype")
        call_col = norm_map.get("pitchcall")
        velo_col = norm_map.get("relspeed") or norm_map.get("pitchvelo") or norm_map.get("velocity")
        spin_col = norm_map.get("spinrate")
        pitchno_col = norm_map.get("pitchno") or norm_map.get("pitchnumber")
        axis_col = norm_map.get("tilt") or norm_map.get("spinaxis")
        ivb_col = norm_map.get("inducedvertbreak") or norm_map.get("ivb")
        hb_col = norm_map.get("horzbreak") or norm_map.get("horizontalbreak") or norm_map.get("hb")
        relh_col = norm_map.get("relheight")
        rels_col = norm_map.get("relside")
        ext_col = norm_map.get("extension")
        vaa_col = norm_map.get("vertapprangle")
        loch_col = norm_map.get("platelocheight")
        locs_col = norm_map.get("platelocside")
        exit_col = norm_map.get("exitspeed")
        la_col = norm_map.get("angle") or norm_map.get("launchangle")

        def fnum(row, col):
            if not col:
                return None
            v = (row.get(col) or "").strip()
            if is_blank(v):
                return None
            try:
                return float(v)
            except ValueError:
                return None

        if not pitcher_col or not velo_col:
            flash("That doesn't look like a TrackMan export - couldn't find the Pitcher and RelSpeed columns.", "error")
            conn.close()
            return redirect(url_for("upload_trackman"))

        # Roster lookup - TrackMan usually writes names as "Last, First".
        name_to_id = {p["name"].strip().lower(): p["id"] for p in players}

        def match_player(raw_name):
            n = (raw_name or "").strip().lower()
            if not n:
                return None
            if n in name_to_id:
                return name_to_id[n]
            if "," in n:
                last, _, first = n.partition(",")
                flipped = f"{first.strip()} {last.strip()}"
                return name_to_id.get(flipped)
            return None

        # (player_id, date) -> aggregates
        sessions = {}
        unmatched = set()
        pitch_rows = 0

        for row in reader:
            player_id = match_player(row.get(pitcher_col))
            if not player_id:
                raw = (row.get(pitcher_col) or "").strip()
                if raw:
                    unmatched.add(raw)
                continue

            if date_override:
                entry_date = date_override
            else:
                entry_date = parse_date((row.get(date_col) or "").split(" ")[0]) if date_col else datetime.today().strftime("%Y-%m-%d")

            key = (player_id, entry_date)
            agg = sessions.setdefault(key, {"pitches": 0, "strikes": 0, "velos": {}, "spins": {}, "raw": []})
            agg["pitches"] += 1
            pitch_rows += 1

            call = normalize_col(row.get(call_col)) if call_col else ""
            if call in TRACKMAN_STRIKE_CALLS:
                agg["strikes"] += 1

            raw_type = (row.get(type_col) or "").strip() if type_col else ""
            abbr = TRACKMAN_PITCH_TYPE_MAP.get(normalize_col(raw_type))
            velo_val = fnum(row, velo_col)
            spin_val = fnum(row, spin_col)
            if abbr:
                if velo_val is not None:
                    agg["velos"].setdefault(abbr, []).append(velo_val)
                if spin_val is not None:
                    agg["spins"].setdefault(abbr, []).append(spin_val)

            # Keep the whole pitch so the player page can show the full report.
            agg["raw"].append({
                "pitch_no": int(fnum(row, pitchno_col)) if fnum(row, pitchno_col) is not None else None,
                "pitch_type": abbr or (raw_type or None),
                "pitch_call": (row.get(call_col) or "").strip() or None if call_col else None,
                "rel_speed": velo_val,
                "spin_rate": spin_val,
                "spin_axis": (row.get(axis_col) or "").strip() or None if axis_col else None,
                "ivb": fnum(row, ivb_col),
                "hb": fnum(row, hb_col),
                "rel_height": fnum(row, relh_col),
                "rel_side": fnum(row, rels_col),
                "extension": fnum(row, ext_col),
                "vaa": fnum(row, vaa_col),
                "loc_height": fnum(row, loch_col),
                "loc_side": fnum(row, locs_col),
                "exit_speed": fnum(row, exit_col),
                "launch_angle": fnum(row, la_col),
            })

        if not sessions:
            msg = "No pitches could be matched to your roster."
            if unmatched:
                msg += f" Unrecognized pitcher name(s): {', '.join(sorted(unmatched))}. Add them as players (TrackMan names like 'Last, First' are matched automatically)."
            flash(msg, "error")
            conn.close()
            return redirect(url_for("upload_trackman"))

        source_file = secure_filename(file.filename)
        import_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def insert_stat(player_id, entry_date, stat_name, value):
            conn.execute(
                """INSERT INTO stat_entries (player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (player_id, entry_date, category, stat_name, value, source_file, import_timestamp),
            )

        for (player_id, entry_date), agg in sessions.items():
            insert_stat(player_id, entry_date, "Pitches", agg["pitches"])
            insert_stat(player_id, entry_date, "Strikes", agg["strikes"])
            if agg["pitches"]:
                insert_stat(player_id, entry_date, "Strike %", round(agg["strikes"] / agg["pitches"] * 100, 1))
            for abbr, velos in agg["velos"].items():
                insert_stat(player_id, entry_date, f"{abbr} Top Velo", round(max(velos), 1))
            for abbr, spins in agg["spins"].items():
                insert_stat(player_id, entry_date, f"{abbr} Avg Spin", round(sum(spins) / len(spins)))

            # Full per-pitch detail for the TrackMan Reports section.
            for p in agg["raw"]:
                conn.execute(
                    """INSERT INTO trackman_pitches
                       (player_id, entry_date, category, pitch_no, pitch_type, pitch_call,
                        rel_speed, spin_rate, spin_axis, ivb, hb, rel_height, rel_side,
                        extension, vaa, loc_height, loc_side, exit_speed, launch_angle,
                        source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (player_id, entry_date, category, p["pitch_no"], p["pitch_type"], p["pitch_call"],
                     p["rel_speed"], p["spin_rate"], p["spin_axis"], p["ivb"], p["hb"],
                     p["rel_height"], p["rel_side"], p["extension"], p["vaa"],
                     p["loc_height"], p["loc_side"], p["exit_speed"], p["launch_angle"],
                     source_file, import_timestamp),
                )

        conn.commit()
        conn.close()

        msg = f"Imported {pitch_rows} pitches into {len(sessions)} {category} session(s)."
        if unmatched:
            msg += f" Skipped unrecognized pitcher(s): {', '.join(sorted(unmatched))}."
        flash(msg, "success")
        return redirect(url_for("upload_trackman"))

    conn.close()
    return render_template("upload_trackman.html", players=players, category_options=TRACKMAN_CATEGORY_OPTIONS)


# ---------- Routes: video upload ----------

@app.route("/upload/video", methods=["GET", "POST"])
def upload_video():
    conn = get_db()
    players = conn.execute("SELECT id, name FROM players ORDER BY name COLLATE NOCASE ASC").fetchall()

    if request.method == "POST":
        player_id = request.form.get("player_id")
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        if category == "Other":
            category = request.form.get("category_other", "").strip()
        notes = request.form.get("notes", "").strip()
        entry_date = parse_date(request.form.get("entry_date"))
        # A lesson/session can produce more than one clip (different angles,
        # multiple reps, etc.), so the file input accepts multiple files and
        # every valid one becomes its own video row sharing the same player,
        # date, title, category, and notes from this one upload.
        files = [f for f in request.files.getlist("video_file") if f and f.filename]

        if not player_id:
            flash("Please choose a player.", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        if not files:
            flash("Please choose at least one video file.", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        uploaded_count = 0
        skipped_names = []
        for file in files:
            if not allowed_file(file.filename, ALLOWED_VIDEO_EXT):
                skipped_names.append(file.filename)
                continue

            safe_name = secure_filename(file.filename)
            stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
            file.save(os.path.join(VIDEO_DIR, stored_filename))

            conn.execute(
                "INSERT INTO videos (player_id, entry_date, title, category, notes, filename) VALUES (?, ?, ?, ?, ?, ?)",
                (player_id, entry_date, title or safe_name, category, notes, stored_filename),
            )
            uploaded_count += 1

        conn.commit()
        conn.close()

        if uploaded_count:
            msg = f"Uploaded {uploaded_count} video{'s' if uploaded_count != 1 else ''}."
            if skipped_names:
                msg += f" Skipped {len(skipped_names)} file(s) with an unsupported type: {', '.join(skipped_names)}."
            flash(msg, "success")
        else:
            flash(
                f"Couldn't upload any of those files - unsupported type (need mp4, mov, m4v, webm, or avi): {', '.join(skipped_names)}.",
                "error",
            )
        return redirect(url_for("player_detail", player_id=player_id))

    conn.close()
    return render_template("upload_video.html", players=players, category_options=CATEGORY_OPTIONS)


@app.route("/videos/<int:video_id>/delete", methods=["POST"])
def delete_video(video_id):
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if video:
        try:
            os.remove(os.path.join(VIDEO_DIR, video["filename"]))
        except OSError:
            pass
        player_id = video["player_id"]
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.commit()
    else:
        player_id = None
    conn.close()
    flash("Video removed.", "success")
    if request.form.get("return_to") == "manage":
        return redirect(url_for("manage_uploads"))
    if player_id:
        return redirect(url_for("player_detail", player_id=player_id))
    return redirect(url_for("index"))


# ---------- Routes: manage uploads (delete CSV imports / videos) ----------

@app.route("/manage")
def manage_uploads():
    conn = get_db()

    import_rows = conn.execute(
        """SELECT source_file, imported_at, category,
                  COUNT(*) AS row_count,
                  COUNT(DISTINCT player_id) AS player_count,
                  MIN(entry_date) AS earliest_date,
                  MAX(entry_date) AS latest_date
           FROM stat_entries
           WHERE source_file IS NOT NULL AND source_file != ''
           GROUP BY source_file, imported_at, category
           ORDER BY imported_at DESC"""
    ).fetchall()

    # Any stat rows with no source_file (shouldn't normally happen, but covers
    # older/edge-case data) get bundled into one "manual entries" bucket per category.
    manual_rows = conn.execute(
        """SELECT category, COUNT(*) AS row_count, COUNT(DISTINCT player_id) AS player_count
           FROM stat_entries
           WHERE source_file IS NULL OR source_file = ''
           GROUP BY category"""
    ).fetchall()

    videos = conn.execute(
        """SELECT v.*, p.name AS player_name
           FROM videos v JOIN players p ON p.id = v.player_id
           ORDER BY v.entry_date DESC, v.id DESC"""
    ).fetchall()

    conn.close()
    return render_template(
        "manage.html", import_rows=import_rows, manual_rows=manual_rows, videos=videos
    )


@app.route("/imports/delete", methods=["POST"])
def delete_import():
    source_file = request.form.get("source_file", "")
    imported_at = request.form.get("imported_at", "")
    category = request.form.get("category", "")

    conn = get_db()
    cur = conn.execute(
        "DELETE FROM stat_entries WHERE source_file = ? AND imported_at = ? AND category = ?",
        (source_file, imported_at, category),
    )
    # TrackMan imports also wrote per-pitch detail rows; remove those too.
    conn.execute(
        "DELETE FROM trackman_pitches WHERE source_file = ? AND imported_at = ? AND category = ?",
        (source_file, imported_at, category),
    )
    conn.commit()
    removed = cur.rowcount
    conn.close()

    flash(f"Removed {removed} stat row(s) from {source_file}.", "success")
    return redirect(url_for("manage_uploads"))


if __name__ == "__main__":
    init_db()
    print("\nSwarm Baseball Progress Tracker")
    print("Open http://127.0.0.1:5000 in your browser. Press Ctrl+C to stop.")
    print("First visit? You'll be asked to create your admin account.\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
else:
    init_db()
