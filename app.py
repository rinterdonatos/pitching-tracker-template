import os
import re
import csv
import io
import base64
import math
import random
import secrets
import smtplib
import colorsys
import tempfile
import traceback
import urllib.parse
import urllib.request
from email.message import EmailMessage
import calendar as calendar_module
import sqlite3
from datetime import datetime, timedelta, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, session, Response, g
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

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


def send_reset_email(to_addr, code, org_name=None):
    # Every organization on the platform shares this one function - it must
    # say who the code is actually for, not a hardcoded org name left over
    # from before this app went multi-tenant.
    label = org_name or PLATFORM_NAME
    msg = EmailMessage()
    msg["Subject"] = f"{label} password reset code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(
        f"Your {label} password reset code is: {code}\n\n"
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


def send_reset_sms(to_phone, code, org_name=None):
    label = org_name or PLATFORM_NAME
    to_number = f"+1{to_phone}" if len(to_phone) == 10 else f"+{to_phone}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "To": to_number,
        "From": TWILIO_FROM,
        "Body": f"{label} password reset code: {code} (expires in {RESET_CODE_MINUTES} min)",
    }).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send_invite_email(to_addr, org_name, login_url):
    msg = EmailMessage()
    msg["Subject"] = f"You've been added to {org_name}"
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(
        f"You've been added to {org_name} on {PLATFORM_NAME}.\n\n"
        f"Sign in here: {login_url}\n"
        "Enter this email (or your phone number) and leave the password field blank - "
        "you'll be prompted to create one."
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False


def send_invite_sms(to_phone, org_name, login_url):
    to_number = f"+1{to_phone}" if len(to_phone) == 10 else f"+{to_phone}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = urllib.parse.urlencode({
        "To": to_number,
        "From": TWILIO_FROM,
        "Body": f"You've been added to {org_name} on {PLATFORM_NAME}. Sign in and set your password: {login_url}",
    }).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send_platform_admin_invite_email(to_addr, name, login_url):
    msg = EmailMessage()
    msg["Subject"] = f"You've been invited as a {PLATFORM_NAME} platform admin"
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(
        f"Hi{f' {name}' if name else ''},\n\n"
        f"You've been added as a platform admin on {PLATFORM_NAME}, with the same "
        "cross-org access as everyone else on that team.\n\n"
        f"Sign in here: {login_url}\n"
        "Enter this email and leave the password field blank - you'll be prompted "
        "to create your own password."
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The database lives wherever PHX_DATA_DIR points - in production that's a
# mounted persistent disk (e.g. Render's /var/data), so it survives
# redeploys instead of being wiped along with the rest of the container.
# Left unset, it defaults next to app.py like before - local dev unaffected.
DATA_DIR = os.environ.get("PHX_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "pvtracker.db")

# Uploaded videos/photos/logos stay under static/uploads on purpose - Flask
# serves them straight through its built-in /static/ route (see every
# `url_for('static', filename='uploads/...')` in the templates), so moving
# this elsewhere would break every video, photo, and logo on the site. To
# persist these across redeploys, mount the same persistent disk directly at
# this path (static/uploads) in Render's dashboard instead of introducing a
# second storage location here.
VIDEO_DIR = os.path.join(BASE_DIR, "static", "uploads", "videos")
PHOTO_DIR = os.path.join(BASE_DIR, "static", "uploads", "photos")
LOGO_DIR = os.path.join(BASE_DIR, "static", "uploads", "logos")

ALLOWED_VIDEO_EXT = {
    "mp4", "mov", "m4v", "webm", "avi", "mkv", "wmv", "flv",
    "3gp", "3g2", "mpg", "mpeg", "m2ts", "mts", "ogv",
}
ALLOWED_PHOTO_EXT = {"png", "jpg", "jpeg", "gif"}
# Team logos are uploaded straight to R2 with no Pillow processing (unlike
# an org's own primary logo, which needs Pillow to extract theme colors and
# so has to stick to formats Pillow can actually open) - so there's no
# reason to reject the HEIC/WEBP files a phone camera produces by default.
ALLOWED_TEAM_LOGO_EXT = ALLOWED_PHOTO_EXT | {"webp", "heic", "heif"}
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

# Extra per-player text fields: player contact info, recruiting profile
# links (Perfect Game / Prep Baseball Report), and social links (Twitter/X,
# Instagram). Shared by the DB migration and the add/edit player forms. Other
# people connected to the player (coaches, trainers, parents, ...) live in
# the player_contacts table instead, so a player can have any number of them.
PLAYER_CONTACT_FIELDS = ["phone", "email", "pg_url", "pbr_url", "twitter_url", "instagram_url"]

# Recruiting measurables a player/parent can fill in themselves - shown on
# the profile and the printable Recruiting Report resume.
PLAYER_MEASURABLE_FIELDS = [
    "height", "weight", "bats", "throws", "sixty_time",
    "gpa", "sat_score", "act_score", "intended_major",
]
PLAYER_MEASURABLE_LABELS = {
    "height": "Height",
    "weight": "Weight",
    "bats": "Bats",
    "throws": "Throws",
    "sixty_time": "60-Yard Dash",
    "gpa": "GPA",
    "sat_score": "SAT",
    "act_score": "ACT",
    "intended_major": "Intended Major",
}

app = Flask(__name__)
app.secret_key = os.environ.get("PHX_SECRET_KEY", "phoenix-pitching-lab-tracker")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB max upload (videos)
app.permanent_session_lifetime = timedelta(days=365)

# Render terminates TLS at its own reverse proxy before traffic ever reaches
# gunicorn, so without this, Flask would see every request as plain http and
# secure-only cookies below would silently never get set.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Render sets RENDER=true on every deployed service automatically - this
# tells "running in production" apart from "running on someone's laptop via
# `python app.py`" without needing yet another env var to remember to set.
IS_PRODUCTION = bool(os.environ.get("RENDER"))
app.config.update(
    SESSION_COOKIE_SECURE=IS_PRODUCTION,  # only send the session cookie over https
    SESSION_COOKIE_HTTPONLY=True,         # never readable from JS
    SESSION_COOKIE_SAMESITE="Lax",
    PREFERRED_URL_SCHEME="https" if IS_PRODUCTION else "http",
)

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(PHOTO_DIR, exist_ok=True)
os.makedirs(LOGO_DIR, exist_ok=True)


def static_url(filename):
    """Cache-busted static asset URL: appends the file's last-modified time
    as a query string, so browsers fetch a fresh copy the moment a deploy
    changes style.css/app.js instead of serving a stale cached version until
    someone remembers to hard-refresh."""
    path = os.path.join(app.static_folder, filename)
    try:
        version = int(os.path.getmtime(path))
    except OSError:
        version = 0
    return url_for("static", filename=filename) + f"?v={version}"


app.jinja_env.globals["static_url"] = static_url

# Neutral platform brand - used only on pages that aren't scoped to any one
# organization (the public marketing landing page, the self-serve org signup
# flow, and the cross-org "choose your organization" login picker). This is
# a placeholder name and can be swapped for a real brand later by changing
# this one constant (plus static/img/default-badge.svg, which doubles as the
# placeholder platform logo).
PLATFORM_NAME = "Mound HQ"
app.jinja_env.globals["platform_name"] = PLATFORM_NAME
app.jinja_env.globals["current_year"] = lambda: datetime.now().year

# Contact address shown on the Terms/Privacy pages - falls back to whatever
# the SMTP "from" address is configured as, then to a placeholder, so this
# never renders blank even on a fresh install with no env vars set yet.
SUPPORT_EMAIL = os.environ.get("PHX_SUPPORT_EMAIL", "") or SMTP_FROM or "support@moundhq.com"
app.jinja_env.globals["support_email"] = SUPPORT_EMAIL


# ---------- Database helpers ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------- Database backups (Cloudflare R2) ----------
#
# A standalone Render Cron Job can't reach this service's persistent disk
# directly - Render disks are attached to exactly one service - so rather
# than run the backup as its own separate job, the cron job just pings this
# token-protected endpoint on a schedule, and the actual backup happens here,
# inside the web service that already has the disk mounted. sqlite3's own
# backup() API is used instead of a raw file copy so a snapshot taken while
# the app is mid-write still comes out consistent rather than corrupted.

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "")
BACKUP_TOKEN = os.environ.get("PHX_BACKUP_TOKEN", "")

# One-time bootstrap for creating a platform-owner login (see
# create_platform_admin below) - separate secret from the backup token so
# the two aren't interchangeable.
OWNER_TOKEN = os.environ.get("PHX_OWNER_TOKEN", "")
BACKUP_RETENTION = 30  # keep the last 30 nightly snapshots, prune anything older

# ---------------------------------------------------------------------------
# Payments (Stripe). Optional - if STRIPE_SECRET_KEY isn't set, billing
# simply isn't offered: orgs sign up and use the app for free, same as
# before this feature existed, and every billing route/page quietly
# no-ops or hides itself. Once you're ready to charge for it:
#   1. Create a Stripe account (stripe.com) and, while testing, stay in
#      test mode (toggle in the Stripe dashboard).
#   2. Run stripe_setup.py with your Stripe secret key to create
#      the Starter/Growth/Pro products and their monthly+annual+overage
#      prices - it prints the exact env vars below.
#   3. Set these environment variables (Render: Environment tab):
#        STRIPE_SECRET_KEY=sk_test_... (sk_live_... when you go live)
#        STRIPE_PUBLISHABLE_KEY=pk_test_...
#        STRIPE_WEBHOOK_SECRET=whsec_...   (from the webhook you add in
#          the Stripe dashboard, pointed at https://yourdomain/webhooks/stripe,
#          listening for checkout.session.completed, customer.subscription.*)
#        STRIPE_PRICE_STARTER_MONTHLY / _ANNUAL / _OVERAGE
#        STRIPE_PRICE_GROWTH_MONTHLY / _ANNUAL / _OVERAGE
#        STRIPE_PRICE_PRO_MONTHLY / _ANNUAL
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
BILLING_ENABLED = bool(STRIPE_SECRET_KEY)
# Off by default - trials here are something you grant by hand to specific
# orgs (Platform Admin -> an org -> "Grant Free Trial"), not something every
# signup gets automatically. Set STRIPE_TRIAL_DAYS to a positive number only
# if you want every new subscription to include that many free days at
# checkout instead (Stripe still collects a card, it just doesn't bill it
# until the trial ends).
TRIAL_DAYS = int(os.environ.get("STRIPE_TRIAL_DAYS", "0"))

if BILLING_ENABLED:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

# Mirrors the tiers on the public /pricing page exactly - cap is the number
# of players included before per-player overage kicks in (None = unlimited).
PLANS = {
    "starter": {
        "label": "Starter",
        "cap": 20,
        "monthly_price_id": os.environ.get("STRIPE_PRICE_STARTER_MONTHLY", ""),
        "annual_price_id": os.environ.get("STRIPE_PRICE_STARTER_ANNUAL", ""),
        "overage_price_id": os.environ.get("STRIPE_PRICE_STARTER_OVERAGE", ""),
        "overage_amount": 6,
        "monthly_amount": 80,
        "annual_amount": 799,
    },
    "growth": {
        "label": "Growth",
        "cap": 40,
        "monthly_price_id": os.environ.get("STRIPE_PRICE_GROWTH_MONTHLY", ""),
        "annual_price_id": os.environ.get("STRIPE_PRICE_GROWTH_ANNUAL", ""),
        "overage_price_id": os.environ.get("STRIPE_PRICE_GROWTH_OVERAGE", ""),
        "overage_amount": 5,
        "monthly_amount": 150,
        "annual_amount": 1499,
    },
    "pro": {
        "label": "Pro",
        "cap": None,
        "monthly_price_id": os.environ.get("STRIPE_PRICE_PRO_MONTHLY", ""),
        "annual_price_id": os.environ.get("STRIPE_PRICE_PRO_ANNUAL", ""),
        "overage_price_id": "",
        "overage_amount": 0,
        "monthly_amount": 295,
        "annual_amount": 2999,
    },
}


def org_player_count(conn, org_id):
    return conn.execute("SELECT COUNT(*) FROM players WHERE organization_id = ?", (org_id,)).fetchone()[0]


def org_plan_cap(org):
    """Max players included in an org's plan, or None if unlimited - which
    covers both the Pro plan and, just as importantly, every org that has
    never subscribed at all. Billing being optional means an org with no
    plan on file must never get retroactively capped."""
    if not org or not org["plan"] or org["subscription_status"] not in ("active", "trialing", "past_due"):
        return None
    return PLANS.get(org["plan"], {}).get("cap")


def org_trial_days_left(org):
    """Days left on a hand-granted trial (see trial_ends_at above), or None
    if the org isn't on one. Never negative - once it's passed, it's just
    not a trial anymore."""
    if not org or not org["trial_ends_at"]:
        return None
    try:
        ends = datetime.strptime(org["trial_ends_at"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    days_left = (ends - datetime.now()).days
    return max(0, days_left) if ends > datetime.now() else None


def sync_overage_quantity(org):
    """Tell Stripe how many players an org is over its plan's cap, so the
    next invoice bills the right number of extra-player line items. Safe to
    call after every roster change - a no-op for orgs with no active
    subscription, no overage item (Pro has no cap to be over), or billing
    disabled entirely."""
    if not BILLING_ENABLED or not org or not org["stripe_subscription_id"] or not org["overage_item_id"]:
        return
    cap = PLANS.get(org["plan"] or "", {}).get("cap")
    if cap is None:
        return
    conn = get_db()
    count = org_player_count(conn, org["id"])
    conn.close()
    try:
        stripe.SubscriptionItem.modify(org["overage_item_id"], quantity=max(0, count - cap))
    except Exception:
        app.logger.exception("Failed to sync Stripe overage quantity for org %s", org["id"])


def _apply_subscription_to_org(conn, org_id, customer_id, subscription_id, subscription_obj=None):
    """Single source of truth for writing a Stripe subscription's state onto
    an organizations row - called from the webhook for both the initial
    checkout completion and any later plan/status change, so the two paths
    can never drift out of sync with each other."""
    sub = subscription_obj or stripe.Subscription.retrieve(subscription_id)
    plan_key = (sub.get("metadata") or {}).get("plan", "")
    interval = (sub.get("metadata") or {}).get("interval", "monthly")
    plan_conf = PLANS.get(plan_key, {})
    overage_item_id = None
    for item in sub["items"]["data"]:
        if plan_conf.get("overage_price_id") and item["price"]["id"] == plan_conf["overage_price_id"]:
            overage_item_id = item["id"]
    conn.execute(
        """UPDATE organizations SET stripe_customer_id = ?, stripe_subscription_id = ?, plan = ?,
           billing_interval = ?, subscription_status = ?, overage_item_id = ? WHERE id = ?""",
        (customer_id, subscription_id, plan_key or None, interval, sub.get("status"), overage_item_id, org_id),
    )
    conn.commit()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    sync_overage_quantity(org)


def r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


@app.route("/internal/backup-db", methods=["POST"])
@csrf.exempt  # authenticated with X-Backup-Token, not a session - there's no
              # browser involved to have a CSRF token in the first place.
def backup_db():
    """Snapshot the live database and ship it to R2. Called only by the
    scheduled cron job (see trigger_backup.py), authenticated with a shared
    secret rather than a login since there's no human on the other end."""
    if not BACKUP_TOKEN or request.headers.get("X-Backup-Token") != BACKUP_TOKEN:
        abort(403)
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME):
        return {"ok": False, "error": "R2 isn't configured (missing env vars)"}, 500

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    key = f"backups/pvtracker-{stamp}.db"

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()

        client = r2_client()
        client.upload_file(tmp_path, R2_BUCKET_NAME, key)

        # Prune anything past the retention window so storage cost doesn't
        # grow forever.
        existing = client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix="backups/pvtracker-")
        objects = sorted(existing.get("Contents", []), key=lambda o: o["Key"])
        stale = objects[:-BACKUP_RETENTION] if len(objects) > BACKUP_RETENTION else []
        for obj in stale:
            client.delete_object(Bucket=R2_BUCKET_NAME, Key=obj["Key"])

        return {"ok": True, "key": key, "pruned": len(stale)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@app.route("/internal/create-platform-admin", methods=["POST"])
@csrf.exempt  # authenticated with X-Owner-Token, not a session - same reasoning as backup_db.
def create_platform_admin():
    """One-time bootstrap for the actual platform owner (you, running the
    whole product) to create your own standalone login - completely
    separate from organizations/users, since running MoundHQ isn't "being
    an organization." Token-protected the same way /internal/backup-db is:
    there's no logged-in platform admin yet the first time this gets
    called, so a session check can't be the gate. Safe to call again later
    to add a second platform admin (e.g. a co-founder)."""
    if not OWNER_TOKEN or request.headers.get("X-Owner-Token") != OWNER_TOKEN:
        abort(403)
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not name or not email or not password:
        return {"ok": False, "error": "Missing 'name', 'email', or 'password'."}, 400
    if len(password) < 8:
        return {"ok": False, "error": "Password must be at least 8 characters."}, 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO platform_admins (name, email, password_hash) VALUES (?, ?, ?)",
            (name, email, generate_password_hash(password)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"ok": False, "error": f"A platform admin with email {email} already exists."}, 409
    conn.close()
    return {"ok": True, "email": email}


@app.route("/platform/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def platform_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        admin = conn.execute("SELECT * FROM platform_admins WHERE lower(email) = ?", (email,)).fetchone()
        conn.close()
        if not admin:
            flash("Incorrect email or password.", "error")
            return redirect(url_for("platform_login", next=request.args.get("next", "")))

        # Invited but hasn't created a password yet -> send them to do that,
        # same pattern organization users already use.
        if not admin["password_hash"]:
            session["pending_platform_admin_id"] = admin["id"]
            return redirect(url_for("platform_set_password", next=request.form.get("next", "")))

        if not check_password_hash(admin["password_hash"], password):
            flash("Incorrect email or password.", "error")
            return redirect(url_for("platform_login", next=request.args.get("next", "")))
        session.clear()
        session.permanent = True
        session["platform_admin_id"] = admin["id"]
        session["platform_admin_name"] = admin["name"]
        flash(f"Welcome back, {admin['name']}.", "success")
        next_url = request.form.get("next") or url_for("platform_organizations")
        return redirect(next_url)
    return render_template("platform_login.html", next=request.args.get("next", ""))


@app.route("/platform/set-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def platform_set_password():
    pending_id = session.get("pending_platform_admin_id")
    if not pending_id:
        return redirect(url_for("platform_login"))

    conn = get_db()
    admin = conn.execute("SELECT * FROM platform_admins WHERE id = ?", (pending_id,)).fetchone()
    if not admin or admin["password_hash"]:
        conn.close()
        session.pop("pending_platform_admin_id", None)
        return redirect(url_for("platform_login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            flash("Password needs at least 8 characters.", "error")
            conn.close()
            return redirect(url_for("platform_set_password", next=request.form.get("next", "")))
        if password != confirm:
            flash("Those passwords don't match.", "error")
            conn.close()
            return redirect(url_for("platform_set_password", next=request.form.get("next", "")))

        conn.execute(
            "UPDATE platform_admins SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), pending_id),
        )
        conn.commit()
        conn.close()

        session.pop("pending_platform_admin_id", None)
        session.clear()
        session.permanent = True
        session["platform_admin_id"] = admin["id"]
        session["platform_admin_name"] = admin["name"]
        flash(f"Welcome, {admin['name']}.", "success")
        next_url = request.form.get("next") or url_for("platform_organizations")
        return redirect(next_url)

    conn.close()
    return render_template("platform_set_password.html", admin=admin, next=request.args.get("next", ""))


@app.route("/platform/logout", methods=["POST"])
def platform_logout():
    session.pop("platform_admin_id", None)
    session.pop("platform_admin_name", None)
    flash("Signed out.", "success")
    return redirect(url_for("platform_login"))


# ---------- Uploaded media (Cloudflare R2) ----------
#
# Player photos, videos, and org logos are uploaded straight to a public R2
# bucket instead of the local disk - a persistent disk works fine for the
# database, but every GB of video anyone uploads would otherwise sit on that
# same paid disk and can't be shared across multiple app instances later.
# This is a separate bucket (and separate credentials) from the one the
# nightly backup job writes to on purpose: that bucket holds full database
# snapshots and must stay private, while this one is deliberately public so
# a plain <img>/<video> tag can point straight at it without the Flask app
# having to proxy every byte through itself.
R2_MEDIA_BUCKET_NAME = os.environ.get("R2_MEDIA_BUCKET_NAME", "")
R2_MEDIA_ACCESS_KEY_ID = os.environ.get("R2_MEDIA_ACCESS_KEY_ID", "")
R2_MEDIA_SECRET_ACCESS_KEY = os.environ.get("R2_MEDIA_SECRET_ACCESS_KEY", "")
# Same Cloudflare account as the backup bucket unless overridden.
R2_MEDIA_ACCOUNT_ID = os.environ.get("R2_MEDIA_ACCOUNT_ID", "") or R2_ACCOUNT_ID
# Public base URL for the bucket - either its free https://pub-xxxx.r2.dev
# subdomain, or a custom domain (e.g. https://cdn.moundhq.com). No trailing
# slash.
R2_MEDIA_PUBLIC_URL = os.environ.get("R2_MEDIA_PUBLIC_URL", "").rstrip("/")

MEDIA_ENABLED = bool(
    R2_MEDIA_BUCKET_NAME and R2_MEDIA_ACCESS_KEY_ID and R2_MEDIA_SECRET_ACCESS_KEY
    and R2_MEDIA_ACCOUNT_ID and R2_MEDIA_PUBLIC_URL
)


def r2_media_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_MEDIA_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_MEDIA_ACCESS_KEY_ID,
        aws_secret_access_key=R2_MEDIA_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload_media(fileobj, key, content_type=None):
    """Upload one already-open file-like object to the public media bucket
    under `key` (e.g. "uploads/videos/20260823_foo.mp4")."""
    extra = {"ContentType": content_type} if content_type else {}
    fileobj.seek(0)
    r2_media_client().upload_fileobj(fileobj, R2_MEDIA_BUCKET_NAME, key, ExtraArgs=extra)


def delete_media(key):
    if not (MEDIA_ENABLED and key):
        return
    try:
        r2_media_client().delete_object(Bucket=R2_MEDIA_BUCKET_NAME, Key=key)
    except Exception:
        pass  # best-effort - a stray orphaned object in R2 isn't worth failing the request over


def media_url(key):
    """Public URL for something stored in the media bucket. `key` is the
    same relative path used everywhere else, e.g. "uploads/photos/x.jpg"."""
    if not key:
        return ""
    return f"{R2_MEDIA_PUBLIC_URL}/{key}"


app.jinja_env.globals["media_url"] = media_url


# ---------- Multi-tenant: organization resolution from the URL ----------
#
# Every route lives under /<org_slug>/... except a small always-global set
# (below) - static files, the one-time setup wizard, invite links, and the
# calendar subscription feed, all of which either predate having an org to
# scope to or already carry their own unguessable token so an org_slug in
# the URL wouldn't add anything.
#
# Rather than hand-editing every one of the ~200 url_for() calls already
# spread across the templates, Flask's url_value_preprocessor/url_defaults
# hooks do this transparently: the preprocessor strips org_slug off the
# incoming URL and resolves it to an organization row on `g.org`, and
# url_defaults injects org_slug back into any url_for() call that needs it,
# pulling it from `g.org` unless a different one was passed explicitly. This
# is the same pattern Flask's own docs use for internationalized URLs like
# /<lang_code>/... - every existing url_for("index") call keeps working
# unchanged because the endpoint name itself never changes, only the URL
# pattern behind it.
ORG_EXEMPT_ENDPOINTS = {
    "static", "landing", "home", "org_picker", "setup", "start", "join", "calendar_feed",
    "terms", "privacy", "robots_txt", "sitemap_xml",
    # Machine-to-machine only, authenticated with its own shared-secret
    # token (see backup_db) rather than a user session - there's no human
    # login involved, so it must never be routed through the login redirect.
    "backup_db", "create_platform_admin", "stripe_webhook",
    # Platform admin's own login - has to be reachable while logged out,
    # same as the org login/coach login routes below.
    "platform_login", "platform_logout", "platform_set_password",
    # College-recruiting coach portal: coaches aren't members of any one
    # organization - they browse opted-in players across all of them - so
    # these routes live outside the /<org_slug>/ scheme entirely and use
    # their own separate login (coach_required / platform_admin_required
    # below), not the team-user session this before_request hook checks.
    "coach_signup", "coach_login", "coach_logout", "coach_players",
    "coach_leaderboards", "coach_feed", "coach_favorite_toggle", "coach_favorites",
    "coach_player_detail", "coach_player_follow_toggle", "coach_following",
    "platform_coaches", "platform_approve_coach", "platform_reject_coach", "platform_revoke_coach",
    # Cross-org owner dashboard - same reasoning as the coach-approval routes
    # above: gated by platform_admin_required (a session flag, not tied to
    # any one org), and deliberately not part of the /<org_slug>/ scheme so
    # one org's data is never confused with another's.
    "platform_organizations", "platform_org_detail", "platform_grant_trial", "platform_revoke_trial",
    "platform_save_team", "platform_delete_team",
    "platform_delete_player", "platform_verify_video", "platform_delete_video", "platform_enter_org",
    "platform_delete_org",
    "platform_admins_page", "platform_admin_add", "platform_admin_resend_invite", "platform_admin_delete",
}


@app.url_value_preprocessor
def pull_org_slug(endpoint, values):
    g.org = None
    if endpoint in ORG_EXEMPT_ENDPOINTS or values is None or "org_slug" not in values:
        return
    slug = values.pop("org_slug")
    conn = get_db()
    g.org = conn.execute("SELECT * FROM organizations WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    if g.org is None:
        abort(404)


@app.url_defaults
def add_org_slug(endpoint, values):
    if endpoint in ORG_EXEMPT_ENDPOINTS or "org_slug" in values:
        return
    if app.url_map.is_endpoint_expecting(endpoint, "org_slug"):
        org = getattr(g, "org", None)
        if org is not None:
            values["org_slug"] = org["slug"]


# Default theme for an org that hasn't uploaded a logo (or whose logo didn't
# yield usable colors) - the same neutral gray every non-Swarm org has always
# gotten.
DEFAULT_ORG_THEME = {
    "blue": "#52525b", "blue_dark": "#3f3f46",
    "orange": "#d4d4d8", "orange_dark": "#a1a1aa",
}


def darken_hex(hex_color, factor=0.72):
    """Darken a #rrggbb color by `factor` (0-1) for hover/active CSS variants -
    mirrors the blue/blue-dark, orange/orange-dark pairing every theme in
    this app already uses."""
    hex_color = (hex_color or "").lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return f"#{hex_color}"
    r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def extract_theme_colors(image_path):
    """Pick a primary + accent color from an uploaded logo, so a new org's
    site can look like *their* brand instead of the generic gray default.
    Returns (primary_hex, accent_hex) or None if Pillow isn't available, the
    file isn't a readable image, or nothing usable comes out of it - any of
    which just means the org falls back to the default gray theme, never a
    crash on signup."""
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        img = Image.open(image_path).convert("RGBA")
        # Flatten transparency onto white first - otherwise a transparent
        # logo background gets counted as black after the RGB conversion
        # below, which would very often "win" as the dominant color.
        flattened = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(flattened, img).convert("RGB")
        img.thumbnail((150, 150))

        # A small palette quantized with PIL's default median-cut algorithm
        # bisects the image's color histogram by luminance/channel range,
        # not by hue - so two genuinely distinct brand colors that happen
        # to share similar darkness (e.g. black and a deep red) can get
        # bucketed together into one averaged "brown" swatch instead of
        # staying separate. FASTOCTREE with a much bigger target palette
        # keeps visually distinct colors apart far more reliably; the
        # clustering pass below then re-merges near-duplicate shades
        # (anti-aliased edge pixels of the same color) so their counts
        # combine without a distinct second color ever getting blended in.
        quantized = img.quantize(colors=32, method=Image.FASTOCTREE)
        palette = quantized.getpalette()
        counts = quantized.getcolors() or []
        if not counts:
            return None

        def rgb_at(idx):
            return tuple(palette[idx * 3:idx * 3 + 3])

        def luminance(rgb):
            r, val_g, b = (c / 255 for c in rgb)
            return 0.299 * r + 0.587 * val_g + 0.114 * b

        def is_near_neutral(rgb):
            # Too close to white/black/gray to read as a real brand color -
            # almost always the logo's background/outline, not an
            # intentional accent color. Very dark colors get an extra,
            # more forgiving check here: a "rich black" ink (the kind a
            # CMYK-to-RGB conversion or JPEG compression commonly produces,
            # e.g. #231F20) can carry a slight warm or cool cast that looks
            # meaningfully "saturated" in raw RGB terms even though it
            # still reads as plain black to the eye - and since black ink
            # (outlines, wordmark, background) usually covers far more of
            # a logo than its actual accent color, letting that cast slip
            # through as "vivid" lets it dominate the extracted theme by
            # sheer pixel count instead of the color that was meant to.
            lum = luminance(rgb)
            if lum > 0.88 or lum < 0.08:
                return True
            if lum < 0.22:
                r, g, b = (c / 255 for c in rgb)
                return colorsys.rgb_to_hsv(r, g, b)[1] < 0.55
            return max(rgb) - min(rgb) < 18

        swatches = sorted(
            ((rgb_at(idx), n) for n, idx in counts), key=lambda item: item[1], reverse=True
        )
        clusters = []  # [[rgb, total_count], ...]
        for rgb, n in swatches:
            match = next(
                (c for c in clusters if sum(abs(a - b) for a, b in zip(c[0], rgb)) < 40),
                None,
            )
            if match:
                match[1] += n
            else:
                clusters.append([rgb, n])
        clusters.sort(key=lambda c: c[1], reverse=True)

        vivid = [c for c in clusters if not is_near_neutral(c[0])]
        pool_pairs = vivid if vivid else clusters

        def saturation(rgb):
            r, g, b = (c / 255 for c in rgb)
            return colorsys.rgb_to_hsv(r, g, b)[1]

        # Rank by pixel count weighted toward saturation, not raw count
        # alone - a black-and-red logo can easily have more total black
        # pixels (outlines, wordmark) than red fill, but black already got
        # filtered out above as near-neutral, so this is really guarding
        # against a duller, muddier blend (a shadow/bevel/gradient between
        # two bolder design colors, or JPEG compression noise along a hard
        # edge) out-counting the actual flat, saturated brand color and
        # getting picked as primary/accent instead of it.
        ranked = sorted(pool_pairs, key=lambda item: item[1] * (0.35 + saturation(item[0])), reverse=True)
        pool = [rgb for rgb, _ in ranked]

        def looks_muddy(rgb):
            # A final sanity check on top of is_near_neutral: reject colors
            # that land in the dull brown/tan/olive zone (low-to-mid
            # saturation or value, in the orange-ish hue range) even if they
            # weren't dark enough to be caught as "near black" above. This
            # is what a player card's avatar circle or the header banner
            # would otherwise render as an unintentional muddy brown.
            r, g, b = (c / 255 for c in rgb)
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            if s < 0.22:
                return True
            hue_deg = h * 360
            return 12 <= hue_deg <= 50 and (s < 0.55 or v < 0.45)

        clean_pool = [c for c in pool if not looks_muddy(c)] or pool
        primary = clean_pool[0]
        primary_lum = luminance(primary)
        # A very light primary makes the white button/nav text unreadable -
        # darken it rather than pick a background-ish color as the theme.
        if primary_lum > 0.7:
            primary = tuple(int(c * 0.55) for c in primary)
        # A very dark primary reads as flat black once it's rendered as a
        # banner - even a deep, richly-saturated brand color (a dark
        # maroon, forest green, navy, etc.) can end up looking like plain
        # black at that low a luminance, especially once the header CSS
        # darkens it further for the gradient/hover variant. Brighten it up
        # into a legible mid-tone instead of ever letting the banner - or
        # anything else that leans on this color, like the avatar circles
        # on player cards - render as black. is_near_neutral() above
        # already keeps genuinely near-black candidates out of the running
        # in most cases, but this is the hard backstop: nothing this
        # function returns should ever be dark enough to read as black.
        elif primary_lum < 0.18:
            primary = tuple(min(255, int(c * 1.8 + 35)) for c in primary)

        accent_search = [c for c in clean_pool if c != primary] or [c for c in pool if c != primary]
        accent = next(
            (c for c in accent_search if sum(abs(a - b) for a, b in zip(c, primary)) > 90),
            None,
        )
        if accent is None:
            accent = tuple(min(255, int(c * 1.3 + 40)) for c in primary)

        def to_hex(rgb):
            return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb))

        return to_hex(primary), to_hex(accent)
    except Exception:
        return None


# Swarm Baseball keeps its own navy/gold logo and color scheme (the site was
# built around it). Every other organization gets a theme tailored to its
# own uploaded logo if it has one (extract_theme_colors, set at signup), or
# the neutral gray default otherwise - these values feed both the
# header/logo markup and a small CSS variable override in base.html so
# buttons, nav highlights, etc. all follow along automatically.
def branding_context_for_org(org):
    """The org_name/org_logo/org_theme_* dict for one organization row - the
    single source of truth for both inject_branding() (every normal
    org-scoped page) and join() (which looks its org up from an invite
    token instead of the URL, so it's outside the g.org flow entirely and
    has to build this context by hand)."""
    if org and org["slug"] != "swarm-baseball":
        if org["logo_filename"] and org["theme_primary"]:
            primary = org["theme_primary"]
            accent = org["theme_accent"] or primary
            return {
                "org_name": org["name"],
                "org_logo": media_url(f"uploads/logos/{org['logo_filename']}"),
                "org_theme_override": True,
                "org_theme_blue": primary,
                "org_theme_blue_dark": darken_hex(primary),
                "org_theme_orange": accent,
                "org_theme_orange_dark": darken_hex(accent),
            }
        return {
            "org_name": org["name"],
            "org_logo": static_url("img/default-badge.svg"),
            "org_theme_override": True,
            "org_theme_blue": DEFAULT_ORG_THEME["blue"],
            "org_theme_blue_dark": DEFAULT_ORG_THEME["blue_dark"],
            "org_theme_orange": DEFAULT_ORG_THEME["orange"],
            "org_theme_orange_dark": DEFAULT_ORG_THEME["orange_dark"],
        }
    return {
        "org_name": org["name"] if org else "Swarm Baseball",
        "org_logo": static_url("img/swarm-badge.png"),
        "org_theme_override": False,
    }


@app.context_processor
def inject_branding():
    return branding_context_for_org(getattr(g, "org", None))


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Whoever runs MoundHQ itself, not a customer. Deliberately its own
        -- table rather than a flag on organizations.users - running the
        -- product isn't "being an organization," so this identity has to
        -- exist outside that whole tree, the same way coaches does.
        CREATE TABLE IF NOT EXISTS platform_admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
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
            organization_id INTEGER REFERENCES organizations(id),
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
            organization_id INTEGER REFERENCES organizations(id),
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
            organization_id INTEGER REFERENCES organizations(id),
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
            organization_id INTEGER REFERENCES organizations(id),
            entry_date TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS calendar_entry_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
            entry_id INTEGER NOT NULL,
            commenter_name TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (entry_id) REFERENCES throwing_entries (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
            name TEXT,
            email TEXT,
            phone TEXT,
            password_hash TEXT,
            is_admin INTEGER DEFAULT 0,
            is_owner INTEGER DEFAULT 0,
            reset_code TEXT,
            reset_expires TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS invite_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
            token TEXT NOT NULL UNIQUE,
            created_by INTEGER,
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trackman_pitches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER REFERENCES organizations(id),
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
            organization_id INTEGER REFERENCES organizations(id),
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

    # Migration: multi-tenant support. Every organization-owned table gets
    # an organization_id column added if it predates this (existing
    # installs). Nothing in the app writes/reads it yet - this is just the
    # data model landing first so a later pass can wire the actual scoping
    # in safely, without changing how the app behaves today.
    org_owned_tables = (
        "teams", "players", "stat_entries", "videos", "comments",
        "throwing_entries", "calendar_entry_comments", "users",
        "invite_links", "trackman_pitches", "player_contacts",
    )
    for table in org_owned_tables:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "organization_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER REFERENCES organizations(id)")
    conn.commit()

    # Make sure one organization exists for any pre-existing data (the
    # business already running this app becomes "Swarm Baseball", the
    # first organization), and attach every row that doesn't have one yet.
    # A brand-new install with no data at all is left with zero
    # organizations until a real signup flow creates one.
    if conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0:
        has_existing_data = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        if has_existing_data:
            conn.execute(
                "INSERT INTO organizations (name, slug) VALUES (?, ?)",
                ("Swarm Baseball", "swarm-baseball"),
            )
            conn.commit()

    default_org = conn.execute("SELECT id FROM organizations ORDER BY id ASC LIMIT 1").fetchone()
    if default_org:
        default_org_id = default_org["id"]
        for table in org_owned_tables:
            conn.execute(
                f"UPDATE {table} SET organization_id = ? WHERE organization_id IS NULL",
                (default_org_id,),
            )
        conn.commit()

    # Per-organization uniqueness: a team name or a login email/phone only
    # has to be unique within one organization, not across the whole
    # platform, now that more than one organization can exist.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_org_name ON teams(organization_id, name)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email ON users(organization_id, email)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_phone ON users(organization_id, phone)")
    conn.commit()

    # Migration: videos can be pinned so the most important clips float to
    # the top of the timeline ahead of everything else.
    video_cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "pinned" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN pinned INTEGER DEFAULT 0")
        conn.commit()

    # Migration: a captured poster frame, generated client-side at upload
    # time and stored as its own image next to the video, so the player
    # page can show an actual thumbnail instead of the plain black box a
    # <video> element renders before anything's played - especially bad on
    # mobile Safari, which doesn't auto-render a first frame the way some
    # desktop browsers do. NULL for videos uploaded before this existed, or
    # if the browser couldn't capture one; those fall back to a generic
    # placeholder image instead of a per-video thumbnail.
    if "thumbnail_filename" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN thumbnail_filename TEXT")
        conn.commit()

    # Migration: a player can opt in to recruiting visibility overall and
    # still not want every single clip shown to college coaches (a rough
    # bullpen, something still being worked on, etc.) - defaults to 1 so
    # every existing video stays visible exactly as it already was.
    if "recruiting_visible" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN recruiting_visible INTEGER DEFAULT 1")
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

    # Migration: recruiting measurables (height, weight, bats/throws, 60 time, GPA).
    for col in PLAYER_MEASURABLE_FIELDS:
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

    # Migration: a user account can be linked to a player (parents/players
    # land on that player's page when they sign in).
    if "player_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN player_id INTEGER REFERENCES players(id)")
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

    # Migration: admins/owner don't get a linked player - that's for parent/player accounts only.
    conn.execute("UPDATE users SET player_id = NULL WHERE is_admin = 1 AND player_id IS NOT NULL")
    conn.commit()

    # Migration: a private, unguessable token per user for subscribing to the
    # lesson calendar from Google/Apple Calendar (calendar apps can't do a
    # login flow, so the token in the URL is what authenticates the feed).
    # SQLite can't add a UNIQUE column via ALTER TABLE, so the column is
    # plain and a unique index enforces it instead (SQLite allows multiple
    # NULLs in a unique index, so this is safe for existing users with no
    # token yet).
    if "calendar_feed_token" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN calendar_feed_token TEXT")
        conn.commit()
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_calendar_feed_token ON users(calendar_feed_token)"
    )
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

    # Migration: events can carry an optional time (24h "HH:MM").
    if "event_time" not in throwing_cols:
        conn.execute("ALTER TABLE throwing_entries ADD COLUMN event_time TEXT")
        conn.commit()

    # Migration: the pre-multi-tenant schema had a single-column UNIQUE
    # constraint baked directly into teams.name and users.email/phone
    # (global uniqueness across the whole site). That's what the composite
    # per-organization indexes further up are meant to replace, but SQLite
    # can't drop or alter a column-level constraint with ALTER TABLE - the
    # only way to actually remove it from an existing database is to rebuild
    # the table. Without this step, two organizations still couldn't both
    # have a team named "Red" or a user with the same email, even though the
    # composite index says they should be able to. This only fires on
    # existing databases that still carry the old constraint; a fresh
    # install never has it, so this is a one-time, idempotent cleanup.
    def _has_single_column_unique(table, column):
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not idx["unique"]:
                continue
            idx_cols = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            if len(idx_cols) == 1 and idx_cols[0]["name"] == column:
                return True
        return False

    needs_teams_rebuild = _has_single_column_unique("teams", "name")
    needs_users_rebuild = _has_single_column_unique("users", "email") or _has_single_column_unique("users", "phone")

    if needs_teams_rebuild or needs_users_rebuild:
        # players.team_id and throwing_entries.team_id hold a REFERENCES
        # teams(id) foreign key, so rebuilding teams (drop + recreate under
        # the same name) has to happen with FK enforcement suspended, or
        # SQLite raises a FOREIGN KEY constraint failure on the DROP even
        # though the new table ends up with the exact same id values. This
        # can only be toggled between transactions, not inside one.
        conn.execute("PRAGMA foreign_keys = OFF")

    if needs_teams_rebuild:
        conn.executescript(
            """
            DROP TABLE IF EXISTS teams_new;
            CREATE TABLE teams_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER REFERENCES organizations(id),
                name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO teams_new (id, organization_id, name, created_at)
                SELECT id, organization_id, name, created_at FROM teams;
            DROP TABLE teams;
            ALTER TABLE teams_new RENAME TO teams;
            """
        )
        conn.commit()
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_org_name ON teams(organization_id, name)")
        conn.commit()

    if needs_users_rebuild:
        conn.executescript(
            """
            DROP TABLE IF EXISTS users_new;
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER REFERENCES organizations(id),
                name TEXT,
                email TEXT,
                phone TEXT,
                password_hash TEXT,
                is_admin INTEGER DEFAULT 0,
                is_owner INTEGER DEFAULT 0,
                reset_code TEXT,
                reset_expires TEXT,
                player_id INTEGER REFERENCES players(id),
                calendar_feed_token TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO users_new (id, organization_id, name, email, phone, password_hash,
                                    is_admin, is_owner, reset_code, reset_expires,
                                    player_id, calendar_feed_token, created_at)
                SELECT id, organization_id, name, email, phone, password_hash,
                       is_admin, is_owner, reset_code, reset_expires,
                       player_id, calendar_feed_token, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            """
        )
        conn.commit()
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_email ON users(organization_id, email)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_phone ON users(organization_id, phone)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_calendar_feed_token ON users(calendar_feed_token)"
        )
        conn.commit()

    if needs_teams_rebuild or needs_users_rebuild:
        conn.execute("PRAGMA foreign_keys = ON")

    # Migration: platform admins used to be required to have a password set
    # at creation time (the site owner typed one for anyone they added).
    # They're now invited by email instead and set their own password on
    # first login, the same pattern organization users already use, so
    # password_hash has to allow NULL between "invited" and "accepted."
    # SQLite can't drop a column-level NOT NULL with ALTER TABLE, so an
    # existing database still carrying the old constraint has to be rebuilt
    # the same way as the teams/users rebuild above. Only fires once.
    pa_cols = conn.execute("PRAGMA table_info(platform_admins)").fetchall()
    needs_platform_admins_rebuild = any(
        c["name"] == "password_hash" and c["notnull"] for c in pa_cols
    )
    if needs_platform_admins_rebuild:
        conn.executescript(
            """
            DROP TABLE IF EXISTS platform_admins_new;
            CREATE TABLE platform_admins_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO platform_admins_new (id, name, email, password_hash, created_at)
                SELECT id, name, email, password_hash, created_at FROM platform_admins;
            DROP TABLE platform_admins;
            ALTER TABLE platform_admins_new RENAME TO platform_admins;
            """
        )
        conn.commit()

    # Migration: college-recruiting coach portal. Players opt in to being
    # visible to coaches (off by default - a real person's info/video isn't
    # shown to strangers on the internet without them or their family
    # choosing that). Coaches are a separate identity from `users` entirely,
    # since a coach isn't a member of any one organization - they browse
    # opted-in players across every organization on the platform. New coach
    # signups need approval before they can see anything.
    player_cols = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    if "recruiting_opt_in" not in player_cols:
        conn.execute("ALTER TABLE players ADD COLUMN recruiting_opt_in INTEGER DEFAULT 0")
        conn.commit()

    # Self-serve orgs can upload a logo at signup and get a site theme
    # tailored to it (extract_theme_colors) instead of the flat gray default
    # every non-Swarm org used to be stuck with.
    org_cols = {row["name"] for row in conn.execute("PRAGMA table_info(organizations)")}
    if "logo_filename" not in org_cols:
        conn.execute("ALTER TABLE organizations ADD COLUMN logo_filename TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN theme_primary TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN theme_accent TEXT")
        conn.commit()

    # Stripe billing state, one row per org. Every column stays NULL for an
    # org that has never subscribed - see org_plan_cap()'s comment on why
    # that has to mean "unlimited," not "blocked."
    org_billing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(organizations)")}
    if "stripe_customer_id" not in org_billing_cols:
        conn.execute("ALTER TABLE organizations ADD COLUMN stripe_customer_id TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN stripe_subscription_id TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN plan TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN billing_interval TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN subscription_status TEXT")
        conn.execute("ALTER TABLE organizations ADD COLUMN overage_item_id TEXT")
        conn.commit()

    # A trial granted by hand (Platform Admin -> an org -> "Grant Free
    # Trial"), separate from Stripe's own per-subscription trial support.
    # This is how you comp a specific org - no card, no Stripe subscription
    # required - rather than every signup getting a trial automatically.
    if "trial_ends_at" not in org_billing_cols:
        conn.execute("ALTER TABLE organizations ADD COLUMN trial_ends_at TEXT")
        conn.commit()

    # Teams can optionally have their own logo (uniform patch, school crest,
    # etc.) shown on the Teams page - separate from the organization's own
    # logo, which is the whole program's brand rather than one team's.
    team_cols = {row["name"] for row in conn.execute("PRAGMA table_info(teams)")}
    if "logo_filename" not in team_cols:
        conn.execute("ALTER TABLE teams ADD COLUMN logo_filename TEXT")
        conn.commit()

    # Anyone in an org can upload their own stats/video (no admin_required
    # gate on those routes), so admins need a way to review new uploads and
    # mark them as checked. Defaults to 0 (unverified) so existing rows show
    # up as pending until an admin clears them.
    stat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(stat_entries)")}
    if "verified" not in stat_cols:
        conn.execute("ALTER TABLE stat_entries ADD COLUMN verified INTEGER DEFAULT 0")
        conn.commit()
    video_cols = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "verified" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN verified INTEGER DEFAULT 0")
        conn.commit()

    conn.execute(
        """CREATE TABLE IF NOT EXISTS coaches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            school TEXT,
            email TEXT NOT NULL UNIQUE,
            phone TEXT,
            password_hash TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS video_favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (coach_id) REFERENCES coaches (id) ON DELETE CASCADE,
            FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_video_favorites_coach_video ON video_favorites(coach_id, video_id)"
    )
    conn.commit()

    # Coaches can also follow a PLAYER directly (not just favorite one video) -
    # a standing watchlist entry, separate from video favorites.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS player_follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (coach_id) REFERENCES coaches (id) ON DELETE CASCADE,
            FOREIGN KEY (player_id) REFERENCES players (id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_player_follows_coach_player ON player_follows(coach_id, player_id)"
    )
    conn.commit()

    # Tracks which videos a coach has already watched in the feed, so the
    # ranking can push already-seen clips down instead of resurfacing the
    # same ones every visit. One row per coach/video pair - re-watching
    # just bumps viewed_at rather than creating duplicates.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS video_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            viewed_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (coach_id) REFERENCES coaches (id) ON DELETE CASCADE,
            FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_video_views_coach_video ON video_views(coach_id, video_id)"
    )
    conn.commit()

    # No longer used for gating anything (see platform_admins table instead)
    # - kept only so older code paths that still read/write this column on
    # a user row (login, /setup, /start, join) don't break on a fresh DB.
    user_cols_now = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "is_platform_admin" not in user_cols_now:
        conn.execute("ALTER TABLE users ADD COLUMN is_platform_admin INTEGER DEFAULT 0")
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

# Velocity readings are each a single session's peak, so summarizing them
# across sessions should take the best one ever seen, not blend them into an
# average - "FB Top Velo" of 92, 93.1, 94 across three bullpens is a 94 mph
# arm, not a 93.03 mph arm. Spin stats ("Avg Spin") are genuinely meant to be
# averaged, so this only applies when "velo" is in the name.
def is_max_stat(name):
    return "velo" in (name or "").lower()

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

# Column order for the player page's stat tables, grouped the way a coach
# actually reads a stat line - workload, then results, then the rates
# derived from them, then velocity by pitch (fastball first) - instead of
# alphabetically, which scrambled "ER" ahead of "IP" ahead of "K". Matched
# via normalize_col(), so "Strikes" / "strikes" / "Strike" all land in the
# same slot regardless of how they were typed on import. Anything not
# recognized here (a custom column someone added) sorts alphabetically
# after all of it, so it still shows up somewhere sane instead of just
# disappearing or getting buried mid-list.
STAT_DISPLAY_ORDER = (
    list(IP_COL_NAMES) + list(PITCHES_COL_NAMES) + list(STRIKES_COL_NAMES) + list(STRIKE_PCT_COL_NAMES)
    + ["outs", "bf", "battersfaced", "ab", "atbats"]
    + ["h", "hits", "singles", "doubles", "triples", "hr", "homeruns"]
    + ["r", "runs"]
    + list(ER_COL_NAMES)
    + ["bb", "walks", "hbp", "hitbypitch"]
    + list(K_COL_NAMES)
    + ["wp", "wildpitches", "pickoffs"]
    + list(ERA_COL_NAMES)
    + ["k/7", "whip"]
    + [
        "fbvelo", "fbtopvelo", "sivelo", "sitopvelo", "ctvelo", "cttopvelo",
        "slvelo", "sltopvelo", "cbvelo", "cbtopvelo", "chvelo", "chtopvelo",
        "splvelo", "spltopvelo", "swpvelo", "swptopvelo", "maxvelo",
    ]
    + ["score", "g", "games", "appearances"]
)
STAT_ORDER_RANK = {name: i for i, name in enumerate(STAT_DISPLAY_ORDER)}


def _stat_sort_key(name):
    norm = normalize_col(name)
    if norm in STAT_ORDER_RANK:
        return (0, STAT_ORDER_RANK[norm], "")
    if "velo" in (name or "").lower():
        # An unrecognized velo column (a custom pitch type) still groups
        # with the other velocity readings instead of scattering off
        # into alphabetical order on its own.
        return (0, len(STAT_DISPLAY_ORDER), name.lower())
    return (1, 0, name.lower())


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


def parse_event_time_minutes(value):
    """Best-effort parse of a free-text calendar time (e.g. '4pm', '4:00 PM',
    '3:30-4:30', '16:00') into minutes-since-midnight, so a day's entries can
    be sorted chronologically. Returns None when nothing time-shaped is
    found, and those entries just sort after the ones that do."""
    if not value:
        return None
    text = value.strip()
    # 12-hour clock with am/pm, e.g. "4pm", "4:30 PM", "4:30p.m."
    m = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([aApP])\.?[mM]?\.?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if m.group(3).lower() == "p" and hour != 12:
            hour += 12
        elif m.group(3).lower() == "a" and hour == 12:
            hour = 0
        return hour * 60 + minute
    # 24-hour clock or a bare "H:MM" with no am/pm, e.g. "16:00", "3:30-4:30"
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


# ---------- iCalendar (.ics) feed for the lesson calendar ----------

def _ics_escape(text):
    """Escape a value per RFC 5545 (backslash, semicolon, comma, newline)."""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _ics_fold(line):
    """RFC 5545 says no content line should exceed 75 octets; continuation
    lines start with a single space. Our lines are almost always short, but
    a long "Other Info" note could exceed it, so fold defensively."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    out = []
    remaining = line
    limit = 75
    while len(remaining.encode("utf-8")) > limit:
        cut = limit
        while len(remaining[:cut].encode("utf-8")) > limit:
            cut -= 1
        out.append(remaining[:cut])
        remaining = " " + remaining[cut:]
        limit = 74  # continuation lines lose one octet to the leading space
    out.append(remaining)
    return "\r\n".join(out)


def build_ics_feed(entries, calendar_name, mark_general=False):
    """Render throwing_entries rows as a minimal, hand-rolled RFC 5545
    iCalendar feed - no external dependency needed. Events with a
    recognizable time become timed (1-hour default duration); everything
    else becomes an all-day event on that date."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Swarm Baseball//Lesson Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for e in entries:
        minutes = parse_event_time_minutes(e["event_time"])
        date_compact = e["entry_date"].replace("-", "")
        lines.append("BEGIN:VEVENT")
        lines.append(_ics_fold(f"UID:calendar-entry-{e['id']}@swarmbaseball.app"))
        lines.append(_ics_fold(f"DTSTAMP:{now_stamp}"))
        if minutes is not None:
            hh, mm = divmod(minutes, 60)
            end_hh, end_mm = divmod(minutes + 60, 60)
            end_hh %= 24
            lines.append(_ics_fold(f"DTSTART:{date_compact}T{hh:02d}{mm:02d}00"))
            lines.append(_ics_fold(f"DTEND:{date_compact}T{end_hh:02d}{end_mm:02d}00"))
        else:
            lines.append(_ics_fold(f"DTSTART;VALUE=DATE:{date_compact}"))
        summary = e["message"] or "Lesson"
        if mark_general and e["team_id"] is None:
            summary += " (General)"
        lines.append(_ics_fold(f"SUMMARY:{_ics_escape(summary)}"))
        if e["location"]:
            lines.append(_ics_fold(f"LOCATION:{_ics_escape(e['location'])}"))
        desc_parts = []
        if e["event_time"]:
            desc_parts.append(f"Time: {e['event_time']}")
        if e["details"]:
            desc_parts.append(e["details"])
        if desc_parts:
            lines.append(_ics_fold(f"DESCRIPTION:{_ics_escape(chr(10).join(desc_parts))}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def get_or_create_feed_token(conn, user_id):
    """Every account gets one stable, unguessable token the moment they need
    it, so their calendar subscription link never changes."""
    user = conn.execute("SELECT calendar_feed_token FROM users WHERE id = ?", (user_id,)).fetchone()
    if user and user["calendar_feed_token"]:
        return user["calendar_feed_token"]
    token = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET calendar_feed_token = ? WHERE id = ?", (token, user_id))
    conn.commit()
    return token


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


def player_initials(name):
    """Avatar fallback letters: first name initial + last name initial
    ("Reed Interdonato" -> "RI"), not just the first two characters of the
    full name. Single-word names fall back to their first two letters."""
    parts = (name or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


app.jinja_env.filters["initials"] = player_initials


def format_stat(value):
    """stat_entries.stat_value is a SQLite REAL column, so even a plain
    count like 32 pitches round-trips through the database as the Python
    float 32.0 - which would print as "32.0" if handed straight to a
    template. This trims a trailing ".0" for whole numbers (32.0 -> "32")
    and otherwise shows up to 2 decimal places with no trailing zeros
    (88.40 -> "88.4"). Genuine baseball IP notation like 4.2 (4 innings,
    2 outs) is a real value, not a rounding artifact, so it's left exactly
    as entered - it just isn't a whole number, so it passes through
    the decimal branch unchanged."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num == int(num):
        return str(int(num))
    return f"{num:.2f}".rstrip("0").rstrip(".")


app.jinja_env.filters["fmt_stat"] = format_stat
app.jinja_env.filters["trial_days_left"] = org_trial_days_left


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


def find_user_by_identifier(conn, identifier, organization_id):
    """Look a user up by email (case-insensitive) or phone number, scoped to
    one organization - the same email or phone can belong to a different
    person in a different organization now that uniqueness is per-org."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    user = conn.execute(
        "SELECT * FROM users WHERE lower(email) = ? AND organization_id = ?",
        (ident.lower(), organization_id),
    ).fetchone()
    if user:
        return user
    phone = normalize_phone(ident)
    if phone and len(phone) >= 7:
        return conn.execute(
            "SELECT * FROM users WHERE phone = ? AND organization_id = ?",
            (phone, organization_id),
        ).fetchone()
    return None


def any_users_exist(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def post_login_url(user, requested=None):
    """Where someone lands after signing in: the page they were headed to,
    their linked player's page (parents/players), or the player roster."""
    if requested:
        return requested
    if user["player_id"] and not user["is_admin"]:
        return url_for("player_detail", player_id=user["player_id"])
    return url_for("index")


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def platform_admin_required(f):
    """Gates the entire platform-owner side of the site (cross-org
    organizations dashboard, coach approvals) behind its own standalone
    login - platform_admins is a completely separate table from
    organizations/users, on purpose. Running MoundHQ as a product isn't
    "being an organization," so the owner shouldn't need a fake team site
    of their own just to get an account. Same reasoning as coach_required
    below using its own coaches table instead of users."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        admin_id = session.get("platform_admin_id")
        if not admin_id:
            return redirect(url_for("platform_login", next=request.path))
        conn = get_db()
        admin = conn.execute("SELECT * FROM platform_admins WHERE id = ?", (admin_id,)).fetchone()
        conn.close()
        if not admin:
            session.pop("platform_admin_id", None)
            session.pop("platform_admin_name", None)
            return redirect(url_for("platform_login", next=request.path))
        g.platform_admin = admin
        return f(*args, **kwargs)
    return wrapper


def coach_required(f):
    """Gates every coach-portal page behind an approved coach login. Looks
    the coach up fresh from the database on every request (rather than
    trusting a flag cached in the session at login time) so a revoked
    approval takes effect immediately, not just after their next login -
    this page shows opted-in players' info and video to a stranger on the
    internet, so that matters."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        coach_id = session.get("coach_id")
        if not coach_id:
            return redirect(url_for("coach_login", next=request.path))
        conn = get_db()
        coach = conn.execute("SELECT * FROM coaches WHERE id = ?", (coach_id,)).fetchone()
        conn.close()
        if not coach:
            session.pop("coach_id", None)
            session.pop("coach_name", None)
            return redirect(url_for("coach_login", next=request.path))
        if not coach["approved"]:
            return render_template("coach_pending.html", coach=coach)
        g.coach = coach
        return f(*args, **kwargs)
    return wrapper


@app.before_request
def require_login():
    if request.endpoint in ORG_EXEMPT_ENDPOINTS:
        return None

    # These need an org resolved (to know which org's user list to check
    # against - email/phone are only unique within one org now) but don't
    # require an existing session, since they're how you get one.
    org_scoped_open_endpoints = {"login", "set_password", "logout", "forgot_password", "forgot_verify"}
    if request.endpoint in org_scoped_open_endpoints:
        return None

    org_slug = g.org["slug"] if getattr(g, "org", None) else None
    if not session.get("user_id"):
        if org_slug:
            return redirect(url_for("login", org_slug=org_slug, next=request.path))
        # No org context to build a login URL with - e.g. an unrecognized
        # URL, or an endpoint that should have been added to
        # ORG_EXEMPT_ENDPOINTS but wasn't. Send them somewhere real instead
        # of 500ing trying to build a /<org_slug>/login URL with no slug.
        return redirect(url_for("org_picker"))

    # Cross-org guard: being logged into one organization must never grant
    # access to another org's data just because its URL is known or
    # guessed. Treat it the same as not being logged in at all, for *this*
    # org, rather than silently mixing sessions.
    if getattr(g, "org", None) is not None and session.get("organization_id") != g.org["id"]:
        session.clear()
        flash("Please sign in.", "error")
        return redirect(url_for("login", org_slug=org_slug, next=request.path))

    return None


@app.route("/")
def landing():
    """Public marketing home page. A returning, already-logged-in user skips
    straight past this to their own organization; everyone else sees the
    pitch, with "Get Started" (self-serve org signup) and "Log In" as the
    two ways in. See home() below for a version that skips this redirect -
    useful for you, since your session is often scoped to whichever org you
    last opened via "Enter Site," and you don't want that to hijack your
    way back to the actual marketing page."""
    if session.get("user_id") and session.get("organization_id"):
        conn = get_db()
        org = conn.execute("SELECT * FROM organizations WHERE id = ?", (session["organization_id"],)).fetchone()
        conn.close()
        if org:
            return redirect(url_for("index", org_slug=org["slug"]))
    return render_template("landing.html")


@app.route("/home")
def home():
    """Always shows the public marketing page, even if you're currently
    logged into an org - unlike landing() (/) above, which intentionally
    bounces a logged-in customer straight to their own dashboard. This is
    the one to link to from anywhere you want a guaranteed way back to the
    actual landing page."""
    return render_template("landing.html")


@app.route("/pricing")
def pricing():
    """Public pricing page - same marketing-site family as landing()/home()
    above, just its own URL so it can be linked to directly."""
    return render_template("pricing.html", trial_days=TRIAL_DAYS)


@app.route("/terms")
def terms():
    """One Terms of Service for the whole platform, not per-organization -
    Mound HQ is the operator regardless of which org you're looking at."""
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    """One Privacy Policy for the whole platform - see terms() above."""
    return render_template("privacy.html")


@app.route("/robots.txt")
def robots_txt():
    """Deny-by-default: only the public marketing/legal pages are worth a
    search engine indexing. Everything else either needs a login (so a
    crawler would just hit a login form anyway) or is data about real kids
    that has no business showing up in search results."""
    lines = [
        "User-agent: *",
        "Disallow: /",
        "Allow: /$",
        "Allow: /start$",
        "Allow: /home$",
        "Allow: /pricing$",
        "Allow: /terms$",
        "Allow: /privacy$",
        "",
        f"Sitemap: {request.url_root.rstrip('/')}/sitemap.xml",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    pages = [
        url_for("landing", _external=True),
        url_for("start", _external=True),
        url_for("pricing", _external=True),
        url_for("terms", _external=True),
        url_for("privacy", _external=True),
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"  <url><loc>{p}</loc></url>" for p in pages]
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


@app.route("/login")
def org_picker():
    """Cross-identity entry point (not the marketing home page - that's
    landing() at "/"). Always shows the chooser - every organization on the
    platform, plus the separate college-coach portal - rather than silently
    bouncing an already-logged-in visitor back into whichever org they
    happen to have an active session with. That auto-redirect made sense
    when there was only ever one organization, but once someone manages (or
    just wants to check) more than one, "click Log In" needs to actually let
    them pick, not assume."""
    conn = get_db()
    orgs = conn.execute("SELECT * FROM organizations ORDER BY name COLLATE NOCASE ASC").fetchall()
    conn.close()

    if not orgs:
        return redirect(url_for("setup"))

    current_org = None
    if session.get("user_id") and session.get("organization_id"):
        current_org = next((o for o in orgs if o["id"] == session["organization_id"]), None)

    return render_template("org_picker.html", organizations=orgs, current_org=current_org)


@app.route("/start", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def start():
    """Public self-serve signup: any new organization can create itself and
    its owner account here, no manual onboarding required. This is distinct
    from setup(), which only ever runs once - for the platform's very first
    organization/owner - and grants the platform-admin role that implies.
    Every org created here is a normal, independent tenant."""
    if request.method == "POST":
        org_name = request.form.get("org_name", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-")

        conn = get_db()
        if not org_name or not slug:
            flash("Enter a name for your organization.", "error")
        elif not name:
            flash("Enter your name.", "error")
        elif not email and not phone:
            flash("Enter an email or a phone number (or both).", "error")
        elif len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            # Org names collide sometimes ("Elite Baseball" is popular) - keep
            # the URL slug unique by appending a short numeric suffix rather
            # than failing the signup outright.
            if conn.execute("SELECT id FROM organizations WHERE slug = ?", (slug,)).fetchone():
                base_slug, suffix = slug, 2
                while conn.execute("SELECT id FROM organizations WHERE slug = ?", (slug,)).fetchone():
                    slug = f"{base_slug}-{suffix}"
                    suffix += 1

            # Logo upload is optional - no logo (or a logo that doesn't
            # yield usable colors) just means the org keeps the default
            # gray theme, same as before this feature existed.
            logo_filename = None
            theme_primary = None
            theme_accent = None
            logo = request.files.get("logo")
            if logo and logo.filename and allowed_file(logo.filename, ALLOWED_PHOTO_EXT):
                safe_name = secure_filename(logo.filename)
                logo_filename = f"{slug}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
                # Read once into memory: Pillow reads color data from the
                # bytes directly (no local file needed), and the same bytes
                # get uploaded to R2 - nothing touches local disk.
                logo_bytes = io.BytesIO(logo.read())
                colors = extract_theme_colors(logo_bytes)
                if colors:
                    theme_primary, theme_accent = colors
                if MEDIA_ENABLED:
                    upload_media(logo_bytes, f"uploads/logos/{logo_filename}", logo.content_type)
                else:
                    logo_filename = None

            org_cur = conn.execute(
                "INSERT INTO organizations (name, slug, logo_filename, theme_primary, theme_accent) "
                "VALUES (?, ?, ?, ?, ?)",
                (org_name, slug, logo_filename, theme_primary, theme_accent),
            )
            org_id = org_cur.lastrowid
            cur = conn.execute(
                "INSERT INTO users (organization_id, name, email, phone, password_hash, is_admin, is_owner) "
                "VALUES (?, ?, ?, ?, ?, 1, 1)",
                (org_id, name, email or None, phone, generate_password_hash(password)),
            )
            conn.commit()
            session.permanent = True
            session["user_id"] = cur.lastrowid
            session["user_name"] = name
            session["is_admin"] = True
            session["is_owner"] = True
            session["is_platform_admin"] = False
            session["player_id"] = None
            session["organization_id"] = org_id
            conn.close()
            flash(f"{org_name} is live. Welcome aboard!", "success")

            # If they arrived here from a specific /pricing card, take them
            # straight to checkout for that plan instead of dumping them on
            # the roster with no next step. Anyone who came in directly
            # (no plan chosen) just lands on their new empty roster, same
            # as before this feature existed - subscribing stays optional.
            plan_key = request.form.get("plan", "")
            if BILLING_ENABLED and plan_key in PLANS:
                return redirect(url_for("billing", org_slug=slug, preselect=plan_key,
                                         interval=request.form.get("interval", "monthly")))
            return redirect(url_for("index", org_slug=slug))
        conn.close()
        return redirect(url_for("start"))

    return render_template("start.html")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the first organization and its owner account.
    Disabled once any organization exists - after that, new organizations
    are created directly rather than through a public signup flow."""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] > 0:
        conn.close()
        return redirect(url_for("org_picker"))

    if request.method == "POST":
        org_name = request.form.get("org_name", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        slug = re.sub(r"[^a-z0-9]+", "-", org_name.lower()).strip("-")

        if not org_name or not slug:
            flash("Enter a name for your organization.", "error")
        elif not email and not phone:
            flash("Enter an email or a phone number (or both).", "error")
        elif len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            org_cur = conn.execute(
                "INSERT INTO organizations (name, slug) VALUES (?, ?)", (org_name, slug)
            )
            org_id = org_cur.lastrowid
            # setup() only ever runs once, before any organization exists, so
            # whoever creates it here is definitionally the platform's very
            # first user - the platform-owner role that gates coach approvals.
            cur = conn.execute(
                "INSERT INTO users (organization_id, name, email, phone, password_hash, is_admin, is_owner, is_platform_admin) VALUES (?, ?, ?, ?, ?, 1, 1, 1)",
                (org_id, name, email or None, phone, generate_password_hash(password)),
            )
            conn.commit()
            session.permanent = True
            session["user_id"] = cur.lastrowid
            session["user_name"] = name
            session["is_admin"] = True
            session["is_owner"] = True
            session["is_platform_admin"] = True
            session["player_id"] = None
            session["organization_id"] = org_id
            conn.close()
            flash("Organization created. Welcome!", "success")
            return redirect(url_for("index", org_slug=slug))
        conn.close()
        return redirect(url_for("setup"))

    conn.close()
    return render_template("setup.html")


@app.route("/<org_slug>/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    conn = get_db()
    org_row_count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE organization_id = ?", (g.org["id"],)
    ).fetchone()[0]
    if org_row_count == 0:
        conn.close()
        return redirect(url_for("setup"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = find_user_by_identifier(conn, identifier, g.org["id"])
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
        session["is_platform_admin"] = bool(user["is_platform_admin"])
        session["player_id"] = user["player_id"]
        session["organization_id"] = g.org["id"]
        return redirect(post_login_url(user, request.form.get("next")))

    conn.close()
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/<org_slug>/set-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
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
        session["is_platform_admin"] = bool(user["is_platform_admin"])
        session["player_id"] = user["player_id"]
        session["organization_id"] = user["organization_id"]
        flash("Password set - you're in!", "success")
        return redirect(post_login_url(user, request.form.get("next")))

    conn.close()
    return render_template("set_password.html", user=user, next=request.args.get("next", ""))


@app.route("/<org_slug>/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        conn = get_db()
        user = find_user_by_identifier(conn, identifier, g.org["id"])

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
            attempts.append(("email", lambda: send_reset_email(user["email"], code, g.org["name"])))
        if user["phone"] and sms_configured():
            attempts.append(("text", lambda: send_reset_sms(user["phone"], code, g.org["name"])))
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


@app.route("/<org_slug>/forgot/verify", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
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
        session["is_platform_admin"] = bool(user["is_platform_admin"])
        session["player_id"] = user["player_id"]
        session["organization_id"] = user["organization_id"]
        flash("Password updated - you're signed in.", "success")
        return redirect(post_login_url(user))

    return render_template("forgot_verify.html")


@app.route("/<org_slug>/account", methods=["GET", "POST"])
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


@app.route("/<org_slug>/branding", methods=["POST"])
@admin_required
def update_org_branding():
    """Lets an org update its own logo (and the site theme colors extracted
    from it) after the fact - not just at /start signup time. Mirrors that
    same upload+extract logic exactly. branding_context_for_org() reads the
    organizations row fresh on every request, so as soon as this commits,
    every page across the org re-themes itself - nothing to invalidate."""
    logo = request.files.get("logo")
    if not logo or not logo.filename:
        flash("Choose a logo image to upload.", "error")
        return redirect(url_for("account"))
    if not allowed_file(logo.filename, ALLOWED_PHOTO_EXT):
        flash("That file isn't a supported image type (PNG, JPG, or GIF).", "error")
        return redirect(url_for("account"))
    if not MEDIA_ENABLED:
        flash("Logo storage isn't configured yet - ask your platform admin.", "error")
        return redirect(url_for("account"))

    safe_name = secure_filename(logo.filename)
    logo_filename = f"{g.org['slug']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    logo_bytes = io.BytesIO(logo.read())
    colors = extract_theme_colors(logo_bytes)
    theme_primary, theme_accent = colors if colors else (None, None)
    upload_media(logo_bytes, f"uploads/logos/{logo_filename}", logo.content_type)

    conn = get_db()
    old_logo_filename = g.org["logo_filename"]
    conn.execute(
        "UPDATE organizations SET logo_filename = ?, theme_primary = ?, theme_accent = ? WHERE id = ?",
        (logo_filename, theme_primary, theme_accent, g.org["id"]),
    )
    conn.commit()
    conn.close()
    if old_logo_filename:
        delete_media(f"uploads/logos/{old_logo_filename}")
    flash("Logo updated - your site's colors have been refreshed to match.", "success")
    return redirect(url_for("account"))


# ---------- Routes: billing (Stripe) ----------
# Optional and additive: an org with no plan on file keeps working exactly
# as it always has (org_plan_cap() returns None -> unlimited). Subscribing
# is something an admin opts into from here, not a gate in front of /start.

@app.route("/<org_slug>/billing")
@admin_required
def billing():
    conn = get_db()
    player_count = org_player_count(conn, g.org["id"])
    conn.close()
    return render_template(
        "billing.html",
        plans=PLANS,
        player_count=player_count,
        billing_enabled=BILLING_ENABLED,
        publishable_key=STRIPE_PUBLISHABLE_KEY,
        trial_days=TRIAL_DAYS,
    )


@app.route("/<org_slug>/billing/checkout", methods=["POST"])
@admin_required
def billing_checkout():
    if not BILLING_ENABLED:
        flash("Billing isn't set up yet - ask your platform admin.", "error")
        return redirect(url_for("billing"))

    plan_key = request.form.get("plan", "")
    interval = "annual" if request.form.get("interval") == "annual" else "monthly"
    plan = PLANS.get(plan_key)
    price_id = plan and (plan["annual_price_id"] if interval == "annual" else plan["monthly_price_id"])
    if not plan or not price_id:
        flash("That plan isn't available yet - ask your platform admin.", "error")
        return redirect(url_for("billing"))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()

    line_items = [{"price": price_id, "quantity": 1}]
    if plan["overage_price_id"]:
        # Metered price - Stripe requires no quantity on the line item
        # itself; usage is reported later via sync_overage_quantity().
        line_items.append({"price": plan["overage_price_id"]})

    checkout_kwargs = dict(
        mode="subscription",
        line_items=line_items,
        client_reference_id=str(g.org["id"]),
        metadata={"organization_id": str(g.org["id"]), "plan": plan_key, "interval": interval},
        subscription_data={
            "metadata": {"organization_id": str(g.org["id"]), "plan": plan_key, "interval": interval},
            **({"trial_period_days": TRIAL_DAYS} if TRIAL_DAYS > 0 else {}),
        },
        success_url=url_for("billing", org_slug=g.org["slug"], _external=True) + "?checkout=success",
        cancel_url=url_for("billing", org_slug=g.org["slug"], _external=True) + "?checkout=cancelled",
        allow_promotion_codes=True,
    )
    if g.org["stripe_customer_id"]:
        checkout_kwargs["customer"] = g.org["stripe_customer_id"]
    elif user["email"]:
        checkout_kwargs["customer_email"] = user["email"]

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception:
        app.logger.exception("Stripe checkout session creation failed for org %s", g.org["id"])
        flash("Couldn't start checkout - try again in a moment.", "error")
        return redirect(url_for("billing"))

    return redirect(checkout_session.url, code=303)


@app.route("/<org_slug>/billing/portal", methods=["POST"])
@admin_required
def billing_portal():
    if not BILLING_ENABLED or not g.org["stripe_customer_id"]:
        flash("No billing account on file yet - subscribe to a plan first.", "error")
        return redirect(url_for("billing"))
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=g.org["stripe_customer_id"],
            return_url=url_for("billing", org_slug=g.org["slug"], _external=True),
        )
    except Exception:
        app.logger.exception("Stripe billing portal session creation failed for org %s", g.org["id"])
        flash("Couldn't open the billing portal - try again in a moment.", "error")
        return redirect(url_for("billing"))
    return redirect(portal_session.url, code=303)


@app.route("/webhooks/stripe", methods=["POST"])
@csrf.exempt  # signed by Stripe (Stripe-Signature header), not a browser
              # session - there's no CSRF token to check, same as backup_db.
def stripe_webhook():
    if not BILLING_ENABLED:
        abort(404)

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    obj = event["data"]["object"]
    conn = get_db()

    if event["type"] == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("organization_id")
        subscription_id = obj.get("subscription")
        if org_id and subscription_id:
            _apply_subscription_to_org(conn, int(org_id), obj.get("customer"), subscription_id)

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.created"):
        org_id = (obj.get("metadata") or {}).get("organization_id")
        if org_id:
            _apply_subscription_to_org(conn, int(org_id), obj.get("customer"), obj.get("id"), subscription_obj=obj)

    elif event["type"] == "customer.subscription.deleted":
        org_id = (obj.get("metadata") or {}).get("organization_id")
        if org_id:
            conn.execute("UPDATE organizations SET subscription_status = 'canceled' WHERE id = ?", (int(org_id),))
            conn.commit()

    conn.close()
    return {"ok": True}


@app.route("/<org_slug>/logout", methods=["POST"])
def logout():
    org_slug = g.org["slug"] if getattr(g, "org", None) else None
    session.clear()
    return redirect(url_for("login", org_slug=org_slug))


# ---------- Routes: college-recruiting coach portal ----------
# A completely separate identity from team users/players. Coaches sign up
# themselves, then wait for a platform admin to approve them before they can
# see anything - every opted-in player's info and video is otherwise a
# stranger-on-the-internet page, so that gate matters. Once approved, a
# coach's login works the same across every organization on the platform.

def normalize_coach_email(raw):
    return (raw or "").strip().lower()


@app.route("/coach/signup", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def coach_signup():
    if session.get("coach_id"):
        return redirect(url_for("coach_players"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        school = request.form.get("school", "").strip()
        email = normalize_coach_email(request.form.get("email", ""))
        phone = normalize_phone(request.form.get("phone", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not name:
            flash("Your name is required.", "error")
        elif not school:
            flash("Let us know what school/program you're with.", "error")
        elif not email:
            flash("A valid email is required.", "error")
        elif len(password) < 6:
            flash("Password needs at least 6 characters.", "error")
        elif password != confirm:
            flash("Those passwords don't match.", "error")
        else:
            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO coaches (name, school, email, phone, password_hash, approved) VALUES (?, ?, ?, ?, ?, 0)",
                    (name, school, email, phone, generate_password_hash(password)),
                )
                conn.commit()
                conn.close()
                flash("Thanks! Your account is pending approval - we'll email you once you're in.", "success")
                return redirect(url_for("coach_login"))
            except sqlite3.IntegrityError:
                conn.close()
                flash("An account with that email already exists - try signing in instead.", "error")

    return render_template("coach_signup.html")


@app.route("/coach/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def coach_login():
    if request.method == "POST":
        email = normalize_coach_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        conn = get_db()
        coach = conn.execute("SELECT * FROM coaches WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not coach or not check_password_hash(coach["password_hash"], password):
            flash("That email or password isn't right.", "error")
            return redirect(url_for("coach_login", next=request.form.get("next", "")))

        session.permanent = True
        session["coach_id"] = coach["id"]
        session["coach_name"] = coach["name"]

        if not coach["approved"]:
            return render_template("coach_pending.html", coach=coach)

        return redirect(request.form.get("next") or url_for("coach_players"))

    return render_template("coach_login.html", next=request.args.get("next", ""))


@app.route("/coach/logout", methods=["POST"])
def coach_logout():
    session.pop("coach_id", None)
    session.pop("coach_name", None)
    return redirect(url_for("coach_login"))


def _coach_grad_year_cutoff():
    """The last grad_year still considered 'current' on the recruiting
    portal. High school graduations land in the late spring, so a class
    flips from 'still playing' to 'graduated' around July 1 of its grad
    year rather than on January 1 - a Class of 2026 player is a current
    recruit all the way through their senior spring, then ages off once
    summer starts. Stored as a string since grad_year is TEXT; comparing
    same-length 4-digit year strings sorts identically to comparing ints."""
    today = date.today()
    cutoff_year = today.year if today.month >= 7 else today.year - 1
    return str(cutoff_year)


# Players without a recorded grad_year are kept visible (unknown grad year
# isn't the same as graduated - hiding them would just be a data-entry
# penalty), everyone else is dropped once their grad_year falls at or
# before the cutoff above.
COACH_NOT_GRADUATED_SQL = "(p.grad_year IS NULL OR p.grad_year = '' OR p.grad_year > ?)"


def _coach_player_filters():
    """Shared WHERE-clause building for the coach player search, leaderboards,
    and video feed: opted-in, not-yet-graduated players only, everywhere,
    plus whatever the coach chose to filter by."""
    conds = ["p.recruiting_opt_in = 1", COACH_NOT_GRADUATED_SQL]
    params = [_coach_grad_year_cutoff()]
    team_id = request.args.get("team", "").strip()
    grad_year = request.args.get("grad_year", "").strip()
    position = request.args.get("position", "").strip()
    q = request.args.get("q", "").strip()
    if team_id.isdigit():
        conds.append("p.team_id = ?")
        params.append(int(team_id))
    if grad_year:
        conds.append("p.grad_year = ?")
        params.append(grad_year)
    if position:
        conds.append("p.position = ?")
        params.append(position)
    if q:
        conds.append("p.name LIKE ?")
        params.append(f"%{q}%")
    player_id = request.args.get("player", "").strip()
    if player_id.isdigit():
        conds.append("p.id = ?")
        params.append(int(player_id))
    return " AND ".join(conds), params, {
        "team": team_id, "grad_year": grad_year, "position": position, "q": q, "player": player_id,
    }


def _coach_filter_options(conn):
    """Distinct team/grad-year/position values across every organization,
    for populating the filter dropdowns - each team is labeled with its
    organization so same-named teams in different orgs aren't ambiguous.
    Scoped to the same not-yet-graduated players the listings themselves
    show, so a coach never sees a filter option (like Class of 2024) that
    would just return an empty page."""
    cutoff = _coach_grad_year_cutoff()
    teams = conn.execute(
        f"""SELECT t.id, t.name, o.name AS org_name
           FROM teams t JOIN organizations o ON o.id = t.organization_id
           WHERE t.id IN (
               SELECT DISTINCT team_id FROM players p
               WHERE recruiting_opt_in = 1 AND team_id IS NOT NULL AND {COACH_NOT_GRADUATED_SQL}
           )
           ORDER BY o.name COLLATE NOCASE ASC, t.name COLLATE NOCASE ASC""",
        (cutoff,),
    ).fetchall()
    grad_years = [
        r[0] for r in conn.execute(
            f"""SELECT DISTINCT grad_year FROM players p
                WHERE recruiting_opt_in = 1 AND grad_year IS NOT NULL AND grad_year != ''
                  AND {COACH_NOT_GRADUATED_SQL}
                ORDER BY grad_year ASC""",
            (cutoff,),
        ).fetchall()
    ]
    positions = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT position FROM players WHERE recruiting_opt_in = 1 AND position IS NOT NULL AND position != '' ORDER BY position ASC"
        ).fetchall()
    ]
    return teams, grad_years, positions


@app.route("/coach/players")
@coach_required
def coach_players():
    conn = get_db()
    where_sql, params, filters = _coach_player_filters()
    players = conn.execute(
        f"""SELECT p.*, t.name AS team_name, t.logo_filename AS team_logo_filename,
                   o.name AS org_name, o.slug AS org_slug,
                   EXISTS(SELECT 1 FROM player_follows pf WHERE pf.player_id = p.id AND pf.coach_id = ?) AS is_followed
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            WHERE {where_sql}
            ORDER BY p.name COLLATE NOCASE ASC""",
        [g.coach["id"]] + params,
    ).fetchall()
    teams, grad_years, positions = _coach_filter_options(conn)
    conn.close()
    return render_template(
        "coach_players.html", players=players, teams=teams, grad_years=grad_years,
        positions=positions, filters=filters,
    )


@app.route("/coach/leaderboards")
@coach_required
def coach_leaderboards():
    conn = get_db()
    where_sql, params, filters = _coach_player_filters()
    # Leaderboards only ever rank players this coach follows - not the whole
    # platform - so it's a personal shortlist comparison, not a general
    # scouting-everyone-at-once ranking (that's what Players/Feed are for).
    where_sql += " AND p.id IN (SELECT player_id FROM player_follows WHERE coach_id = ?)"
    params = params + [g.coach["id"]]
    teams, grad_years, positions = _coach_filter_options(conn)

    # Top Velo used to blend Bullpen, Pulldown, and Game/Live-ABs readings
    # into one "any pitch" number - but a Pulldown max-effort throw and a
    # Bullpen fastball aren't really the same measurement, so they're split
    # into their own leaderboards instead of getting averaged together.
    bullpen_velo_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, o.name AS org_name, MAX(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            WHERE lower(s.stat_name) LIKE '%velo%' AND s.category = 'Bullpen' AND {where_sql}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    pulldown_velo_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, o.name AS org_name, MAX(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            WHERE lower(s.stat_name) LIKE '%velo%' AND s.category = 'Pulldown' AND {where_sql}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    strike_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, o.name AS org_name,
                   ROUND(AVG(s.stat_value), 1) AS value, COUNT(*) AS sessions
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            WHERE s.stat_name = 'Strike %' AND {where_sql}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    k_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, o.name AS org_name, SUM(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            WHERE lower(s.stat_name) IN ('k', 'so', 'strikeouts', 'ks') AND {where_sql}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    conn.close()
    return render_template(
        "coach_leaderboards.html", bullpen_velo_leaders=bullpen_velo_leaders,
        pulldown_velo_leaders=pulldown_velo_leaders, strike_leaders=strike_leaders,
        k_leaders=k_leaders, teams=teams, grad_years=grad_years, positions=positions, filters=filters,
    )


def _rank_feed_videos(videos):
    """Order the feed by a blended score instead of pure recency: harder
    velocity readings and bullpen/game reps are what a coach actually wants
    to evaluate first, recent uploads get a boost so the feed stays fresh,
    popular players/clips (followers, favorites) get a nudge upward, clips
    this coach has already watched get pushed way down (but not excluded
    entirely), and a healthy random jitter keeps the order from going stale
    on repeat visits and makes sure every video - not just the popular
    ones - has a real shot at the top of the feed."""
    if not videos:
        return videos

    velos = [v["player_best_velo"] for v in videos if v["player_best_velo"] is not None]
    max_velo = max(velos) if velos else None
    min_velo = min(velos) if velos else None
    today = date.today()

    # Popularity is log-scaled before normalizing so one heavily-followed
    # player or one viral clip can't just run away with the feed by having
    # 10x the raw count of everyone else - going from 5 to 50 followers
    # matters a lot more to the score than going from 500 to 545.
    max_followers = max((v["follower_count"] or 0) for v in videos)
    max_favorites = max((v["favorite_count"] or 0) for v in videos)
    log_max_followers = math.log1p(max_followers)
    log_max_favorites = math.log1p(max_favorites)

    scored = []
    for v in videos:
        velo = v["player_best_velo"]
        if velo is not None and max_velo is not None and max_velo > min_velo:
            velo_score = (velo - min_velo) / (max_velo - min_velo)
        elif velo is not None:
            velo_score = 1.0
        else:
            # No recorded velo (common for position players) shouldn't bury
            # the clip - treat it as a neutral middle score, not a penalty.
            velo_score = 0.35

        category = (v["category"] or "").strip().lower()
        category_score = 1.0 if category in ("bullpen", "game") else 0.4

        try:
            entry_date = date.fromisoformat((v["entry_date"] or "")[:10])
            days_old = max((today - entry_date).days, 0)
        except ValueError:
            days_old = 365
        recency_score = 1.0 / (1.0 + days_old / 30.0)

        follower_score = (math.log1p(v["follower_count"] or 0) / log_max_followers) if log_max_followers else 0.0
        favorite_score = (math.log1p(v["favorite_count"] or 0) / log_max_favorites) if log_max_favorites else 0.0
        popularity_score = (0.5 * follower_score) + (0.5 * favorite_score)

        weighted = (
            (0.20 * velo_score)
            + (0.15 * category_score)
            + (0.20 * recency_score)
            + (0.15 * popularity_score)
        )
        # A clip this coach has already watched takes a flat penalty - not
        # a big enough hit to make it mathematically impossible to resurface
        # (nothing to show otherwise would be worse), but deliberately
        # smaller than the jitter amplitude below so there's always at
        # least a small window where a re-watch can still happen, while an
        # unseen clip wins the comparison the large majority of the time.
        if v["is_seen"]:
            weighted = max(0.0, weighted - 0.22)
        # Max weighted score is 0.70 - a jitter of up to 0.30 is deliberately
        # bigger than the entire popularity weight, so even a video from a
        # player with zero followers/favorites still has a real (if smaller)
        # chance of landing near the top against the most popular player on
        # any given load. Popularity shifts the odds, it doesn't decide them.
        jitter = random.random() * 0.30
        scored.append((weighted + jitter, v))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [v for _, v in scored]


@app.route("/coach/feed")
@coach_required
def coach_feed():
    conn = get_db()
    where_sql, params, filters = _coach_player_filters()

    following_only = request.args.get("following") == "1"
    filters["following"] = following_only
    if following_only:
        where_sql += " AND p.id IN (SELECT player_id FROM player_follows WHERE coach_id = ?)"
        params = params + [g.coach["id"]]

    videos = conn.execute(
        f"""SELECT v.*, p.name AS player_name, p.position, p.grad_year, p.photo_filename AS player_photo_filename,
                   t.name AS team_name, o.name AS org_name,
                   EXISTS(SELECT 1 FROM video_favorites vf WHERE vf.video_id = v.id AND vf.coach_id = ?) AS is_favorited,
                   EXISTS(SELECT 1 FROM player_follows pf WHERE pf.player_id = v.player_id AND pf.coach_id = ?) AS is_followed,
                   bv.best_velo AS player_best_velo,
                   (SELECT COUNT(*) FROM video_favorites vf2 WHERE vf2.video_id = v.id) AS favorite_count,
                   (SELECT COUNT(*) FROM player_follows pf2 WHERE pf2.player_id = v.player_id) AS follower_count,
                   EXISTS(SELECT 1 FROM video_views vv WHERE vv.video_id = v.id AND vv.coach_id = ?) AS is_seen
            FROM videos v
            JOIN players p ON p.id = v.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            JOIN organizations o ON o.id = p.organization_id
            LEFT JOIN (
                SELECT player_id, MAX(stat_value) AS best_velo
                FROM stat_entries
                WHERE lower(stat_name) LIKE '%velo%'
                GROUP BY player_id
            ) bv ON bv.player_id = v.player_id
            WHERE {where_sql} AND v.recruiting_visible = 1
            ORDER BY v.entry_date DESC, v.id DESC""",
        [g.coach["id"], g.coach["id"], g.coach["id"]] + params,
    ).fetchall()
    videos = _rank_feed_videos(videos)
    teams, grad_years, positions = _coach_filter_options(conn)
    conn.close()
    return render_template(
        "coach_feed.html", videos=videos, teams=teams, grad_years=grad_years,
        positions=positions, filters=filters,
    )


@app.route("/coach/videos/<int:video_id>/favorite", methods=["POST"])
@coach_required
def coach_favorite_toggle(video_id):
    conn = get_db()
    # Re-check opt-in on every toggle, not just when the video feed was first
    # loaded - a family can turn recruiting visibility off at any time, and a
    # stale favorite shouldn't be a backdoor to a video that's no longer opted in.
    video = conn.execute(
        """SELECT v.id FROM videos v JOIN players p ON p.id = v.player_id
           WHERE v.id = ? AND p.recruiting_opt_in = 1 AND v.recruiting_visible = 1""",
        (video_id,),
    ).fetchone()
    if not video:
        conn.close()
        abort(404)

    existing = conn.execute(
        "SELECT id FROM video_favorites WHERE coach_id = ? AND video_id = ?",
        (g.coach["id"], video_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM video_favorites WHERE id = ?", (existing["id"],))
        favorited = False
    else:
        conn.execute(
            "INSERT INTO video_favorites (coach_id, video_id) VALUES (?, ?)", (g.coach["id"], video_id)
        )
        favorited = True
    conn.commit()
    conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"favorited": favorited}
    return redirect(request.referrer or url_for("coach_feed"))


@app.route("/coach/videos/<int:video_id>/seen", methods=["POST"])
@coach_required
def coach_mark_video_seen(video_id):
    """Fired from the feed once a clip actually starts playing (not just
    scrolls past) - records that this coach has watched it, so the feed
    ranking can push it down in favor of stuff they haven't seen yet.
    Fire-and-forget from the client, so this stays lightweight: no need to
    re-validate recruiting visibility here the way favoriting does, since
    this has no effect on what's shown to anyone, only on ordering."""
    conn = get_db()
    conn.execute(
        """INSERT INTO video_views (coach_id, video_id, viewed_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(coach_id, video_id) DO UPDATE SET viewed_at = excluded.viewed_at""",
        (g.coach["id"], video_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.route("/coach/favorites")
@coach_required
def coach_favorites():
    conn = get_db()
    videos = conn.execute(
        """SELECT v.*, p.name AS player_name, p.position, p.grad_year,
                  t.name AS team_name, o.name AS org_name, 1 AS is_favorited,
                  EXISTS(SELECT 1 FROM player_follows pf WHERE pf.player_id = p.id AND pf.coach_id = ?) AS is_followed
           FROM video_favorites vf
           JOIN videos v ON v.id = vf.video_id
           JOIN players p ON p.id = v.player_id
           LEFT JOIN teams t ON t.id = p.team_id
           JOIN organizations o ON o.id = p.organization_id
           WHERE vf.coach_id = ? AND p.recruiting_opt_in = 1 AND v.recruiting_visible = 1 AND {not_graduated}
           ORDER BY vf.created_at DESC""".format(not_graduated=COACH_NOT_GRADUATED_SQL),
        (g.coach["id"], g.coach["id"], _coach_grad_year_cutoff()),
    ).fetchall()
    conn.close()
    return render_template("coach_favorites.html", videos=videos)


@app.route("/coach/player/<int:player_id>")
@coach_required
def coach_player_detail(player_id):
    conn = get_db()
    # Cross-org lookup (a coach isn't scoped to one organization), gated on
    # recruiting_opt_in and not-yet-graduated same as everywhere else in the
    # coach portal - a player who isn't opted in (or has graduated) doesn't
    # exist as far as this route is concerned, full stop, even via a direct
    # link or an old bookmark.
    player = conn.execute(
        """SELECT p.*, t.name AS team_name, o.name AS org_name
           FROM players p
           LEFT JOIN teams t ON t.id = p.team_id
           JOIN organizations o ON o.id = p.organization_id
           WHERE p.id = ? AND p.recruiting_opt_in = 1 AND {not_graduated}""".format(
            not_graduated=COACH_NOT_GRADUATED_SQL
        ),
        (player_id, _coach_grad_year_cutoff()),
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    date_from, date_to, is_default_range = _player_date_range_from_request()
    ctx = _build_player_profile_context(conn, player_id, date_from, date_to, is_coach_view=True)

    is_followed = conn.execute(
        "SELECT 1 FROM player_follows WHERE coach_id = ? AND player_id = ?",
        (g.coach["id"], player_id),
    ).fetchone() is not None

    conn.close()
    # Reuses the exact same player.html template org members see - same
    # stats, charts, video timeline - just with editing, printing, and
    # adding stats/video/comments hidden via is_coach_view (enforced in the
    # template, not just cosmetically: those routes require an org_slug a
    # coach session doesn't have, so they'd 500 if ever rendered here).
    return render_template(
        "player.html", player=player, is_coach_view=True, is_followed=is_followed,
        is_default_range=is_default_range, **ctx
    )


@app.route("/coach/players/<int:player_id>/follow", methods=["POST"])
@coach_required
def coach_player_follow_toggle(player_id):
    conn = get_db()
    # Re-check opt-in on every toggle, same reasoning as favoriting a video -
    # a family can turn recruiting visibility off at any time.
    player = conn.execute(
        "SELECT id FROM players WHERE id = ? AND recruiting_opt_in = 1", (player_id,)
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    existing = conn.execute(
        "SELECT id FROM player_follows WHERE coach_id = ? AND player_id = ?",
        (g.coach["id"], player_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM player_follows WHERE id = ?", (existing["id"],))
        followed = False
    else:
        conn.execute(
            "INSERT INTO player_follows (coach_id, player_id) VALUES (?, ?)", (g.coach["id"], player_id)
        )
        followed = True
    conn.commit()
    conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"followed": followed}
    return redirect(request.referrer or url_for("coach_player_detail", player_id=player_id))


@app.route("/coach/following")
@coach_required
def coach_following():
    conn = get_db()
    players = conn.execute(
        """SELECT p.*, t.name AS team_name, t.logo_filename AS team_logo_filename,
                  o.name AS org_name, o.slug AS org_slug
           FROM player_follows pf
           JOIN players p ON p.id = pf.player_id
           LEFT JOIN teams t ON t.id = p.team_id
           JOIN organizations o ON o.id = p.organization_id
           WHERE pf.coach_id = ? AND p.recruiting_opt_in = 1 AND {not_graduated}
           ORDER BY pf.created_at DESC""".format(not_graduated=COACH_NOT_GRADUATED_SQL),
        (g.coach["id"], _coach_grad_year_cutoff()),
    ).fetchall()
    conn.close()
    return render_template("coach_following.html", players=players)


# ---------- Routes: platform admin (coach approvals) ----------

@app.route("/platform/coaches")
@platform_admin_required
def platform_coaches():
    conn = get_db()
    pending = conn.execute(
        "SELECT * FROM coaches WHERE approved = 0 ORDER BY created_at ASC"
    ).fetchall()
    approved = conn.execute(
        "SELECT * FROM coaches WHERE approved = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return render_template("platform_coaches.html", pending=pending, approved=approved)


@app.route("/platform/coaches/<int:coach_id>/approve", methods=["POST"])
@platform_admin_required
def platform_approve_coach(coach_id):
    conn = get_db()
    conn.execute("UPDATE coaches SET approved = 1 WHERE id = ?", (coach_id,))
    conn.commit()
    conn.close()
    flash("Coach approved.", "success")
    return redirect(url_for("platform_coaches"))


@app.route("/platform/coaches/<int:coach_id>/revoke", methods=["POST"])
@platform_admin_required
def platform_revoke_coach(coach_id):
    conn = get_db()
    conn.execute("UPDATE coaches SET approved = 0 WHERE id = ?", (coach_id,))
    conn.commit()
    conn.close()
    flash("Access revoked.", "success")
    return redirect(url_for("platform_coaches"))


@app.route("/platform/coaches/<int:coach_id>/reject", methods=["POST"])
@platform_admin_required
def platform_reject_coach(coach_id):
    conn = get_db()
    conn.execute("DELETE FROM coaches WHERE id = ?", (coach_id,))
    conn.commit()
    conn.close()
    flash("Coach account removed.", "success")
    return redirect(url_for("platform_coaches"))


# ---------- Routes: platform admin (cross-org owner access) ----------
#
# Everything below is reachable only at /platform/... - deliberately never
# linked from base.html's nav (which is shared by every organization's own
# site), so it stays a private side of the site rather than something that
# shows up for a customer's own admins. It's gated purely by
# platform_admin_required (session["is_platform_admin"]), independent of
# which org, if any, the platform admin's own session happens to be scoped
# to - see ORG_EXEMPT_ENDPOINTS below.

@app.route("/platform/organizations")
@platform_admin_required
def platform_organizations():
    conn = get_db()
    orgs = conn.execute(
        """SELECT o.*,
                  (SELECT COUNT(*) FROM players WHERE organization_id = o.id) AS player_count,
                  (SELECT COUNT(*) FROM teams WHERE organization_id = o.id) AS team_count,
                  (SELECT COUNT(*) FROM videos WHERE organization_id = o.id) AS video_count,
                  (SELECT COUNT(*) FROM users WHERE organization_id = o.id) AS user_count
           FROM organizations o
           ORDER BY o.name COLLATE NOCASE ASC"""
    ).fetchall()
    conn.close()
    return render_template("platform_organizations.html", orgs=orgs)


def _platform_get_org(conn, org_id):
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        conn.close()
        abort(404)
    return org


@app.route("/platform/organizations/<int:org_id>")
@platform_admin_required
def platform_org_detail(org_id):
    conn = get_db()
    org = _platform_get_org(conn, org_id)
    teams = conn.execute(
        """SELECT t.*, COUNT(p.id) AS player_count
           FROM teams t LEFT JOIN players p ON p.team_id = t.id
           WHERE t.organization_id = ?
           GROUP BY t.id ORDER BY t.name COLLATE NOCASE ASC""",
        (org_id,),
    ).fetchall()
    players = conn.execute(
        """SELECT p.*, t.name AS team_name FROM players p
           LEFT JOIN teams t ON t.id = p.team_id
           WHERE p.organization_id = ?
           ORDER BY p.name COLLATE NOCASE ASC""",
        (org_id,),
    ).fetchall()
    videos = conn.execute(
        """SELECT v.*, p.name AS player_name FROM videos v
           JOIN players p ON p.id = v.player_id
           WHERE v.organization_id = ?
           ORDER BY v.entry_date DESC, v.id DESC""",
        (org_id,),
    ).fetchall()
    conn.close()
    return render_template("platform_org_detail.html", org=org, teams=teams, players=players, videos=videos)


@app.route("/platform/organizations/<int:org_id>/trial/grant", methods=["POST"])
@platform_admin_required
def platform_grant_trial(org_id):
    """Comp a specific org a free trial - no card, no Stripe subscription
    needed. This is the manual, per-org alternative to every signup getting
    a trial automatically (see TRIAL_DAYS)."""
    try:
        days = max(1, int(request.form.get("days", "30")))
    except ValueError:
        days = 30
    ends_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    org = _platform_get_org(conn, org_id)
    if not org:
        conn.close()
        abort(404)
    conn.execute("UPDATE organizations SET trial_ends_at = ? WHERE id = ?", (ends_at, org_id))
    conn.commit()
    conn.close()
    flash(f"{org['name']} now has a free trial through {ends_at[:10]}.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/trial/revoke", methods=["POST"])
@platform_admin_required
def platform_revoke_trial(org_id):
    conn = get_db()
    org = _platform_get_org(conn, org_id)
    if not org:
        conn.close()
        abort(404)
    conn.execute("UPDATE organizations SET trial_ends_at = NULL WHERE id = ?", (org_id,))
    conn.commit()
    conn.close()
    flash(f"Ended {org['name']}'s free trial.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/teams/<int:team_id>/save", methods=["POST"])
@platform_admin_required
def platform_save_team(org_id, team_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("platform_org_detail", org_id=org_id))

    conn = get_db()
    team = conn.execute(
        "SELECT logo_filename FROM teams WHERE id = ? AND organization_id = ?", (team_id, org_id)
    ).fetchone()
    if not team:
        conn.close()
        abort(404)

    logo_filename = team["logo_filename"]
    logo = request.files.get("logo")
    if logo and logo.filename:
        if not MEDIA_ENABLED:
            flash("Name saved, but the logo wasn't saved - media storage isn't configured on this deployment.", "error")
        elif not allowed_file(logo.filename, ALLOWED_TEAM_LOGO_EXT):
            flash(f'Name saved, but "{logo.filename}" isn\'t a supported image type (use PNG, JPG, GIF, WEBP, or HEIC).', "error")
        else:
            old_logo_filename = logo_filename
            safe_name = secure_filename(logo.filename)
            logo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            upload_media(logo.stream, f"uploads/team-logos/{logo_filename}", logo.content_type)
            if old_logo_filename:
                delete_media(f"uploads/team-logos/{old_logo_filename}")

    try:
        conn.execute(
            "UPDATE teams SET name = ?, logo_filename = ? WHERE id = ? AND organization_id = ?",
            (name, logo_filename, team_id, org_id),
        )
        conn.commit()
        flash("Team updated.", "success")
    except sqlite3.IntegrityError:
        flash(f"A team named {name} already exists in that org.", "error")
    conn.close()
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/teams/<int:team_id>/delete", methods=["POST"])
@platform_admin_required
def platform_delete_team(org_id, team_id):
    conn = get_db()
    team = conn.execute(
        "SELECT logo_filename FROM teams WHERE id = ? AND organization_id = ?", (team_id, org_id)
    ).fetchone()
    conn.execute("UPDATE players SET team_id = NULL WHERE team_id = ? AND organization_id = ?", (team_id, org_id))
    conn.execute(
        "UPDATE throwing_entries SET team_id = NULL WHERE team_id = ? AND organization_id = ?", (team_id, org_id)
    )
    conn.execute("DELETE FROM teams WHERE id = ? AND organization_id = ?", (team_id, org_id))
    conn.commit()
    conn.close()
    if team and team["logo_filename"]:
        delete_media(f"uploads/team-logos/{team['logo_filename']}")
    flash("Team deleted.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/players/<int:player_id>/delete", methods=["POST"])
@platform_admin_required
def platform_delete_player(org_id, player_id):
    conn = get_db()
    player = conn.execute(
        "SELECT photo_filename FROM players WHERE id = ? AND organization_id = ?", (player_id, org_id)
    ).fetchone()
    conn.execute("DELETE FROM players WHERE id = ? AND organization_id = ?", (player_id, org_id))
    conn.commit()
    conn.close()
    if player and player["photo_filename"]:
        delete_media(f"uploads/photos/{player['photo_filename']}")
    flash("Player removed.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/videos/<int:video_id>/verify", methods=["POST"])
@platform_admin_required
def platform_verify_video(org_id, video_id):
    conn = get_db()
    conn.execute("UPDATE videos SET verified = 1 WHERE id = ? AND organization_id = ?", (video_id, org_id))
    conn.commit()
    conn.close()
    flash("Video verified.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/videos/<int:video_id>/delete", methods=["POST"])
@platform_admin_required
def platform_delete_video(org_id, video_id):
    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, org_id)
    ).fetchone()
    if video:
        delete_media(f"uploads/videos/{video['filename']}")
        if video["thumbnail_filename"]:
            delete_media(f"uploads/videos/thumbs/{video['thumbnail_filename']}")
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.commit()
    conn.close()
    flash("Video removed.", "success")
    return redirect(url_for("platform_org_detail", org_id=org_id))


@app.route("/platform/organizations/<int:org_id>/enter", methods=["POST"])
@platform_admin_required
def platform_enter_org(org_id):
    """Drop the platform admin straight into an org's own real site, with
    full owner permissions, using that org's own existing owner account
    rather than creating any new/visible one - there's nothing for that
    org's own staff to spot on their Users page, since no new row exists.
    Deliberately does NOT clear platform_admin_id from the session, so the
    "Exit to Platform Admin" banner (see base.html) can tell this apart
    from a real owner logging in, and so leaving doesn't require logging
    into /platform/login again."""
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        conn.close()
        abort(404)
    owner = conn.execute(
        "SELECT * FROM users WHERE organization_id = ? AND is_owner = 1 ORDER BY id ASC LIMIT 1", (org_id,)
    ).fetchone()
    conn.close()
    if not owner:
        flash(f"{org['name']} doesn't have an owner account yet.", "error")
        return redirect(url_for("platform_org_detail", org_id=org_id))

    session["user_id"] = owner["id"]
    session["user_name"] = owner["name"]
    session["is_admin"] = True
    session["is_owner"] = True
    session["is_platform_admin"] = False
    session["player_id"] = owner["player_id"]
    session["organization_id"] = org_id
    return redirect(url_for("index", org_slug=org["slug"]))


@app.route("/platform/organizations/<int:org_id>/delete", methods=["POST"])
@platform_admin_required
def platform_delete_org(org_id):
    """Permanently wipes an entire organization - every team, player, stat,
    video, comment, invite, and login. Deliberately hard to trigger by
    accident: reaching this requires already being on that one org's detail
    page, expanding the collapsed Danger Zone, and typing the organization's
    exact name into a field the submit button stays disabled without (see
    platform_org_detail.html) - re-checked here server-side too, since a
    client-side check alone is just a suggestion."""
    conn = get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        conn.close()
        abort(404)

    confirm_name = (request.form.get("confirm_name") or "").strip()
    if confirm_name != org["name"]:
        conn.close()
        flash(f'That didn\'t match "{org["name"]}" exactly, so nothing was deleted.', "error")
        return redirect(url_for("platform_org_detail", org_id=org_id))

    # Every R2 object this org owns has to be gathered before any row is
    # deleted - there's no way to look these filenames up again afterward.
    photo_files = [r["photo_filename"] for r in conn.execute(
        "SELECT photo_filename FROM players WHERE organization_id = ? AND photo_filename IS NOT NULL", (org_id,)
    ).fetchall()]
    video_files = [r["filename"] for r in conn.execute(
        "SELECT filename FROM videos WHERE organization_id = ?", (org_id,)
    ).fetchall()]
    video_thumb_files = [r["thumbnail_filename"] for r in conn.execute(
        "SELECT thumbnail_filename FROM videos WHERE organization_id = ? AND thumbnail_filename IS NOT NULL", (org_id,)
    ).fetchall()]
    team_logo_files = [r["logo_filename"] for r in conn.execute(
        "SELECT logo_filename FROM teams WHERE organization_id = ? AND logo_filename IS NOT NULL", (org_id,)
    ).fetchall()]

    # Deletion order matters under PRAGMA foreign_keys = ON (see get_db()).
    # Deleting players first cascades to their stat entries, videos,
    # trackman rows, comments, and contacts automatically (all declared
    # ON DELETE CASCADE off players.id); deleting throwing_entries cascades
    # to calendar comments the same way. teams/invite_links/users have no
    # cascade tied to organizations, so they're deleted explicitly.
    conn.execute("DELETE FROM players WHERE organization_id = ?", (org_id,))
    conn.execute("DELETE FROM throwing_entries WHERE organization_id = ?", (org_id,))
    conn.execute("DELETE FROM teams WHERE organization_id = ?", (org_id,))
    conn.execute("DELETE FROM invite_links WHERE organization_id = ?", (org_id,))
    conn.execute("DELETE FROM users WHERE organization_id = ?", (org_id,))
    conn.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
    conn.commit()
    conn.close()

    for fn in photo_files:
        delete_media(f"uploads/photos/{fn}")
    for fn in video_files:
        delete_media(f"uploads/videos/{fn}")
    for fn in video_thumb_files:
        delete_media(f"uploads/videos/thumbs/{fn}")
    for fn in team_logo_files:
        delete_media(f"uploads/team-logos/{fn}")
    if org["logo_filename"]:
        delete_media(f"uploads/logos/{org['logo_filename']}")

    flash(f'{org["name"]} and everything in it has been permanently deleted.', "success")
    return redirect(url_for("platform_organizations"))


# ---------- Routes: platform admins (invite/manage other platform admins) ----------

@app.route("/platform/admins")
@platform_admin_required
def platform_admins_page():
    conn = get_db()
    admins = conn.execute(
        "SELECT id, name, email, created_at, password_hash FROM platform_admins ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return render_template("platform_admins.html", admins=admins)


@app.route("/platform/admins/add", methods=["POST"])
@platform_admin_required
def platform_admin_add():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    if not name or not email:
        flash("Name and email are both required.", "error")
        return redirect(url_for("platform_admins_page"))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO platform_admins (name, email, password_hash) VALUES (?, ?, NULL)",
            (name, email),
        )
        conn.commit()

        login_url = url_for("platform_login", _external=True)
        if email_configured() and send_platform_admin_invite_email(email, name, login_url):
            flash(f"Invited {name} as a platform admin - they'll get an email with a sign-in link and set their own password.", "success")
        else:
            flash(f"Added {name} as a platform admin, but the invite email couldn't be sent. Have them sign in at {login_url} with their email and a blank password to set one up.", "error")
    except sqlite3.IntegrityError:
        flash(f"A platform admin with email {email} already exists.", "error")
    conn.close()
    return redirect(url_for("platform_admins_page"))


@app.route("/platform/admins/<int:admin_id>/resend-invite", methods=["POST"])
@platform_admin_required
def platform_admin_resend_invite(admin_id):
    conn = get_db()
    admin = conn.execute("SELECT * FROM platform_admins WHERE id = ?", (admin_id,)).fetchone()
    conn.close()
    if not admin:
        flash("That platform admin no longer exists.", "error")
        return redirect(url_for("platform_admins_page"))
    if admin["password_hash"]:
        flash(f"{admin['name']} already signed in and set a password - there's no invite to resend.", "error")
        return redirect(url_for("platform_admins_page"))

    login_url = url_for("platform_login", _external=True)
    if email_configured() and send_platform_admin_invite_email(admin["email"], admin["name"], login_url):
        flash(f"Resent the invite to {admin['name']}.", "success")
    else:
        flash(f"Couldn't send the invite email. Have them sign in at {login_url} with their email and a blank password.", "error")
    return redirect(url_for("platform_admins_page"))


@app.route("/platform/admins/<int:admin_id>/delete", methods=["POST"])
@platform_admin_required
def platform_admin_delete(admin_id):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM platform_admins").fetchone()[0]
    if total <= 1:
        conn.close()
        flash("Can't remove the last platform admin - you'd lock everyone out of this area.", "error")
        return redirect(url_for("platform_admins_page"))
    conn.execute("DELETE FROM platform_admins WHERE id = ?", (admin_id,))
    conn.commit()
    conn.close()
    flash("Platform admin removed.", "success")
    return redirect(url_for("platform_admins_page"))


# ---------- Routes: user management (admin only) ----------

def can_manage_user(target):
    """Admins have near-full access, same as the site owner: anyone can be
    managed except the owner, who can't be managed by anyone (including themselves)."""
    if target is None or target["is_owner"]:
        return False
    return True


@app.route("/<org_slug>/users")
@admin_required
def users_page():
    conn = get_db()
    users = conn.execute(
        """SELECT u.*, p.name AS player_name FROM users u
           LEFT JOIN players p ON p.id = u.player_id
           WHERE u.organization_id = ?
           ORDER BY u.name COLLATE NOCASE ASC""",
        (g.org["id"],),
    ).fetchall()
    invites = conn.execute(
        "SELECT * FROM invite_links WHERE organization_id = ? ORDER BY created_at DESC", (g.org["id"],)
    ).fetchall()
    all_players = conn.execute(
        "SELECT id, name FROM players WHERE organization_id = ? ORDER BY name COLLATE NOCASE ASC", (g.org["id"],)
    ).fetchall()
    conn.close()
    owners = [u for u in users if u["is_owner"]]
    admins = [u for u in users if u["is_admin"] and not u["is_owner"]]
    members = [u for u in users if not u["is_admin"] and not u["is_owner"]]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "users.html", owners=owners, admins=admins, members=members,
        invites=invites, now=now, all_players=all_players,
    )


@app.route("/<org_slug>/users/<int:user_id>/link-player", methods=["POST"])
@admin_required
def link_user_player(user_id):
    conn = get_db()
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND organization_id = ?", (user_id, g.org["id"])
    ).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You don't have permission to edit that account.", "error")
        return redirect(url_for("users_page"))
    if target["is_admin"]:
        conn.close()
        flash("Admins don't have linked players.", "error")
        return redirect(url_for("users_page"))
    raw = request.form.get("player_id", "").strip()
    player_id = int(raw) if raw.isdigit() else None
    if player_id is not None:
        owned = conn.execute(
            "SELECT id FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
        ).fetchone()
        if not owned:
            conn.close()
            flash("That player isn't part of this organization.", "error")
            return redirect(url_for("users_page"))
    conn.execute("UPDATE users SET player_id = ? WHERE id = ?", (player_id, user_id))
    conn.commit()
    conn.close()
    flash("Updated - they'll land on that player's page when they sign in." if player_id else "Player link removed.", "success")
    return redirect(url_for("users_page"))


@app.route("/<org_slug>/invites/create", methods=["POST"])
@admin_required
def create_invite():
    token = secrets.token_urlsafe(16)
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute(
        "INSERT INTO invite_links (organization_id, token, created_by, expires_at) VALUES (?, ?, ?, ?)",
        (g.org["id"], token, session["user_id"], expires),
    )
    conn.commit()
    conn.close()
    flash("Invite link created - it works for 7 days. Copy it and send it to whoever you want to let in.", "success")
    return redirect(url_for("users_page"))


@app.route("/<org_slug>/invites/<int:invite_id>/delete", methods=["POST"])
@admin_required
def delete_invite(invite_id):
    conn = get_db()
    conn.execute("DELETE FROM invite_links WHERE id = ? AND organization_id = ?", (invite_id, g.org["id"]))
    conn.commit()
    conn.close()
    flash("Invite link deactivated.", "success")
    return redirect(url_for("users_page"))


@app.route("/join/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def join(token):
    conn = get_db()
    invite = conn.execute("SELECT * FROM invite_links WHERE token = ?", (token,)).fetchone()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # An invalid/unknown token means there's no org to send them back to
    # (login itself requires one) - the org picker is the only safe fallback.
    # An expired-but-real invite still knows its org, so it can link there.
    if not invite:
        conn.close()
        return render_template("join.html", invalid=True, token=token, back_url=url_for("org_picker"))

    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (invite["organization_id"],)).fetchone()
    if not org:
        conn.close()
        return render_template("join.html", invalid=True, token=token, back_url=url_for("org_picker"))

    if invite["expires_at"] and now > invite["expires_at"]:
        conn.close()
        return render_template(
            "join.html",
            invalid=True,
            token=token,
            back_url=url_for("login", org_slug=org["slug"]),
            **branding_context_for_org(org),
        )

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
                    "INSERT INTO users (organization_id, name, email, phone, password_hash, is_admin) VALUES (?, ?, ?, ?, ?, 0)",
                    (org["id"], name, email or None, phone, generate_password_hash(password)),
                )
                conn.commit()
                session.permanent = True
                session["user_id"] = cur.lastrowid
                session["user_name"] = name
                session["is_admin"] = False
                session["is_owner"] = False
                session["is_platform_admin"] = False
                session["player_id"] = None
                session["organization_id"] = org["id"]
                conn.close()
                flash(f"Welcome, {name}! Your account is ready.", "success")
                return redirect(url_for("index", org_slug=org["slug"]))
            except sqlite3.IntegrityError:
                flash("An account with that email or phone already exists - try signing in instead.", "error")
        conn.close()
        return redirect(url_for("join", token=token))

    conn.close()
    return render_template(
        "join.html",
        invalid=False,
        token=token,
        **branding_context_for_org(org),
    )


@app.route("/<org_slug>/users/add", methods=["POST"])
@admin_required
def add_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower() or None
    phone = normalize_phone(request.form.get("phone", ""))
    # Only the site owner can create admins; anyone an admin invites is a member.
    is_admin = 1 if request.form.get("is_admin") else 0
    raw_player = request.form.get("player_id", "").strip()
    # Admins don't get a linked player - that's for parent/player accounts only.
    player_id = int(raw_player) if (raw_player.isdigit() and not is_admin) else None

    if not email and not phone:
        flash("Enter an email or a phone number (or both) so they can sign in.", "error")
        return redirect(url_for("users_page"))

    conn = get_db()
    if player_id is not None:
        owned = conn.execute(
            "SELECT id FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
        ).fetchone()
        if not owned:
            player_id = None
    try:
        conn.execute(
            "INSERT INTO users (organization_id, name, email, phone, is_admin, player_id) VALUES (?, ?, ?, ?, ?, ?)",
            (g.org["id"], name, email, phone, is_admin, player_id),
        )
        conn.commit()

        login_url = url_for("login", org_slug=g.org["slug"], _external=True)
        sent_via = []
        if email and email_configured() and send_invite_email(email, g.org["name"], login_url):
            sent_via.append("email")
        if phone and sms_configured() and send_invite_sms(phone, g.org["name"], login_url):
            sent_via.append("text")

        if sent_via:
            flash(f"Added {name or email or phone} and sent them an invite by {' and '.join(sent_via)}.", "success")
        else:
            flash(f"Added {name or email or phone}. They can now sign in and create their password.", "success")
    except sqlite3.IntegrityError:
        flash("A user with that email or phone already exists.", "error")
    conn.close()
    return redirect(url_for("users_page"))


@app.route("/<org_slug>/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    conn = get_db()
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND organization_id = ?", (user_id, g.org["id"])
    ).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You don't have permission to remove that account.", "error")
        return redirect(url_for("users_page"))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User removed - they can no longer sign in.", "success")
    return redirect(url_for("users_page"))


@app.route("/<org_slug>/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def reset_user_password(user_id):
    conn = get_db()
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND organization_id = ?", (user_id, g.org["id"])
    ).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You don't have permission to reset that account's password.", "error")
        return redirect(url_for("users_page"))
    conn.execute("UPDATE users SET password_hash = NULL WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("Password cleared - they'll create a new one next time they sign in.", "success")
    return redirect(url_for("users_page"))


@app.route("/<org_slug>/users/<int:user_id>/role", methods=["POST"])
@admin_required
def change_user_role(user_id):
    """Any admin can promote a member to admin or demote an admin to member.
    The site owner can't be touched by anyone, including themselves."""
    conn = get_db()
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND organization_id = ?", (user_id, g.org["id"])
    ).fetchone()
    if not can_manage_user(target):
        conn.close()
        flash("You can't change that account's role.", "error")
        return redirect(url_for("users_page"))
    new_role = 0 if target["is_admin"] else 1
    # Admins don't have a linked player - clear it when promoting.
    if new_role == 1:
        conn.execute("UPDATE users SET is_admin = ?, player_id = NULL WHERE id = ?", (new_role, user_id))
    else:
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    flash(f"{target['name'] or target['email'] or target['phone']} is now {'an admin' if new_role else 'a member'}.", "success")
    return redirect(url_for("users_page"))


# ---------- Routes: leaderboard ----------

@app.route("/<org_slug>/leaderboard")
def leaderboard():
    team_filter = request.args.get("team", "").strip()
    grad_year_filter = request.args.get("grad_year", "").strip()
    conn = get_db()
    all_teams = _all_teams(conn, g.org["id"])
    grad_years = [
        row[0] for row in conn.execute(
            """SELECT DISTINCT grad_year FROM players
               WHERE organization_id = ? AND grad_year IS NOT NULL AND grad_year != ''
               ORDER BY grad_year ASC""",
            (g.org["id"],),
        ).fetchall()
    ]

    filter_cond = " AND p.organization_id = ?"
    params = [g.org["id"]]
    if team_filter.isdigit():
        filter_cond += " AND p.team_id = ?"
        params.append(int(team_filter))
    if grad_year_filter:
        filter_cond += " AND p.grad_year = ?"
        params.append(grad_year_filter)

    # Split the same way as the coach portal's leaderboard - a Pulldown
    # max-effort throw and a Bullpen fastball aren't the same measurement,
    # so they shouldn't get blended into one "any pitch" number.
    bullpen_velo_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, MAX(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE lower(s.stat_name) LIKE '%velo%' AND s.category = 'Bullpen'{filter_cond}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    pulldown_velo_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, MAX(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE lower(s.stat_name) LIKE '%velo%' AND s.category = 'Pulldown'{filter_cond}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    strike_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name,
                   ROUND(AVG(s.stat_value), 1) AS value, COUNT(*) AS sessions
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE s.stat_name = 'Strike %'{filter_cond}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    k_leaders = conn.execute(
        f"""SELECT p.id, p.name, t.name AS team_name, SUM(s.stat_value) AS value
            FROM stat_entries s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE lower(s.stat_name) IN ('k', 'so', 'strikeouts', 'ks'){filter_cond}
            GROUP BY p.id ORDER BY value DESC LIMIT 10""",
        params,
    ).fetchall()

    conn.close()
    return render_template(
        "leaderboard.html", bullpen_velo_leaders=bullpen_velo_leaders,
        pulldown_velo_leaders=pulldown_velo_leaders, strike_leaders=strike_leaders,
        k_leaders=k_leaders, teams=all_teams, team_filter=team_filter,
        grad_years=grad_years, grad_year_filter=grad_year_filter,
    )


# ---------- Routes: player roster ----------

@app.route("/<org_slug>/")
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
    where = ["p.organization_id = ?"]
    params = [g.org["id"]]
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
    all_teams = _all_teams(conn, g.org["id"])
    conn.close()

    # Roster convention: alphabetize by last name (then first name to break
    # ties), not just the raw "First Last" string the SQL sort above gives.
    def _last_name_key(p):
        parts = (p["name"] or "").split()
        last = parts[-1] if parts else ""
        first = parts[0] if parts else ""
        return (last.lower(), first.lower())

    players = sorted(players, key=_last_name_key)

    return render_template("index.html", players=players, teams=all_teams, q=q, team_filter=team_filter)


# ---------- Routes: teams ----------

@app.route("/<org_slug>/teams")
def teams_page():
    conn = get_db()
    team_rows = conn.execute(
        """SELECT t.*, COUNT(p.id) AS player_count
           FROM teams t LEFT JOIN players p ON p.team_id = t.id
           WHERE t.organization_id = ?
           GROUP BY t.id
           ORDER BY t.name COLLATE NOCASE ASC""",
        (g.org["id"],),
    ).fetchall()
    unassigned_count = conn.execute(
        "SELECT COUNT(*) FROM players WHERE team_id IS NULL AND organization_id = ?", (g.org["id"],)
    ).fetchone()[0]
    conn.close()
    return render_template("teams.html", teams=team_rows, unassigned_count=unassigned_count)


@app.route("/<org_slug>/teams/add", methods=["POST"])
def add_team():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams_page"))

    logo_filename = None
    logo = request.files.get("logo")
    if logo and logo.filename:
        if not MEDIA_ENABLED:
            flash("Team added, but the logo wasn't saved - media storage isn't configured on this deployment.", "error")
        elif not allowed_file(logo.filename, ALLOWED_TEAM_LOGO_EXT):
            flash(f'Team added, but "{logo.filename}" isn\'t a supported image type (use PNG, JPG, GIF, WEBP, or HEIC).', "error")
        else:
            safe_name = secure_filename(logo.filename)
            logo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            upload_media(logo.stream, f"uploads/team-logos/{logo_filename}", logo.content_type)

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO teams (organization_id, name, logo_filename) VALUES (?, ?, ?)",
            (g.org["id"], name, logo_filename),
        )
        conn.commit()
        flash(f"Added team {name}.", "success")
    except sqlite3.IntegrityError:
        flash(f"A team named {name} already exists.", "error")
        if logo_filename:
            delete_media(f"uploads/team-logos/{logo_filename}")
    conn.close()
    return redirect(url_for("teams_page"))


@app.route("/<org_slug>/teams/<int:team_id>/rename", methods=["POST"])
def rename_team(team_id):
    # Despite the name, this route also handles updating a team's logo -
    # both live in the same "Rename / Delete" form on the Teams page, so one
    # POST covers both instead of splitting into a separate endpoint.
    name = request.form.get("name", "").strip()
    if not name:
        flash("Team name is required.", "error")
        return redirect(url_for("teams_page"))
    conn = get_db()
    team = conn.execute(
        "SELECT logo_filename FROM teams WHERE id = ? AND organization_id = ?", (team_id, g.org["id"])
    ).fetchone()
    if not team:
        conn.close()
        abort(404)

    logo_filename = team["logo_filename"]
    logo = request.files.get("logo")
    if logo and logo.filename:
        if not MEDIA_ENABLED:
            flash("Name saved, but the logo wasn't saved - media storage isn't configured on this deployment.", "error")
        elif not allowed_file(logo.filename, ALLOWED_TEAM_LOGO_EXT):
            flash(f'Name saved, but "{logo.filename}" isn\'t a supported image type (use PNG, JPG, GIF, WEBP, or HEIC).', "error")
        else:
            old_logo_filename = logo_filename
            safe_name = secure_filename(logo.filename)
            logo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            upload_media(logo.stream, f"uploads/team-logos/{logo_filename}", logo.content_type)
            if old_logo_filename:
                delete_media(f"uploads/team-logos/{old_logo_filename}")

    try:
        conn.execute(
            "UPDATE teams SET name = ?, logo_filename = ? WHERE id = ? AND organization_id = ?",
            (name, logo_filename, team_id, g.org["id"]),
        )
        conn.commit()
        flash("Team updated.", "success")
    except sqlite3.IntegrityError:
        flash(f"A team named {name} already exists.", "error")
    conn.close()
    return redirect(url_for("teams_page"))


@app.route("/<org_slug>/teams/<int:team_id>/delete", methods=["POST"])
def delete_team(team_id):
    conn = get_db()
    team = conn.execute(
        "SELECT logo_filename FROM teams WHERE id = ? AND organization_id = ?", (team_id, g.org["id"])
    ).fetchone()
    conn.execute(
        "UPDATE players SET team_id = NULL WHERE team_id = ? AND organization_id = ?", (team_id, g.org["id"])
    )
    conn.execute(
        "UPDATE throwing_entries SET team_id = NULL WHERE team_id = ? AND organization_id = ?",
        (team_id, g.org["id"]),
    )
    conn.execute("DELETE FROM teams WHERE id = ? AND organization_id = ?", (team_id, g.org["id"]))
    conn.commit()
    conn.close()
    if team and team["logo_filename"]:
        delete_media(f"uploads/team-logos/{team['logo_filename']}")
    flash("Team deleted. Its players are now unassigned and its calendar entries moved to the General calendar.", "success")
    return redirect(url_for("teams_page"))


# ---------- Routes: players ----------

def _team_id_from_form(conn, organization_id):
    """The team dropdown posts a team id, or empty string for 'No team'.
    Only accepted if that team actually belongs to this organization -
    otherwise a crafted request could attach a player to another org's team."""
    raw = request.form.get("team_id", "").strip()
    if not raw.isdigit():
        return None
    team_id = int(raw)
    owned = conn.execute(
        "SELECT id FROM teams WHERE id = ? AND organization_id = ?", (team_id, organization_id)
    ).fetchone()
    return team_id if owned else None


def _all_teams(conn, organization_id):
    return conn.execute(
        "SELECT * FROM teams WHERE organization_id = ? ORDER BY name COLLATE NOCASE ASC", (organization_id,)
    ).fetchall()


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


@app.route("/<org_slug>/players/add", methods=["GET", "POST"])
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
        contact = {f: request.form.get(f, "").strip() for f in PLAYER_CONTACT_FIELDS}
        measurables = {f: request.form.get(f, "").strip() for f in PLAYER_MEASURABLE_FIELDS}

        photo_filename = None
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT) and MEDIA_ENABLED:
            safe_name = secure_filename(photo.filename)
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            upload_media(photo.stream, f"uploads/photos/{photo_filename}", photo.content_type)

        conn = get_db()
        team_id = _team_id_from_form(conn, g.org["id"])
        contact_cols = ", ".join(PLAYER_CONTACT_FIELDS)
        contact_marks = ", ".join("?" for _ in PLAYER_CONTACT_FIELDS)
        measurable_cols = ", ".join(PLAYER_MEASURABLE_FIELDS)
        measurable_marks = ", ".join("?" for _ in PLAYER_MEASURABLE_FIELDS)
        cur = conn.execute(
            f"INSERT INTO players (organization_id, name, jersey_number, position, grad_year, photo_filename, notes, team_id, {contact_cols}, {measurable_cols}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {contact_marks}, {measurable_marks})",
            (g.org["id"], name, jersey_number, position, grad_year, photo_filename, notes, team_id,
             *[contact[f] for f in PLAYER_CONTACT_FIELDS],
             *[measurables[f] for f in PLAYER_MEASURABLE_FIELDS]),
        )
        _save_contacts(conn, cur.lastrowid, _contacts_from_form())
        conn.commit()

        # Plan enforcement is deliberately additive, not a hard block: an org
        # can always add the player, they just start incurring the plan's
        # per-player overage rate once they're past the included cap. Orgs
        # with no active subscription (org_plan_cap() -> None) are unaffected.
        cap = org_plan_cap(g.org)
        if cap is not None:
            count = org_player_count(conn, g.org["id"])
            sync_overage_quantity(g.org)
            if count == cap + 1:
                overage_rate = PLANS.get(g.org["plan"], {}).get("overage_amount", 0)
                flash(
                    f"Added {name}. You're now over your {g.org['plan'].title()} plan's {cap}-player "
                    f"cap, so extra players are billed at ${overage_rate}/player this cycle.",
                    "success",
                )
                conn.close()
                return redirect(url_for("index"))

        conn.close()
        flash(f"Added {name} to the player page.", "success")
        return redirect(url_for("index"))

    conn = get_db()
    all_teams = _all_teams(conn, g.org["id"])
    conn.close()
    return render_template("add_player.html", teams=all_teams, contacts=[])


def _build_player_profile_context(conn, player_id, date_from, date_to, is_coach_view=False):
    """Everything a player's profile page needs, besides the `player` row
    itself: stat tables, velocity charts, video timeline, comments, TrackMan
    sessions. Shared between the org-member player page and the coach
    portal's read-only view of the same player, so both stay in sync
    automatically instead of drifting apart as two separate implementations."""
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

    # Same-day stat snapshot for the video timeline: if a player has, say,
    # Pulldown data recorded the same day as a video, that day's numbers show
    # up right alongside the clip instead of only living in the tables above.
    stats_by_date = {}
    for row in stat_rows:
        cat = row["category"] or "General"
        stats_by_date.setdefault(row["entry_date"], {}).setdefault(cat, []).append(
            {"stat_name": row["stat_name"], "stat_value": row["stat_value"]}
        )

    # Pinned clips float to the top of the timeline ahead of everything else,
    # then it's newest-first as usual. A player/team can mark individual
    # clips as not visible to recruiters even while opted in overall (e.g. a
    # rough bullpen they don't want a college coach's first impression to
    # be) - that only applies to the coach-portal view; the org's own site
    # always shows every video regardless of recruiting_visible.
    visibility_cond = " AND recruiting_visible = 1" if is_coach_view else ""
    videos = conn.execute(
        f"SELECT * FROM videos WHERE player_id = ?{date_conds}{visibility_cond} ORDER BY pinned DESC, entry_date DESC, id DESC",
        (player_id, *date_params),
    ).fetchall()

    # Pinned videos get their own side-by-side section up top, so they're
    # pulled out of the timeline entirely rather than just sorted first.
    pinned_videos = [v for v in videos if v["pinned"]]
    timeline_videos = [v for v in videos if not v["pinned"]]

    # Group same-day videos into one timeline entry so multiple clips from a
    # single session can be flipped through instead of listed as separate
    # rows. Videos are already ordered entry_date DESC, so consecutive rows
    # with the same date land in the same group automatically.
    video_groups = []
    for v in timeline_videos:
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
        stat_names = sorted(bucket["stat_names"], key=_stat_sort_key)
        dates = sorted(bucket["dates"])

        table_rows = []
        for d in dates:
            row_cells = bucket["cells"].get(d, {})
            table_rows.append({"date": d, "values": [row_cells.get(sn) for sn in stat_names]})

        # Summary row: counting stats (IP, H, K, BB, Pitches, ...) are
        # totaled like a season stat line; velo readings take the best one
        # ever recorded; everything else that's a rate (%, ERA, Avg Spin) is
        # averaged.
        averages = []
        for sn in stat_names:
            vals = [bucket["cells"][d][sn] for d in dates if sn in bucket["cells"].get(d, {})]
            if not vals:
                averages.append(None)
            elif normalize_col(sn) in IP_COL_NAMES:
                averages.append(sum_innings(vals))
            elif is_cumulative_stat(sn):
                averages.append(round(sum(vals), 2))
            elif is_max_stat(sn):
                averages.append(round(max(vals), 2))
            else:
                averages.append(round(sum(vals) / len(vals), 2))

        category_tables.append(
            {"category": cat, "stat_names": stat_names, "rows": table_rows, "averages": averages}
        )

    comments_by_video = {}
    for c in video_comment_rows:
        comments_by_video.setdefault(c["video_id"], []).append(c)

    return {
        "category_tables": category_tables,
        "velocity_by_stat": velocity_by_stat,
        "video_groups": video_groups,
        "stats_by_date": stats_by_date,
        "pinned_videos": pinned_videos,
        "comments_by_video": comments_by_video,
        "general_comments": general_comments,
        "contacts": contacts,
        "date_from": date_from,
        "date_to": date_to,
        "tm_sessions": tm_sessions,
    }


def _player_date_range_from_request():
    """A brand-new visit to a player's profile (no date filter chosen yet)
    defaults to the last 12 months instead of a player's entire history,
    which gets unwieldy for anyone who's been logging stats for years.
    Picking dates explicitly, or clicking "Show All Data" (?range=all),
    both bypass this and show exactly what was asked for. Returns
    (date_from, date_to, is_default_range)."""
    if request.args.get("range") == "all" or "date_from" in request.args or "date_to" in request.args:
        return (
            request.args.get("date_from", "").strip(),
            request.args.get("date_to", "").strip(),
            False,
        )
    return (date.today() - timedelta(days=365)).strftime("%Y-%m-%d"), "", True


@app.route("/<org_slug>/players/<int:player_id>")
def player_detail(player_id):
    conn = get_db()
    player = conn.execute(
        "SELECT p.*, t.name AS team_name FROM players p LEFT JOIN teams t ON t.id = p.team_id WHERE p.id = ? AND p.organization_id = ?",
        (player_id, g.org["id"]),
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    # Optional ?date_from= / ?date_to= narrow every dated thing on the page
    # (charts, stat tables, videos) to that range. Missing ends are open,
    # unless neither is present at all, in which case _player_date_range_
    # from_request() applies the last-12-months default.
    date_from, date_to, is_default_range = _player_date_range_from_request()
    ctx = _build_player_profile_context(conn, player_id, date_from, date_to)
    conn.close()
    return render_template("player.html", player=player, is_coach_view=False, is_default_range=is_default_range, **ctx)


@app.route("/<org_slug>/players/<int:player_id>/report")
def player_report(player_id):
    conn = get_db()
    player = conn.execute(
        "SELECT p.*, t.name AS team_name FROM players p LEFT JOIN teams t ON t.id = p.team_id WHERE p.id = ? AND p.organization_id = ?",
        (player_id, g.org["id"]),
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    contacts = conn.execute(
        "SELECT * FROM player_contacts WHERE player_id = ? ORDER BY id ASC", (player_id,)
    ).fetchall()
    stat_rows = conn.execute(
        "SELECT entry_date, category, stat_name, stat_value FROM stat_entries WHERE player_id = ?",
        (player_id,),
    ).fetchall()
    tm_types = conn.execute(
        """SELECT pitch_type, COUNT(*) AS pitches,
                  ROUND(AVG(spin_rate)) AS avg_spin,
                  ROUND(AVG(ivb), 1) AS avg_ivb, ROUND(AVG(hb), 1) AS avg_hb
           FROM trackman_pitches
           WHERE player_id = ? AND pitch_type IS NOT NULL
           GROUP BY pitch_type ORDER BY pitches DESC""",
        (player_id,),
    ).fetchall()
    conn.close()

    # Career bests: highest value of every velo stat (chart-style name merge).
    # Pulldown is excluded - it's a different kind of session, so its "Max
    # Velo" doesn't count toward the report's velocity numbers. Only
    # Bullpen / Live ABs / Game sessions feed this.
    bests = {}
    for row in stat_rows:
        if row["category"] == "Pulldown":
            continue
        if "velo" in row["stat_name"].lower():
            name = " ".join(w for w in row["stat_name"].split() if w.lower() != "top")
            bests[name] = max(bests.get(name, 0), row["stat_value"])
    bests = sorted(bests.items(), key=lambda kv: -kv[1])

    # Game totals: sum the counting stats from Game sessions, then derive
    # ERA / K/7 from the totals.
    game_totals = {}
    ip_vals = []
    for row in stat_rows:
        if row["category"] != "Game":
            continue
        sn = row["stat_name"]
        if normalize_col(sn) in IP_COL_NAMES:
            ip_vals.append(row["stat_value"])
        elif is_cumulative_stat(sn):
            game_totals[sn] = game_totals.get(sn, 0) + row["stat_value"]
    if ip_vals:
        game_totals["IP"] = sum_innings(ip_vals)
        ip_true = sum(int(v) + ({1: 1/3, 2: 2/3}.get(round((v - int(v)) * 10), 0)) for v in ip_vals)
        er = next((v for k, v in game_totals.items() if normalize_col(k) in ER_COL_NAMES), None)
        k = next((v for k, v in game_totals.items() if normalize_col(k) in K_COL_NAMES), None)
        if ip_true > 0 and er is not None:
            game_totals["ERA"] = round(er / ip_true * INNINGS_PER_GAME, 2)
        if ip_true > 0 and k is not None:
            game_totals["K/7"] = round(k / ip_true * INNINGS_PER_GAME, 2)

    session_count = len({(r["entry_date"], r["category"]) for r in stat_rows})

    return render_template(
        "report.html", player=player, contacts=contacts, bests=bests,
        game_totals=game_totals, tm_types=tm_types, session_count=session_count,
        today=date.today().strftime("%B %d, %Y"),
    )


@app.route("/<org_slug>/players/<int:player_id>/edit", methods=["GET", "POST"])
def edit_player(player_id):
    conn = get_db()
    player = conn.execute(
        "SELECT * FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    # A member can only edit the player account they're linked to - editing
    # someone else's profile is admin/owner territory.
    if not session.get("is_admin") and session.get("player_id") != player_id:
        conn.close()
        flash("You don't have permission to edit that player's info.", "error")
        return redirect(url_for("player_detail", player_id=player_id))

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
        recruiting_opt_in = 1 if request.form.get("recruiting_opt_in") else 0
        team_id = _team_id_from_form(conn, g.org["id"])
        contact = {f: request.form.get(f, "").strip() for f in PLAYER_CONTACT_FIELDS}
        measurables = {f: request.form.get(f, "").strip() for f in PLAYER_MEASURABLE_FIELDS}

        photo_filename = player["photo_filename"]
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename, ALLOWED_PHOTO_EXT) and MEDIA_ENABLED:
            safe_name = secure_filename(photo.filename)
            old_photo_filename = photo_filename
            photo_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
            upload_media(photo.stream, f"uploads/photos/{photo_filename}", photo.content_type)
            if old_photo_filename:
                delete_media(f"uploads/photos/{old_photo_filename}")

        contact_sets = ", ".join(f"{f} = ?" for f in PLAYER_CONTACT_FIELDS)
        measurable_sets = ", ".join(f"{f} = ?" for f in PLAYER_MEASURABLE_FIELDS)
        conn.execute(
            f"""UPDATE players SET name = ?, jersey_number = ?, position = ?, grad_year = ?,
               notes = ?, photo_filename = ?, team_id = ?, recruiting_opt_in = ?, {contact_sets}, {measurable_sets} WHERE id = ?""",
            (name, jersey_number, position, grad_year, notes, photo_filename, team_id, recruiting_opt_in,
             *[contact[f] for f in PLAYER_CONTACT_FIELDS],
             *[measurables[f] for f in PLAYER_MEASURABLE_FIELDS], player_id),
        )
        _save_contacts(conn, player_id, _contacts_from_form())
        conn.commit()
        conn.close()
        flash(f"Updated {name}.", "success")
        return redirect(url_for("player_detail", player_id=player_id))

    all_teams = _all_teams(conn, g.org["id"])
    contacts = conn.execute(
        "SELECT * FROM player_contacts WHERE player_id = ? ORDER BY id ASC", (player_id,)
    ).fetchall()
    conn.close()
    return render_template("edit_player.html", player=player, teams=all_teams, contacts=contacts)


@app.route("/<org_slug>/players/<int:player_id>/delete", methods=["POST"])
@admin_required
def delete_player(player_id):
    conn = get_db()
    conn.execute("DELETE FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"]))
    conn.commit()
    conn.close()
    sync_overage_quantity(g.org)
    flash("Player removed.", "success")
    return redirect(url_for("index"))


# ---------- Routes: lesson calendar ----------

@app.route("/<org_slug>/calendar")
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
        current_team = conn.execute(
            "SELECT * FROM teams WHERE id = ? AND organization_id = ?", (int(team_param), g.org["id"])
        ).fetchone()
    all_teams = _all_teams(conn, g.org["id"])

    month_start = f"{year:04d}-{month:02d}-01"
    last_day = calendar_module.monthrange(year, month)[1]
    month_end = f"{year:04d}-{month:02d}-{last_day:02d}"

    # General entries (no team) appear on EVERY calendar; a team's calendar
    # shows its own entries plus the general ones.
    if current_team:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE organization_id = ? AND entry_date BETWEEN ? AND ? AND (team_id = ? OR team_id IS NULL)
               ORDER BY entry_date ASC, id ASC""",
            (g.org["id"], month_start, month_end, current_team["id"]),
        ).fetchall()
    else:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE organization_id = ? AND entry_date BETWEEN ? AND ? AND team_id IS NULL
               ORDER BY entry_date ASC, id ASC""",
            (g.org["id"], month_start, month_end),
        ).fetchall()

    # Comments on each lesson day, keyed by entry id, with the delete URL
    # baked in so the calendar's click-to-view popup (built from data-*
    # attributes, not server-rendered HTML) doesn't need to construct routes
    # itself in JS.
    comments_by_entry = {}
    entry_ids = [e["id"] for e in entries]
    if entry_ids:
        placeholders = ",".join("?" * len(entry_ids))
        comment_rows = conn.execute(
            f"SELECT * FROM calendar_entry_comments WHERE entry_id IN ({placeholders}) ORDER BY created_at ASC",
            entry_ids,
        ).fetchall()
        for c in comment_rows:
            comments_by_entry.setdefault(c["entry_id"], []).append({
                "id": c["id"],
                "commenter_name": c["commenter_name"],
                "body": c["body"],
                "created_at": format_comment_time(c["created_at"]),
                "delete_url": url_for("delete_calendar_comment", comment_id=c["id"]) if session.get("is_admin") else None,
            })

    feed_token = get_or_create_feed_token(conn, session["user_id"])
    conn.close()

    entries_by_date = {}
    for e in entries:
        entries_by_date.setdefault(e["entry_date"], []).append(e)

    # Within a day, show entries in time order - whichever ones have a
    # recognizable time first (earliest to latest), then anything with no
    # time (or unparseable text) after, in the order they were added.
    def _entry_sort_key(e):
        minutes = parse_event_time_minutes(e["event_time"])
        return (minutes is None, minutes or 0)

    for day_entries in entries_by_date.values():
        day_entries.sort(key=_entry_sort_key)

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
        comments_by_entry=comments_by_entry,
        feed_token=feed_token,
    )


@app.route("/calendar/feed/<token>.ics")
def calendar_feed(token):
    conn = get_db()
    user = conn.execute(
        "SELECT id, organization_id FROM users WHERE calendar_feed_token = ?", (token,)
    ).fetchone()
    if not user:
        conn.close()
        abort(404)
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (user["organization_id"],)).fetchone()
    if not org:
        conn.close()
        abort(404)

    team_param = request.args.get("team", "").strip()
    current_team = None
    if team_param.isdigit():
        current_team = conn.execute(
            "SELECT * FROM teams WHERE id = ? AND organization_id = ?", (int(team_param), org["id"])
        ).fetchone()

    today = date.today()
    window_start = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    window_end = (today + timedelta(days=365)).strftime("%Y-%m-%d")

    if current_team:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE organization_id = ? AND entry_date BETWEEN ? AND ? AND (team_id = ? OR team_id IS NULL)
               ORDER BY entry_date ASC, id ASC""",
            (org["id"], window_start, window_end, current_team["id"]),
        ).fetchall()
        calendar_name = f"{org['name']} – {current_team['name']}"
    else:
        entries = conn.execute(
            """SELECT * FROM throwing_entries
               WHERE organization_id = ? AND entry_date BETWEEN ? AND ? AND team_id IS NULL
               ORDER BY entry_date ASC, id ASC""",
            (org["id"], window_start, window_end),
        ).fetchall()
        calendar_name = f"{org['name']} – General"

    conn.close()

    ics_text = build_ics_feed(entries, calendar_name, mark_general=bool(current_team))
    response = Response(ics_text, mimetype="text/calendar")
    response.headers["Content-Disposition"] = "inline; filename=swarm-baseball-calendar.ics"
    return response


@app.route("/<org_slug>/calendar/add", methods=["POST"])
def add_calendar_entry():
    entry_date = parse_date(request.form.get("entry_date"))
    message = request.form.get("message", "").strip()
    location = request.form.get("location", "").strip()
    details = request.form.get("details", "").strip()
    event_time = request.form.get("event_time", "").strip()
    raw_team = request.form.get("team_id", "").strip()
    team_id = int(raw_team) if raw_team.isdigit() else None

    if not message:
        flash("Add a message for that lesson day (e.g. the player's name).", "error")
    else:
        conn = get_db()
        conn.execute(
            "INSERT INTO throwing_entries (organization_id, entry_date, message, team_id, location, details, event_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (g.org["id"], entry_date, message, team_id, location, details, event_time or None),
        )
        conn.commit()
        conn.close()
        flash("Added to the calendar.", "success")

    year, month = entry_date.split("-")[0], entry_date.split("-")[1]
    return redirect(url_for("lesson_calendar", year=int(year), month=int(month), team=team_id))


@app.route("/<org_slug>/calendar/<int:entry_id>/delete", methods=["POST"])
def delete_calendar_entry(entry_id):
    conn = get_db()
    entry = conn.execute(
        "SELECT * FROM throwing_entries WHERE id = ? AND organization_id = ?", (entry_id, g.org["id"])
    ).fetchone()
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

@app.route("/<org_slug>/players/<int:player_id>/comments/add", methods=["POST"])
def add_player_comment(player_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        return redirect(url_for("player_detail", player_id=player_id))

    conn = get_db()
    player = conn.execute(
        "SELECT id FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    conn.execute(
        "INSERT INTO comments (organization_id, player_id, video_id, commenter_name, body) VALUES (?, ?, NULL, ?, ?)",
        (g.org["id"], player_id, commenter_name, body),
    )
    conn.commit()
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + "#feedback")


@app.route("/<org_slug>/videos/<int:video_id>/comments/add", methods=["POST"])
def add_video_comment(video_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    ).fetchone()
    if not video:
        conn.close()
        abort(404)

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        conn.close()
        return redirect(url_for("player_detail", player_id=video["player_id"]) + f"#video-{video_id}")

    conn.execute(
        "INSERT INTO comments (organization_id, player_id, video_id, commenter_name, body) VALUES (?, ?, ?, ?, ?)",
        (g.org["id"], video["player_id"], video_id, commenter_name, body),
    )
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Comment added.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


@app.route("/<org_slug>/comments/<int:comment_id>/delete", methods=["POST"])
@admin_required
def delete_comment(comment_id):
    conn = get_db()
    comment = conn.execute(
        "SELECT * FROM comments WHERE id = ? AND organization_id = ?", (comment_id, g.org["id"])
    ).fetchone()
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


def _calendar_redirect(request_form, entry_id):
    """Bounce back to the exact month/team view the calendar comment form was
    opened from, with ?entry=<id> so the page reopens that event's popup."""
    raw_year = request_form.get("year", "").strip()
    raw_month = request_form.get("month", "").strip()
    raw_team = request_form.get("team_id", "").strip()
    return redirect(url_for(
        "lesson_calendar",
        year=int(raw_year) if raw_year.isdigit() else None,
        month=int(raw_month) if raw_month.isdigit() else None,
        team=int(raw_team) if raw_team.isdigit() else None,
        entry=entry_id,
    ))


@app.route("/<org_slug>/calendar/<int:entry_id>/comments/add", methods=["POST"])
def add_calendar_comment(entry_id):
    commenter_name = request.form.get("commenter_name", "").strip()
    body = request.form.get("body", "").strip()

    conn = get_db()
    entry = conn.execute(
        "SELECT * FROM throwing_entries WHERE id = ? AND organization_id = ?", (entry_id, g.org["id"])
    ).fetchone()
    if not entry:
        conn.close()
        abort(404)

    if not commenter_name or not body:
        flash("Name and comment are both required.", "error")
        conn.close()
        return _calendar_redirect(request.form, entry_id)

    conn.execute(
        "INSERT INTO calendar_entry_comments (organization_id, entry_id, commenter_name, body) VALUES (?, ?, ?, ?)",
        (g.org["id"], entry_id, commenter_name, body),
    )
    conn.commit()
    conn.close()
    flash("Comment added.", "success")
    return _calendar_redirect(request.form, entry_id)


@app.route("/<org_slug>/calendar/comments/<int:comment_id>/delete", methods=["POST"])
@admin_required
def delete_calendar_comment(comment_id):
    conn = get_db()
    comment = conn.execute(
        "SELECT * FROM calendar_entry_comments WHERE id = ? AND organization_id = ?", (comment_id, g.org["id"])
    ).fetchone()
    if not comment:
        conn.close()
        abort(404)

    entry_id = comment["entry_id"]
    conn.execute("DELETE FROM calendar_entry_comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    flash("Comment deleted.", "success")
    return _calendar_redirect(request.form, entry_id)


# ---------- Routes: CSV stat upload ----------

@app.route("/<org_slug>/upload/csv", methods=["GET", "POST"])
def upload_csv():
    conn = get_db()
    players = conn.execute(
        "SELECT id, name FROM players WHERE organization_id = ? ORDER BY name COLLATE NOCASE ASC", (g.org["id"],)
    ).fetchall()

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
                    """INSERT INTO stat_entries (organization_id, player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (g.org["id"], player_id, entry_date, category, col.strip(), value, source_file, import_timestamp),
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
                                """INSERT INTO stat_entries (organization_id, player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (g.org["id"], player_id, entry_date, category, "Strike %", strike_pct, source_file, import_timestamp),
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
                                        """INSERT INTO stat_entries (organization_id, player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (g.org["id"], player_id, entry_date, category, "ERA", era_val, source_file, import_timestamp),
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
                                        """INSERT INTO stat_entries (organization_id, player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (g.org["id"], player_id, entry_date, category, "K/7", k7_val, source_file, import_timestamp),
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

@app.route("/<org_slug>/upload/trackman", methods=["GET", "POST"])
def upload_trackman():
    conn = get_db()
    players = conn.execute(
        "SELECT id, name FROM players WHERE organization_id = ? ORDER BY name COLLATE NOCASE ASC", (g.org["id"],)
    ).fetchall()

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
                """INSERT INTO stat_entries (organization_id, player_id, entry_date, category, stat_name, stat_value, source_file, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (g.org["id"], player_id, entry_date, category, stat_name, value, source_file, import_timestamp),
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
                       (organization_id, player_id, entry_date, category, pitch_no, pitch_type, pitch_call,
                        rel_speed, spin_rate, spin_axis, ivb, hb, rel_height, rel_side,
                        extension, vaa, loc_height, loc_side, exit_speed, launch_angle,
                        source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (g.org["id"], player_id, entry_date, category, p["pitch_no"], p["pitch_type"], p["pitch_call"],
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

@app.route("/<org_slug>/upload/video", methods=["GET", "POST"])
def upload_video():
    conn = get_db()
    players = conn.execute(
        "SELECT id, name FROM players WHERE organization_id = ? ORDER BY name COLLATE NOCASE ASC", (g.org["id"],)
    ).fetchall()

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

        owned = conn.execute(
            "SELECT id FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
        ).fetchone()
        if not owned:
            flash("That player isn't part of this organization.", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        if not files:
            flash("Please choose at least one video file.", "error")
            conn.close()
            return redirect(url_for("upload_video"))

        uploaded_count = 0
        skipped_names = []
        for idx, file in enumerate(files):
            if not allowed_file(file.filename, ALLOWED_VIDEO_EXT) or not MEDIA_ENABLED:
                skipped_names.append(file.filename)
                continue

            safe_name = secure_filename(file.filename)
            stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
            upload_media(file.stream, f"uploads/videos/{stored_filename}", file.content_type)

            conn.execute(
                "INSERT INTO videos (organization_id, player_id, entry_date, title, category, notes, filename) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (g.org["id"], player_id, entry_date, title or safe_name, category, notes, stored_filename),
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
                f"Couldn't upload any of those files - unsupported video type: {', '.join(skipped_names)}.",
                "error",
            )
        return redirect(url_for("player_detail", player_id=player_id))

    conn.close()
    return render_template("upload_video.html", players=players, category_options=CATEGORY_OPTIONS)


# Direct-to-R2 upload: instead of the browser sending the video through this
# server and then this server relaying it on to R2 (two full transfers of
# every byte, and a big video tying up a gunicorn worker for the whole
# thing), the upload form's JS asks this endpoint for a short-lived
# presigned R2 URL per file and PUTs the file straight to R2 itself - this
# server never touches the video bytes at all. upload_video() above stays
# exactly as it was and still works as a plain multipart form post; it's
# the fallback if JS is unavailable or a presigned URL can't be obtained
# (e.g. the R2 bucket's CORS policy isn't set up yet).
@app.route("/<org_slug>/upload/video/presign", methods=["POST"])
def presign_video_upload():
    if not MEDIA_ENABLED:
        return {"ok": False, "error": "Media storage isn't configured."}, 400

    payload = request.get_json(silent=True) or {}
    requested = payload.get("files") or []
    if not isinstance(requested, list) or not requested:
        return {"ok": False, "error": "No files given."}, 400

    client = r2_media_client()
    results = []
    for item in requested[:20]:  # a generous cap - nobody's adding 20+ clips in one go
        original_name = (item or {}).get("filename") or ""
        content_type = (item or {}).get("content_type") or ""
        if not allowed_file(original_name, ALLOWED_VIDEO_EXT):
            results.append({"filename": original_name, "error": "Unsupported video type."})
            continue
        safe_name = secure_filename(original_name)
        stored_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
        key = f"uploads/videos/{stored_filename}"
        try:
            # ContentType is deliberately left out of the signed params here -
            # if it were included, the browser's PUT would have to send back
            # that exact Content-Type header or R2 rejects the signature.
            # Leaving it unsigned lets the browser send whatever Content-Type
            # it detected (or none) without the upload failing over a
            # mismatch neither side has much control over.
            url = client.generate_presigned_url(
                "put_object",
                Params={"Bucket": R2_MEDIA_BUCKET_NAME, "Key": key},
                ExpiresIn=3600,
            )
        except Exception:
            results.append({"filename": original_name, "error": "Couldn't get an upload URL."})
            continue
        results.append({
            "filename": original_name,
            "stored_filename": stored_filename,
            "key": key,
            "url": url,
            "content_type": content_type,
        })

    return {"ok": True, "files": results}


@app.route("/<org_slug>/upload/video/finalize", methods=["POST"])
def finalize_video_upload():
    payload = request.get_json(silent=True) or {}
    player_id = payload.get("player_id")
    title = (payload.get("title") or "").strip()
    category = (payload.get("category") or "").strip()
    if category == "Other":
        category = (payload.get("category_other") or "").strip()
    notes = (payload.get("notes") or "").strip()
    entry_date = parse_date(payload.get("entry_date"))
    files = [f for f in (payload.get("files") or []) if f.get("stored_filename")]

    if not player_id:
        return {"ok": False, "error": "Choose a player."}, 400

    conn = get_db()
    owned = conn.execute(
        "SELECT id FROM players WHERE id = ? AND organization_id = ?", (player_id, g.org["id"])
    ).fetchone()
    if not owned:
        conn.close()
        return {"ok": False, "error": "That player isn't part of this organization."}, 400

    if not files:
        conn.close()
        return {"ok": False, "error": "No uploaded files to save."}, 400

    for f in files:
        safe_name = secure_filename(f.get("filename") or f["stored_filename"])
        conn.execute(
            "INSERT INTO videos (organization_id, player_id, entry_date, title, category, notes, filename) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (g.org["id"], player_id, entry_date, title or safe_name, category, notes, f["stored_filename"]),
        )
    conn.commit()
    conn.close()

    flash(f"Uploaded {len(files)} video{'s' if len(files) != 1 else ''}.", "success")
    return {"ok": True, "redirect": url_for("player_detail", player_id=player_id)}


@app.route("/<org_slug>/videos/<int:video_id>/delete", methods=["POST"])
@admin_required
def delete_video(video_id):
    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    ).fetchone()
    if video:
        delete_media(f"uploads/videos/{video['filename']}")
        if video["thumbnail_filename"]:
            delete_media(f"uploads/videos/thumbs/{video['thumbnail_filename']}")
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


@app.route("/<org_slug>/videos/<int:video_id>/edit-date", methods=["POST"])
def edit_video_date(video_id):
    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    ).fetchone()
    if not video:
        conn.close()
        abort(404)
    # Same permission model as pinning: admins/owners, or the account linked
    # to this exact player, can fix a date that got entered wrong - not any
    # random logged-in user on someone else's video.
    if not session.get("is_admin") and session.get("player_id") != video["player_id"]:
        conn.close()
        abort(403)
    new_date = parse_date(request.form.get("entry_date"))
    conn.execute("UPDATE videos SET entry_date = ? WHERE id = ?", (new_date, video_id))
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Video date updated.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


@app.route("/<org_slug>/videos/<int:video_id>/pin", methods=["POST"])
def toggle_video_pin(video_id):
    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    ).fetchone()
    if not video:
        conn.close()
        abort(404)
    # Only admins/owners or the account linked to this exact player can pin
    # that player's videos - one player's account shouldn't be able to pin
    # videos on someone else's page.
    if not session.get("is_admin") and session.get("player_id") != video["player_id"]:
        conn.close()
        abort(403)
    new_pinned = 0 if video["pinned"] else 1
    conn.execute("UPDATE videos SET pinned = ? WHERE id = ?", (new_pinned, video_id))
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Pinned to the top of the timeline." if new_pinned else "Unpinned.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


@app.route("/<org_slug>/videos/<int:video_id>/toggle-recruiting-visible", methods=["POST"])
def toggle_video_recruiting_visible(video_id):
    conn = get_db()
    video = conn.execute(
        "SELECT * FROM videos WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    ).fetchone()
    if not video:
        conn.close()
        abort(404)
    # Same permission model as pinning: admins/owners or the account linked
    # to this exact player. A player who's opted in to recruiting overall can
    # still keep individual clips (a rough bullpen, a work-in-progress rep)
    # out of what college coaches see without opting out entirely.
    if not session.get("is_admin") and session.get("player_id") != video["player_id"]:
        conn.close()
        abort(403)
    new_visible = 0 if video["recruiting_visible"] else 1
    conn.execute("UPDATE videos SET recruiting_visible = ? WHERE id = ?", (new_visible, video_id))
    conn.commit()
    player_id = video["player_id"]
    conn.close()
    flash("Visible to recruiters again." if new_visible else "Hidden from recruiters.", "success")
    return redirect(url_for("player_detail", player_id=player_id) + f"#video-{video_id}")


# ---------- Routes: manage uploads (delete CSV imports / videos) ----------

@app.route("/<org_slug>/manage")
@admin_required
def manage_uploads():
    conn = get_db()

    import_rows = conn.execute(
        """SELECT source_file, imported_at, category,
                  COUNT(*) AS row_count,
                  COUNT(DISTINCT player_id) AS player_count,
                  MIN(entry_date) AS earliest_date,
                  MAX(entry_date) AS latest_date,
                  MIN(COALESCE(verified, 0)) AS verified
           FROM stat_entries
           WHERE organization_id = ? AND source_file IS NOT NULL AND source_file != ''
           GROUP BY source_file, imported_at, category
           ORDER BY imported_at DESC""",
        (g.org["id"],),
    ).fetchall()

    # Any stat rows with no source_file (shouldn't normally happen, but covers
    # older/edge-case data) get bundled into one "manual entries" bucket per category.
    manual_rows = conn.execute(
        """SELECT category, COUNT(*) AS row_count, COUNT(DISTINCT player_id) AS player_count
           FROM stat_entries
           WHERE organization_id = ? AND (source_file IS NULL OR source_file = '')
           GROUP BY category""",
        (g.org["id"],),
    ).fetchall()

    videos = conn.execute(
        """SELECT v.*, p.name AS player_name
           FROM videos v JOIN players p ON p.id = v.player_id
           WHERE v.organization_id = ?
           ORDER BY v.entry_date DESC, v.id DESC""",
        (g.org["id"],),
    ).fetchall()

    conn.close()
    return render_template(
        "manage.html", import_rows=import_rows, manual_rows=manual_rows, videos=videos
    )


@app.route("/<org_slug>/imports/delete", methods=["POST"])
@admin_required
def delete_import():
    source_file = request.form.get("source_file", "")
    imported_at = request.form.get("imported_at", "")
    category = request.form.get("category", "")

    conn = get_db()
    cur = conn.execute(
        "DELETE FROM stat_entries WHERE source_file = ? AND imported_at = ? AND category = ? AND organization_id = ?",
        (source_file, imported_at, category, g.org["id"]),
    )
    # TrackMan imports also wrote per-pitch detail rows; remove those too.
    conn.execute(
        "DELETE FROM trackman_pitches WHERE source_file = ? AND imported_at = ? AND category = ? AND organization_id = ?",
        (source_file, imported_at, category, g.org["id"]),
    )
    conn.commit()
    removed = cur.rowcount
    conn.close()

    flash(f"Removed {removed} stat row(s) from {source_file}.", "success")
    return redirect(url_for("manage_uploads"))


@app.route("/<org_slug>/imports/verify", methods=["POST"])
@admin_required
def verify_import():
    source_file = request.form.get("source_file", "")
    imported_at = request.form.get("imported_at", "")
    category = request.form.get("category", "")

    conn = get_db()
    cur = conn.execute(
        "UPDATE stat_entries SET verified = 1 WHERE source_file = ? AND imported_at = ? AND category = ? AND organization_id = ?",
        (source_file, imported_at, category, g.org["id"]),
    )
    conn.commit()
    verified_count = cur.rowcount
    conn.close()

    flash(f"Verified {verified_count} stat row(s) from {source_file}.", "success")
    return redirect(url_for("manage_uploads"))


@app.route("/<org_slug>/videos/<int:video_id>/verify", methods=["POST"])
@admin_required
def verify_video(video_id):
    conn = get_db()
    conn.execute(
        "UPDATE videos SET verified = 1 WHERE id = ? AND organization_id = ?", (video_id, g.org["id"])
    )
    conn.commit()
    conn.close()
    flash("Video verified.", "success")
    return redirect(url_for("manage_uploads"))


# ---------- Error pages + lightweight crash alerting ----------
#
# There's no third-party error-monitoring service (Sentry, etc.) wired up -
# that would need its own account/DSN - so this is a low-tech stand-in:
# log the traceback and, if email is configured, send it to SUPPORT_EMAIL.
# Throttled per error type so a bug that fires on every request sends one
# email instead of flooding the inbox.

_last_crash_alert_at = {}
_CRASH_ALERT_COOLDOWN_SECONDS = 600  # at most one email per error type / 10 min


def _maybe_email_crash_alert(subject, body):
    if not email_configured():
        return
    now = datetime.now()
    last = _last_crash_alert_at.get(subject)
    if last and (now - last).total_seconds() < _CRASH_ALERT_COOLDOWN_SECONDS:
        return
    _last_crash_alert_at[subject] = now
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = SUPPORT_EMAIL
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception:
        pass  # the alert itself failing shouldn't ever crash the request


@app.errorhandler(404)
def handle_not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def handle_server_error(e):
    return render_template("500.html"), 500


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Catches anything that isn't an HTTPException (a raw KeyError,
    sqlite3.Error, etc.) - real bugs, not routine 404s/403s. Those still
    fall through to Flask's normal handling (or the 404/500 handlers
    above), so a Flask-Limiter 429 or an abort(403) isn't mistaken for a
    crash."""
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled exception")
    _maybe_email_crash_alert(
        f"{PLATFORM_NAME} error: {type(e).__name__}",
        f"{request.method} {request.path}\n\n{traceback.format_exc()}",
    )
    return render_template("500.html"), 500


if __name__ == "__main__":
    init_db()
    print(f"\n{PLATFORM_NAME}")
    print("Open http://127.0.0.1:5000 in your browser. Press Ctrl+C to stop.")
    print("First visit? You'll be asked to create your admin account.\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
else:
    init_db()
