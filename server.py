from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from functools import wraps
import psycopg2  # PostgreSQL driver
from psycopg2 import sql

# --- NOTE: moved registration of admin_mark_completed to EOF to ensure app exists ---
import stripe
from werkzeug.security import generate_password_hash, check_password_hash
from backend.data.jsonData import getJsonData
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os
import io
import time
import smtplib
import re
import uuid
import cloudinary
import cloudinary.uploader
try:
    from PIL import Image, ImageOps  # type: ignore
except Exception:
    Image = None
    ImageOps = None

load_dotenv()


app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")

UPLOAD_ROOT = os.getenv("UPLOAD_ROOT") or os.path.join(app.root_path, "uploads")
app.config["UPLOAD_ROOT"] = UPLOAD_ROOT
PRODUCT_UPLOAD_SUBDIR = os.getenv("PRODUCT_UPLOAD_SUBDIR", "products")
app.config["PRODUCT_UPLOAD_SUBDIR"] = PRODUCT_UPLOAD_SUBDIR
os.makedirs(os.path.join(app.config["UPLOAD_ROOT"], PRODUCT_UPLOAD_SUBDIR), exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "5"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "products")
_cloudinary_configured = False
cloudinary_url = os.getenv("CLOUDINARY_URL")
if cloudinary_url:
    cloudinary.config(cloudinary_url=cloudinary_url, secure=True)
    _cloudinary_configured = True
else:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if cloud_name and api_key and api_secret:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        _cloudinary_configured = True

def _should_expose_reset_link() -> bool:
    """Return True when we can surface the reset link in logs/UI (dev mode)."""
    explicit = os.getenv("RESET_SHOW_DEV_LINK")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return app.debug


EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def is_valid_email(address: str) -> bool:
    """Return True when the supplied email address looks valid."""
    if not address:
        return False
    return EMAIL_PATTERN.match(address.strip()) is not None


def build_media_url(path: str | None) -> str:
    """Return a public URL for either static or uploaded product assets."""
    if not path:
        return url_for("static", filename="images/product-1.jpeg")
    path_str = str(path).strip()
    if not path_str:
        return url_for("static", filename="images/product-1.jpeg")
    if path_str.startswith("http://") or path_str.startswith("https://"):
        return path_str
    cleaned = path_str.lstrip("/")
    static_candidate = os.path.join(app.static_folder, cleaned)
    if os.path.isfile(static_candidate):
        return url_for("static", filename=cleaned)
    upload_candidate = os.path.join(app.config["UPLOAD_ROOT"], cleaned)
    if os.path.isfile(upload_candidate):
        return url_for("serve_upload", filename=cleaned)
    return url_for("static", filename=cleaned)


def allowed_image_file(filename: str) -> bool:
    if not filename:
        return False
    return os.path.splitext(filename)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def ensure_cloudinary_ready():
    if not _cloudinary_configured:
        raise RuntimeError("Cloudinary is not configured. Please set CLOUDINARY_URL or CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.")


def upload_product_image(file_storage):
    ensure_cloudinary_ready()
    filename = secure_filename(file_storage.filename or "")
    if not allowed_image_file(filename):
        raise ValueError("Unsupported image format. Allowed: jpg, jpeg, png, webp.")
    unique_id = uuid.uuid4().hex
    options = {
        "resource_type": "image",
        "overwrite": True,
    }
    folder = (CLOUDINARY_FOLDER or "").strip().strip("/")
    if folder:
        options["folder"] = folder
        options["public_id"] = unique_id
    else:
        options["public_id"] = unique_id
    file_storage.stream.seek(0)
    result = cloudinary.uploader.upload(file_storage, **options)
    secure_url = result.get("secure_url")
    public_id = result.get("public_id")
    if not secure_url or not public_id:
        raise RuntimeError("Cloudinary upload failed to return secure URL.")
    return secure_url, public_id


def delete_product_image(public_id: str | None):
    if not public_id:
        return
    try:
        ensure_cloudinary_ready()
    except RuntimeError:
        return
    try:
        cloudinary.uploader.destroy(public_id, invalidate=True)
    except Exception as exc:
        app.logger.warning("Failed to delete Cloudinary asset %s: %s", public_id, exc)


def extract_public_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        if "/upload/" not in url:
            return None
        remainder = url.split("/upload/", 1)[1]
        remainder = remainder.split("?", 1)[0].split("#", 1)[0]
        remainder = remainder.rsplit(".", 1)[0]
        return remainder
    except Exception:
        return None


def resolve_product_columns(conn):
    cols_raw = table_columns(conn, "Products")
    cols_lookup = {c.lower(): c for c in cols_raw}

    def pick(candidates):
        for cand in candidates:
            key = cand.lower()
            if key in cols_lookup:
                return cols_lookup[key]
        return None

    return {
        "id": table_id_column(conn, "Products", ("id", "product_id", "ProductID", "productid")),
        "name": pick(("name", "product_name", "title")),
        "price": pick(("price", "amount", "cost")),
        "image": pick(("image", "image_url", "photo", "picture", "imagepath")),
        "image_public_id": pick(("image_public_id", "cloudinary_id", "public_id", "imagepublicid")),
        "quantity": pick(("quantity", "qty", "stock", "in_stock")),
    }


def fetch_product_by_id(product_id: int):
    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        select_parts = [f"{columns['id']} AS id"]
        aliases = ["id"]
        for alias, column in (
            ("name", columns["name"]),
            ("price", columns["price"]),
            ("image", columns["image"]),
            ("image_public_id", columns["image_public_id"]),
            ("quantity", columns["quantity"]),
        ):
            if column:
                select_parts.append(f"{column} AS {alias}")
                aliases.append(alias)
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(select_parts)} FROM Products WHERE {columns['id']} = %s", (product_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        data = {}
        for idx, alias in enumerate(aliases):
            data[alias] = row[idx]
        if data.get("id") is not None:
            try:
                data["id"] = int(data["id"])
            except Exception:
                pass
        if data.get("price") is not None:
            try:
                data["price"] = float(data["price"])
            except Exception:
                pass
        if data.get("quantity") is not None:
            try:
                data["quantity"] = int(data["quantity"])
            except Exception:
                pass
        return data
    finally:
        conn.close()


def get_cart_summary():
    cart = session.get("cart", [])
    items_count = 0
    total = 0.0
    for item in cart:
        try:
            qty = int(item.get("quantity", 1))
        except Exception:
            qty = 1
        items_count += qty
        try:
            total += float(item.get("price", 0)) * qty
        except Exception:
            continue
    return cart, items_count, round(total, 2)


def resolve_table_columns_generic(conn, table_candidates):
    for name in table_candidates:
        try:
            cols = table_columns(conn, name)
            if cols:
                lookup = {c.lower(): c for c in cols}
                return lookup, name
        except Exception:
            continue
    return None, None


def delete_order_items_for_product(conn, product_id):
    lookup, table_name = resolve_table_columns_generic(conn, ['OrderItems', 'order_items', 'orderitems'])
    if not lookup:
        return
    prod_col = None
    for cand in ('product_id', 'productid', 'product'):
        if cand in lookup:
            prod_col = lookup[cand]
            break
    if not prod_col:
        for key, actual in lookup.items():
            if 'product' in key and key.endswith('id'):
                prod_col = actual
                break
    if not prod_col:
        return
    cur = conn.cursor()
    try:
        stmt = sql.SQL("DELETE FROM {table} WHERE {col} = %s").format(
            table=sql.Identifier(table_name),
            col=sql.Identifier(prod_col)
        )
        cur.execute(stmt, (product_id,))
    finally:
        cur.close()


def remove_product_record(product_id: int):
    product = fetch_product_by_id(product_id)
    if not product:
        return False, "Product not found.", None
    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        delete_order_items_for_product(conn, product_id)
        cur = conn.cursor()
        cur.execute(f"DELETE FROM Products WHERE {columns['id']} = %s", (product_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return False, "Product not found.", None
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Failed to delete product %s: %s", product_id, exc)
        return False, "Unable to delete product.", None
    finally:
        conn.close()
    public_id = product.get("image_public_id") or extract_public_id_from_url(product.get("image"))
    delete_product_image(public_id)
    return True, "Product deleted.", product
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")


def _env_flag(name: str, default: str = "false") -> bool:
    '''Return True when an env var looks like a truthy value such as 1/true/yes.'''
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def send_email(recipient: str, subject: str, body: str, purpose: str = "email") -> bool:
    """Send an email via SMTP using env configuration."""
    if not recipient:
        return False

    smtp_host = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT")
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM_EMAIL") or smtp_user

    if not (smtp_host and smtp_port and sender):
        app.logger.warning("Skipping %s: SMTP settings are incomplete.", purpose)
        return False

    try:
        port = int(smtp_port)
    except ValueError:
        app.logger.error("Skipping %s: invalid SMTP_PORT '%s'.", purpose, smtp_port)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    use_ssl = _env_flag("SMTP_USE_SSL", "false")
    use_tls = _env_flag("SMTP_USE_TLS", "true")

    try:
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(smtp_host, port, timeout=20) as smtp:
            if not use_ssl and use_tls:
                smtp.starttls()
            if smtp_user and smtp_pass:
                smtp.login(smtp_user, smtp_pass)
            smtp.send_message(message)
        return True
    except Exception as exc:
        app.logger.error("Failed to send %s: %s", purpose, exc)
        return False


def send_signup_email(recipient: str, first_name: str = "") -> bool:
    """Send a welcome email to the new user after signup."""
    subject = os.getenv("SIGNUP_EMAIL_SUBJECT", "Welcome to CleanX!")
    greeting_name = first_name or recipient.split("@")[0]
    default_body = (
        f"Hi {greeting_name},\n\n"
        "Thanks for creating a CleanX account. You can now log in anytime to book "
        "and manage your cleaning services.\n\n"
        "If you did not create this account, please contact our support team immediately.\n\n"
        "-- The CleanX Team"
    )
    body = os.getenv("SIGNUP_EMAIL_BODY", default_body)
    return send_email(recipient, subject, body, purpose="signup email")


def send_password_reset_email(recipient: str, reset_url: str) -> bool:
    """Send the password reset link to a user."""
    subject = os.getenv("RESET_EMAIL_SUBJECT", "Reset your CleanX password")
    default_body = (
        "Hi,\n\n"
        "We received a request to reset your CleanX password. To choose a new password, "
        f"please open the link below:\n{reset_url}\n\n"
        "This link will expire soon. If you did not request a reset, you can ignore this email.\n\n"
        "-- The CleanX Team"
    )
    body = os.getenv("RESET_EMAIL_BODY", default_body)
    return send_email(recipient, subject, body, purpose="password reset email")


def send_order_confirmation_email(
    recipient: str,
    order_id: int | None,
    first_name: str | None,
    items: list[dict[str, object]] | None,
    total_amount: float,
    shipping_address: str | None = None,
) -> bool:
    """Send an order confirmation email summarizing purchased items."""
    if not recipient:
        return False
    subject = os.getenv("ORDER_CONFIRMATION_SUBJECT", "Your CleanX order confirmation")
    name = (first_name or "").strip() or recipient.split("@")[0]
    lines = [
        f"Hi {name},",
        "",
        "Thank you for your order! We're getting everything ready.",
    ]
    if order_id:
        lines.append(f"Order number: #{order_id}")
    lines.append("")
    lines.append("Order summary:")
    items = items or []
    if items:
        for item in items:
            title = str(item.get("name") or item.get("title") or item.get("id") or "Item")
            try:
                qty = int(item.get("quantity", 1))
            except Exception:
                qty = 1
            try:
                price = float(item.get("price", 0.0))
            except Exception:
                price = 0.0
            lines.append(f"• {title} x{qty} — R{price:.2f}")
    else:
        lines.append("• Items recorded in your order")
    lines.append("")
    lines.append(f"Total: R{total_amount:.2f}")
    if shipping_address:
        lines.extend(["", "Shipping address:", shipping_address])
    lines.extend(
        [
            "",
            "You'll receive another update when your order ships.",
            "",
            "-- The CleanX Team",
        ]
    )
    body = "\n".join(lines)
    custom_body = os.getenv("ORDER_CONFIRMATION_BODY")
    if custom_body:
        body = custom_body.format(
            name=name,
            order_id=order_id or "",
            total=f"R{total_amount:.2f}",
            address=shipping_address or "",
        )
    return send_email(recipient, subject, body, purpose="order confirmation email")


@app.context_processor
def inject_media_helpers():
    return {
        "product_image_url": build_media_url,
    }


@app.route("/uploads/<path:filename>")
def serve_upload(filename: str):
    safe_path = filename.strip("/")
    return send_from_directory(app.config["UPLOAD_ROOT"], safe_path)


def get_reset_serializer() -> URLSafeTimedSerializer:
    """Return a serializer for password reset tokens."""
    if not app.secret_key:
        raise RuntimeError("SECRET_KEY must be set for password reset tokens.")
    salt = os.getenv("RESET_TOKEN_SALT", "password-reset")
    return URLSafeTimedSerializer(app.secret_key, salt=salt)


def get_reset_token_ttl() -> int:
    """Return max age in seconds for reset tokens."""
    try:
        return int(os.getenv("RESET_TOKEN_TTL", "3600"))
    except ValueError:
        return 3600

def get_db_connection():
    """Create a new PostgreSQL connection using DATABASE_URL."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg2.connect(DATABASE_URL)


@app.context_processor
def inject_nav_state():
    """Inject common navbar state across all templates without touching CSS.
    Provides itemsInCart count and Login/Signup labels based on auth state.
    """
    cart = session.get("cart", [])
    def _to_int(v, default=1):
        try:
            return int(v)
        except Exception:
            return default
    items_count = sum(_to_int(item.get("quantity", 1), 1) for item in cart)

    if session.get("email"):
        # Fetch names from DB if available
        first_name, last_name = get_user_names_by_email(session.get("email"))
        return {
            "itemsInCart": items_count,
            "Login": None,
            "Signup": None,
            "user_email": session.get("email"),
            "user_first_name": first_name,
            "user_last_name": last_name,
        }
    else:
        return {
            "itemsInCart": items_count,
            "Login": "Login",
            "Signup": "Signup",
            "user_email": None,
        }


def table_columns(conn, table_name):
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            ("public", table_name.lower()),
        )
        rows = cur.fetchall()
        return {str(r[0]).lower() for r in rows}
    except Exception:
        return set()


# Utility to resolve an ID column for a table (prefers common names)
def table_id_column(conn, table_name, candidates=("id", "order_id", "OrderID", "orderid")):
    cols = {c.lower() for c in table_columns(conn, table_name)}
    # Preferred exact matches
    for cand in candidates:
        if cand.lower() in cols:
            return cand
    # Fallback: any column ending with 'id'
    for c in cols:
        if c.endswith("id"):
            return c
    # Last resort: return 'id' and let caller handle errors
    return "id"

# --------------------------- Admin Helpers ---------------------------
def get_admin_emails():
    raw = os.getenv("ADMIN_EMAILS", "")
    emails = [e.strip().lower() for e in raw.split(",") if e.strip()]
    return set(emails)


def is_admin_user(email: str) -> bool:
    if not email:
        return False
    # Prefer database flag if present
    try:
        conn = get_db_connection()
        cols = table_columns(conn, 'Users')
        if 'is_admin' in cols:
            cur = conn.cursor()
            cur.execute("SELECT is_admin FROM Users WHERE email = %s LIMIT 1", (email,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row is not None:
                try:
                    return bool(row[0])
                except Exception:
                    return False
        else:
            conn.close()
    except Exception:
        pass
    # Fallback to environment list
    return email.lower() in get_admin_emails()


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("email"):
            flash("Please log in to access admin.", "error")
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return wrapper


# --------------------------- User Helpers ---------------------------
def get_user_names_by_email(email: str):
    """Return (first_name, last_name) for a given user email if columns exist, else (None, None).

    Supports common column variants like first_name/firstname/name and last_name/lastname/surname.
    """
    if not email:
        return None, None
    try:
        conn = get_db_connection()
        cols = table_columns(conn, 'Users')

        first_candidates = (
            'first_name', 'firstname', 'first', 'name', 'given_name', 'givenname'
        )
        last_candidates = (
            'last_name', 'lastname', 'last', 'surname', 'family_name', 'familyname'
        )

        first_col = next((c for c in first_candidates if c in cols), None)
        last_col = next((c for c in last_candidates if c in cols), None)

        if not first_col and not last_col:
            conn.close()
            return None, None

        select_cols = ", ".join([c for c in (first_col, last_col) if c])
        cur = conn.cursor()
        cur.execute(f"SELECT {select_cols} FROM Users WHERE email = %s LIMIT 1", (email,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None, None

        # Map returned row to (first, last) in the same order as select_cols
        values = list(row)
        first_val = values[0] if first_col else None
        last_val = values[-1] if last_col else None
        if not first_col:
            first_val = None
        if not last_col:
            last_val = None
        return first_val, last_val
    except Exception:
        return None, None


@app.route("/signup", methods=["GET", "POST"])
def signup():
    cart = session.get("cart", [])
    itemsInCart = sum(int(item.get("quantity", 1)) for item in cart)

    if request.method == "POST":
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        # Basic validation
        if not first_name or not last_name:
            return render_template("signup.html", error="Please enter your first and last name.", itemsInCart=itemsInCart, form_email=email, form_first_name=first_name, form_last_name=last_name)
        if not is_valid_email(email):
            return render_template("signup.html", error="Please enter a valid email.", itemsInCart=itemsInCart, form_email=email, form_first_name=first_name, form_last_name=last_name)
        if len(password) < 6:
            return render_template("signup.html", error="Password must be at least 6 characters.", itemsInCart=itemsInCart, form_email=email, form_first_name=first_name, form_last_name=last_name)
        if password != confirm:
            return render_template("signup.html", error="Passwords do not match.", itemsInCart=itemsInCart, form_email=email, form_first_name=first_name, form_last_name=last_name)

        hashed_pw = generate_password_hash(password)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM Users WHERE email = %s", (email,))
        existing_user = cursor.fetchone()

        if existing_user:
            conn.close()
            return render_template("signup.html", error="Email already registered. Please log in.", itemsInCart=itemsInCart, form_email=email, form_first_name=first_name, form_last_name=last_name)

        # Insert with optional first/last name if columns exist (supports common variants)
        cols = table_columns(conn, 'Users')
        first_candidates = (
            'first_name', 'firstname', 'first', 'name', 'given_name', 'givenname'
        )
        last_candidates = (
            'last_name', 'lastname', 'last', 'surname', 'family_name', 'familyname'
        )

        first_col = next((c for c in first_candidates if c in cols), None)
        last_col = next((c for c in last_candidates if c in cols), None)

        insert_cols = ['email', 'password']
        insert_vals = [email, hashed_pw]
        if first_col:
            insert_cols.append(first_col)
            insert_vals.append(first_name)
        if last_col:
            insert_cols.append(last_col)
            insert_vals.append(last_name)

        placeholders = ", ".join(["%s" for _ in insert_cols])
        sql = f"INSERT INTO Users ({', '.join(insert_cols)}) VALUES ({placeholders})"
        cursor.execute(sql, tuple(insert_vals))
        conn.commit()
        conn.close()

        send_signup_email(email, first_name)

        flash("Account created successfully! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html", itemsInCart=itemsInCart)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        cursor = conn.cursor()
        # Try fetch with is_admin; fallback if column missing
        user = None
        is_admin_val = 0
        try:
            cursor.execute("SELECT email, password, id, is_admin FROM Users WHERE email = %s", (email,))
            row = cursor.fetchone()
            if row:
                user = row
                try:
                    is_admin_val = int(row[3]) if row[3] is not None else 0
                except Exception:
                    is_admin_val = 0
        except Exception:
            cursor.execute("SELECT email, password, id FROM Users WHERE email = %s", (email,))
            user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[1], password):
            session["user_id"] = user[2]
            session["email"] = user[0]
            session["is_admin"] = bool(is_admin_val)
            flash("Logged in successfully!", "success")
            if session.get("is_admin"):
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("home"))
        else:
            cart = session.get("cart", [])
            itemsInCart = sum(int(item.get("quantity", 1)) for item in cart)
            return render_template("login.html", error="Invalid email or password", itemsInCart=itemsInCart)

    cart = session.get("cart", [])
    itemsInCart = sum(int(item.get("quantity", 1)) for item in cart)
    return render_template("login.html", itemsInCart=itemsInCart)


@app.route("/forgotPassword", methods=["GET", "POST"])
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    cart = session.get("cart", [])
    itemsInCart = sum(int(item.get("quantity", 1)) for item in cart)

    if request.method == "POST":
        payload = request.get_json(silent=True) if request.is_json else None
        email = (payload or {}).get("email") or request.form.get("email") or ""
        email = email.strip()
        if not email:
            error_msg = "Please enter your email address."
            if request.is_json:
                return jsonify({"ok": False, "error": error_msg}), 400
            flash(error_msg, "error")
            return render_template("forgot_password.html", itemsInCart=itemsInCart, form_email=email, error=error_msg)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT email FROM Users WHERE email = %s", (email,))
        row = cur.fetchone()
        conn.close()

        dev_link = None
        if row:
            serializer = get_reset_serializer()
            token = serializer.dumps(email)
            reset_url = url_for("reset_password", token=token, _external=True)
            if not send_password_reset_email(email, reset_url):
                app.logger.warning("Password reset email could not be sent to %s.", email)
                if _should_expose_reset_link():
                    dev_link = reset_url

        success_msg = "If an account exists for that email, you'll receive a password reset link shortly."
        if request.is_json:
            resp = {"ok": True, "message": success_msg}
            if dev_link:
                resp["dev_reset_url"] = dev_link
            return jsonify(resp)

        flash(success_msg, "info")
        if dev_link:
            flash(f"Developer reset link: {dev_link}", "info")
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html", itemsInCart=itemsInCart)


@app.route("/reset_password/<token>", methods=["GET", "POST"])
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    cart = session.get("cart", [])
    itemsInCart = sum(int(item.get("quantity", 1)) for item in cart)

    serializer = get_reset_serializer()
    try:
        email = serializer.loads(token, max_age=get_reset_token_ttl())
    except SignatureExpired:
        error_msg = "This password reset link has expired. Please request a new one."
        if request.is_json:
            return jsonify({"ok": False, "error": error_msg}), 400
        flash(error_msg, "error")
        return redirect(url_for("forgot_password"))
    except BadSignature:
        error_msg = "Invalid password reset link. Please request a new one."
        if request.is_json:
            return jsonify({"ok": False, "error": error_msg}), 400
        flash(error_msg, "error")
        return redirect(url_for("forgot_password"))

    def render_reset(error_msg=None):
        return render_template(
            "reset_password.html",
            token=token,
            itemsInCart=itemsInCart,
            email=email,
            error=error_msg,
        )

    if request.method == "POST":
        payload = request.get_json(silent=True) if request.is_json else None
        password = (payload or {}).get("password") or request.form.get("password") or ""
        confirm = (payload or {}).get("confirm_password") or request.form.get("confirm_password") or ""

        if len(password) < 6:
            error = "New password must be at least 6 characters."
            if request.is_json:
                return jsonify({"ok": False, "error": error}), 400
            return render_reset(error)
        if password != confirm:
            error = "New passwords do not match."
            if request.is_json:
                return jsonify({"ok": False, "error": error}), 400
            return render_reset(error)

        hashed = generate_password_hash(password)
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE Users SET password = %s WHERE email = %s", (hashed, email))
            conn.commit()
        finally:
            conn.close()

        success_msg = "Password updated successfully. Please log in."
        if request.is_json:
            return jsonify({"ok": True, "message": success_msg, "redirect_to": url_for("login")})
        flash(success_msg, "success")
        return redirect(url_for("login"))

    return render_reset()

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully!", "success")
    return redirect(url_for("home"))


@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if "email" not in session:
        flash("Please log in to change your password.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm_pw = request.form.get("confirm_new_password") or ""

        if len(new_pw) < 6:
            flash("New password must be at least 6 characters.", "error")
            return redirect(url_for("change_password"))
        if new_pw != confirm_pw:
            flash("New passwords do not match.", "error")
            return redirect(url_for("change_password"))

        conn = get_db_connection()
        cur = conn.cursor()
        # Fetch current password hash
        cur.execute("SELECT password FROM Users WHERE email = %s", (session.get("email"),))
        row = cur.fetchone()
        if not row:
            conn.close()
            flash("User not found.", "error")
            return redirect(url_for("change_password"))

        stored_hash = row[0]
        if not check_password_hash(stored_hash, current_pw):
            conn.close()
            flash("Current password is incorrect.", "error")
            return redirect(url_for("change_password"))

        new_hash = generate_password_hash(new_pw)
        cur.execute("UPDATE Users SET password = %s WHERE email = %s", (new_hash, session.get("email")))
        conn.commit()
        conn.close()
        flash("Password updated successfully.", "success")
        return redirect(url_for("admin_dashboard") if session.get("email") else url_for("home"))

    return render_template("change_password.html")


@app.route("/")
def home():
    #If no user logged in, go to signup
    # if "user_id" not in session:
    #     return redirect(url_for("signup"))
    if "email" in session:
        return render_template("test.html")
    return render_template("test.html", Login="Login", Signup="Signup")


@app.route("/booking", methods=["GET", "POST"])
def booking():
    if request.method == "POST":
        name = request.form["name"]
        address = request.form["address"]
        service_type = request.form["service_type"]
        date = request.form["date"]

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO Bookings (name, address, service_type, date) VALUES (%s, %s, %s, %s)",
            (name, address, service_type, date)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("success"))
    return render_template("booking.html")

@app.route("/products")
def products():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Discover available columns (supports quantity and description gracefully)
    pcols = {c.lower() for c in table_columns(conn, 'Products')}
    id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
    select_cols = [f"{id_col} AS id"]
    has_name = 'name' in pcols
    has_price = 'price' in pcols
    has_image = 'image' in pcols
    qty_col = next((c for c in ('quantity','qty','stock','in_stock') if c in pcols), None)
    if has_name: select_cols.append('name')
    if has_price: select_cols.append('price')
    if has_image: select_cols.append('image')
    if qty_col: select_cols.append(qty_col)

    # Optional search by name (contains)
    q = (request.args.get('q') or '').strip()
    try:
        if q and has_name:
            cursor.execute(f"SELECT {', '.join(select_cols)} FROM Products WHERE LOWER(name) LIKE LOWER(%s)", (f"%{q}%",))
        else:
            cursor.execute(f"SELECT {', '.join(select_cols)} FROM Products")
        rows = cursor.fetchall()
    except psycopg2.errors.UndefinedTable:
        # Database is missing the Products table; treat as no inventory
        conn.rollback()
        rows = []
    except Exception:
        # A failed statement puts the transaction in an error state; clear it before retrying
        conn.rollback()
        try:
            cursor.execute(f"SELECT {', '.join(select_cols)} FROM Products")
            rows = cursor.fetchall()
        except Exception:
            rows = []
    conn.close()

    items = []
    for r in rows:
        idx = 0
        rid = r[idx]; idx += 1
        try:
            rid = int(rid)
        except Exception:
            pass
        obj = {"id": str(rid)}
        if has_name:
            obj["name"] = r[idx]; idx += 1
        if has_price:
            try:
                obj["price"] = float(r[idx]) if r[idx] is not None else 0.0
            except Exception:
                obj["price"] = 0.0
            idx += 1
        if has_image:
            obj["image"] = r[idx]; idx += 1
        if qty_col:
            try:
                obj["quantity"] = int(r[idx]) if r[idx] is not None else None
            except Exception:
                obj["quantity"] = None
            idx += 1
        items.append(obj)
    cart = session.get("cart", [])

    itemsInCart = 0
    for item in cart:
        itemsInCart = itemsInCart + (1 * item.get("quantity"))
    session["itemsInCart"] = itemsInCart
        
    cartTotal = 0
    for i in cart:
        cartTotal = cartTotal + (i.get("price") * i.get("quantity"))
    session["cartTotal"] = cartTotal
    
    cartTotal = round(cartTotal, 2)
    if "email" in session:
        return render_template("products.html", products=items, cart=cart, itemsInCart=itemsInCart, cartTotal=cartTotal)
        
    return render_template("products.html", products=items, cart=cart, itemsInCart=itemsInCart, cartTotal=cartTotal, Login="Login", Signup="Signup")


@app.route("/admin/add-product", methods=["GET", "POST"])
@admin_required
def admin_add_product():
    cart, itemsInCart, cartTotal = get_cart_summary()
    if request.method == "GET":
        return render_template(
            "add_product.html",
            cart=cart,
            itemsInCart=itemsInCart,
            cartTotal=cartTotal,
            max_upload_mb=MAX_UPLOAD_MB,
        )

    name = (request.form.get("name") or "").strip()
    price_raw = (request.form.get("price") or "").strip()
    file = request.files.get("image")
    quantity_raw = request.form.get("quantity")
    quantity_value = None
    quantity_field_present = quantity_raw is not None
    if quantity_field_present:
        quantity_raw = quantity_raw.strip()
        if quantity_raw:
            try:
                quantity_value = int(quantity_raw)
            except Exception:
                flash("Please provide a valid stock level.", "error")
                return redirect(url_for("admin_edit_product", product_id=product_id))
            if quantity_value < 0:
                flash("Stock level cannot be negative.", "error")
                return redirect(url_for("admin_edit_product", product_id=product_id))
        else:
            quantity_value = None

    if not name:
        flash("Product name is required.", "error")
        return redirect(url_for("admin_add_product"))
    try:
        price = float(price_raw)
    except Exception:
        flash("Please provide a valid price.", "error")
        return redirect(url_for("admin_add_product"))
    if price < 0:
        flash("Price cannot be negative.", "error")
        return redirect(url_for("admin_add_product"))
    if not file or not getattr(file, "filename", ""):
        flash("Please upload an image.", "error")
        return redirect(url_for("admin_add_product"))
    if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
        flash(f"Image must be smaller than {MAX_UPLOAD_MB}MB.", "error")
        return redirect(url_for("admin_add_product"))

    try:
        image_url, public_id = upload_product_image(file)
    except Exception as exc:
        app.logger.error("Image upload failed: %s", exc)
        flash("Failed to upload product image. Please try again.", "error")
        return redirect(url_for("admin_add_product"))

    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        if not columns["name"] or not columns["price"] or not columns["image"]:
            raise RuntimeError("Products table is missing required columns.")
        insert_cols = [columns["name"], columns["price"], columns["image"]]
        values = [name, price, image_url]
        if columns["image_public_id"]:
            insert_cols.append(columns["image_public_id"])
            values.append(public_id)
        placeholders = ", ".join(["%s"] * len(insert_cols))
        sql = f"INSERT INTO Products ({', '.join(insert_cols)}) VALUES ({placeholders})"
        cur = conn.cursor()
        cur.execute(sql, tuple(values))
        conn.commit()
        cur.close()
        flash("Product created successfully.", "success")
        return redirect(url_for("products"))
    except Exception as exc:
        conn.rollback()
        delete_product_image(public_id)
        app.logger.exception("Failed to save product: %s", exc)
        flash("Unable to save product. Please try again.", "error")
        return redirect(url_for("admin_add_product"))
    finally:
        conn.close()


@app.route("/admin/edit-product/<int:product_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_product(product_id):
    cart, itemsInCart, cartTotal = get_cart_summary()
    product = fetch_product_by_id(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("products"))

    if request.method == "GET":
        return render_template(
            "edit_product.html",
            product=product,
            cart=cart,
            itemsInCart=itemsInCart,
            cartTotal=cartTotal,
            max_upload_mb=MAX_UPLOAD_MB,
        )

    name = (request.form.get("name") or "").strip()
    price_raw = (request.form.get("price") or "").strip()
    file = request.files.get("image")
    quantity_raw = request.form.get("quantity")
    quantity_value = None
    quantity_field_present = quantity_raw is not None
    if quantity_field_present:
        quantity_raw = quantity_raw.strip()
        if quantity_raw:
            try:
                quantity_value = int(quantity_raw)
            except Exception:
                flash("Please provide a valid stock level.", "error")
                return redirect(url_for("admin_edit_product", product_id=product_id))
            if quantity_value < 0:
                flash("Stock level cannot be negative.", "error")
                return redirect(url_for("admin_edit_product", product_id=product_id))
        else:
            quantity_value = None

    if not name:
        flash("Product name is required.", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    try:
        price = float(price_raw)
    except Exception:
        flash("Please provide a valid price.", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    if price < 0:
        flash("Price cannot be negative.", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))

    new_image_url = None
    new_public_id = None
    if file and getattr(file, "filename", ""):
        if request.content_length and request.content_length > app.config["MAX_CONTENT_LENGTH"]:
            flash(f"Image must be smaller than {MAX_UPLOAD_MB}MB.", "error")
            return redirect(url_for("admin_edit_product", product_id=product_id))
        try:
            new_image_url, new_public_id = upload_product_image(file)
        except Exception as exc:
            app.logger.error("Image upload failed: %s", exc)
            flash("Unable to upload new image.", "error")
            return redirect(url_for("admin_edit_product", product_id=product_id))

    conn = get_db_connection()
    try:
        columns = resolve_product_columns(conn)
        updates = []
        params = []
        if columns["name"]:
            updates.append(f"{columns['name']} = %s")
            params.append(name)
        if columns["price"]:
            updates.append(f"{columns['price']} = %s")
            params.append(price)
        if new_image_url and columns["image"]:
            updates.append(f"{columns['image']} = %s")
            params.append(new_image_url)
            if columns["image_public_id"] and new_public_id:
                updates.append(f"{columns['image_public_id']} = %s")
                params.append(new_public_id)
        if columns["quantity"] and quantity_field_present:
            updates.append(f"{columns['quantity']} = %s")
            params.append(quantity_value)
        if not updates:
            flash("Nothing to update.", "info")
            return redirect(url_for("admin_edit_product", product_id=product_id))
        params.append(product_id)
        cur = conn.cursor()
        cur.execute(f"UPDATE Products SET {', '.join(updates)} WHERE {columns['id']} = %s", tuple(params))
        conn.commit()
        cur.close()
        if new_image_url:
            old_public_id = product.get("image_public_id") or extract_public_id_from_url(product.get("image"))
            delete_product_image(old_public_id)
        flash("Product updated.", "success")
        return redirect(url_for("products"))
    except Exception as exc:
        conn.rollback()
        if new_public_id:
            delete_product_image(new_public_id)
        app.logger.exception("Failed to update product %s: %s", product_id, exc)
        flash("Unable to update product.", "error")
        return redirect(url_for("admin_edit_product", product_id=product_id))
    finally:
        conn.close()


@app.post("/admin/delete-product/<int:product_id>")
@admin_required
def admin_delete_product(product_id):
    success, message, _ = remove_product_record(product_id)
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": success, "message": message})
    flash(message, "success" if success else "error")
    return redirect(url_for("products"))


@app.post("/admin/delete-all-products")
@admin_required
def admin_delete_all_products():
    conn = get_db_connection()
    deleted = []
    try:
        columns = resolve_product_columns(conn)
        select_cols = [f"{columns['id']} AS id"]
        aliases = ["id"]
        for alias, column in (
            ("image", columns["image"]),
            ("image_public_id", columns["image_public_id"]),
        ):
            if column:
                select_cols.append(f"{column} AS {alias}")
                aliases.append(alias)
        cur = conn.cursor()
        if len(select_cols) > 1:
            cur.execute(f"SELECT {', '.join(select_cols)} FROM Products")
            for row in cur.fetchall():
                record = {}
                for idx, alias in enumerate(aliases):
                    record[alias] = row[idx]
                deleted.append(record)
        cur.execute("DELETE FROM Products")
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Failed to delete all products: %s", exc)
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "message": "Unable to delete products."}), 500
        flash("Unable to delete products.", "error")
        return redirect(url_for("products"))
    finally:
        conn.close()
    for record in deleted:
        public_id = record.get("image_public_id") or extract_public_id_from_url(record.get("image"))
        delete_product_image(public_id)
    message = f"Deleted {len(deleted)} products."
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "message": message})
    flash(message, "success")
    return redirect(url_for("products"))


def delete_all_orders():
    conn = get_db_connection()
    try:
        lookup, table_name = resolve_table_columns_generic(conn, ['OrderItems', 'order_items', 'orderitems'])
        if lookup:
            cur = conn.cursor()
            cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table_name)))
            cur.close()
        cur = conn.cursor()
        cur.execute("DELETE FROM Orders")
        affected = cur.rowcount
        conn.commit()
        cur.close()
        return affected
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Failed to delete orders: %s", exc)
        raise
    finally:
        conn.close()


@app.post("/admin/delete-all-orders")
@admin_required
def admin_delete_all_orders():
    wants_json = request.accept_mimetypes.best == "application/json"
    try:
        count = delete_all_orders()
        message = f"Deleted {count} orders."
        if wants_json:
            return jsonify({"ok": True, "message": message})
        flash(message, "success")
    except Exception:
        message = "Unable to delete orders."
        if wants_json:
            return jsonify({"ok": False, "message": message}), 500
        flash(message, "error")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/orders/delete/<int:order_id>")
@admin_required
def admin_delete_single_order(order_id):
    wants_json = request.accept_mimetypes.best == "application/json"
    try:
        delete_all_orders_for_ids([order_id])
        message = f"Order #{order_id} deleted."
        if wants_json:
            return jsonify({"ok": True, "message": message})
        flash(message, "success")
    except Exception:
        message = "Unable to delete that order."
        if wants_json:
            return jsonify({"ok": False, "message": message}), 500
        flash(message, "error")
    return redirect(url_for("admin_dashboard"))


def delete_all_orders_for_ids(order_ids):
    if not order_ids:
        return
    conn = get_db_connection()
    try:
        lookup, table_name = resolve_table_columns_generic(conn, ['OrderItems', 'order_items', 'orderitems'])
        if lookup:
            cur = conn.cursor()
            order_fk = None
            for cand in ('order_id', 'orderid', 'order'):
                if cand in lookup:
                    order_fk = lookup[cand]
                    break
            if not order_fk:
                for key, actual in lookup.items():
                    if 'order' in key and key.endswith('id'):
                        order_fk = actual
                        break
            if order_fk:
                stmt = sql.SQL("DELETE FROM {table} WHERE {col} = ANY(%s)").format(
                    table=sql.Identifier(table_name),
                    col=sql.Identifier(order_fk),
                )
                cur.execute(stmt, (order_ids,))
                cur.close()
        cur = conn.cursor()
        cur.execute(sql.SQL("DELETE FROM Orders WHERE id = ANY(%s)"), (order_ids,))
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Failed to delete orders %s: %s", order_ids, exc)
        raise
    finally:
        conn.close()

@app.route("/add-to-cart", methods=["POST"])
def add_to_cart():
    payload = request.get_json(silent=True) if request.is_json else None
    product_id = (payload or {}).get("id") if payload else None
    if not product_id:
        product_id = request.form.get("id")

    quantity_raw = (payload or {}).get("quantity") if payload else request.form.get("quantity")
    try:
        quantity = max(1, int(quantity_raw)) if quantity_raw is not None else 1
    except Exception:
        quantity = 1
    session["quantity"] = quantity
    cart = session.get("cart", [])

    def respond(ok=True, status=200, **extra):
        items_count = 0
        for it in cart:
            try:
                items_count += int(it.get("quantity", 1))
            except Exception:
                continue
        cart_total = 0.0
        for it in cart:
            try:
                cart_total += float(it.get("price", 0)) * int(it.get("quantity", 1))
            except Exception:
                continue
        cart_total = round(cart_total, 2)
        session["cart"] = cart
        session["itemsInCart"] = items_count
        session["cartTotal"] = cart_total
        cart_payload = []
        for it in cart:
            try:
                qty = int(it.get("quantity", 1))
            except Exception:
                qty = 1
            try:
                price_val = float(it.get("price", 0.0))
            except Exception:
                price_val = 0.0
            image_val = it.get("image")
            image_url = build_media_url(image_val)
            cart_payload.append({
                "id": it.get("id"),
                "name": it.get("name") or it.get("title"),
                "title": it.get("title") or it.get("name"),
                "price": price_val,
                "quantity": qty,
                "image": image_val,
                "image_url": image_url,
            })
        if request.is_json:
            payload = {"ok": ok, "items_count": items_count, "cart_total": cart_total}
            payload.update(extra)
            payload["cart"] = cart_payload
            return jsonify(payload), status
        return redirect(url_for("products"))

    if not product_id:
        if not request.is_json:
            flash("Unable to add that product.", "error")
        return respond(ok=False, status=400, error="Missing product id")
    
    # Fetch product from DB
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        pid_param = int(product_id)
    except Exception:
        pid_param = product_id
    # Dynamically resolve id/quantity columns
    pcols = {c.lower() for c in table_columns(conn, 'Products')}
    id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
    qty_col = next((c for c in ('quantity','qty','stock','in_stock') if c in pcols), None)
    if qty_col:
        cursor.execute(f"SELECT {id_col} AS id, name, price, image, {qty_col} AS qty FROM Products WHERE {id_col} = %s", (pid_param,))
    else:
        cursor.execute(f"SELECT {id_col} AS id, name, price, image FROM Products WHERE {id_col} = %s", (pid_param,))
    row = cursor.fetchone()
    desc = cursor.description
    conn.close()

    if row:
        # Map column names -> values so tuple cursors work like dict lookups
        row_map = {}
        if desc:
            for idx, col in enumerate(desc):
                try:
                    key = col.name.lower()
                except AttributeError:
                    key = str(col[0]).lower()
                row_map[key] = row[idx] if idx < len(row) else None
        price_val = 0.0
        raw_price = row_map.get('price')
        if raw_price is None and hasattr(row, 'price'):
            raw_price = row.price
        if raw_price is not None:
            try:
                price_val = float(raw_price)
            except Exception:
                price_val = 0.0
        available = None
        qty_val = row_map.get('qty')
        if qty_val is None and hasattr(row, 'qty'):
            qty_val = getattr(row, 'qty')
        if qty_val is not None:
            try:
                available = int(qty_val)
            except Exception:
                available = None
        raw_id = row_map.get('id')
        if raw_id is None and hasattr(row, 'id'):
            raw_id = row.id
        elif raw_id is None and len(row) > 0:
            raw_id = row[0]
        name_val = row_map.get('name') if 'name' in row_map else getattr(row, 'name', None)
        image_val = row_map.get('image') if 'image' in row_map else getattr(row, 'image', None)
        product = {"id": str(raw_id), "name": name_val, "price": price_val, "image": image_val, "quantity": quantity}

        # Current quantity in cart for this product
        current_in_cart = 0
        for it in cart:
            if str(it.get("id")) == str(product.get("id")):
                try:
                    current_in_cart += int(it.get("quantity", 0))
                except Exception:
                    pass

        # Enforce stock cap if available known
        if available is not None and current_in_cart >= available:
            message = f"Only {available} left in stock."
            if not request.is_json:
                flash(message, "error")
            return respond(ok=False, status=400, error=message)

        for item in cart:
            if item.get("id") == product.get("id"):
                # Add one but do not exceed available
                new_qty = int(item.get("quantity", 1)) + 1
                if available is not None and new_qty > available:
                    new_qty = available
                    notice = f"Cart limited to available stock ({available})."
                    if not request.is_json:
                        flash(notice, "info")
                else:
                    notice = None
                item["quantity"] = new_qty
                return respond(message=notice or "Added to cart")
        # New line item; ensure at least 1 and not exceeding available
        if available is not None and available <= 0:
            sold_out_msg = "This item is sold out."
            if not request.is_json:
                flash(sold_out_msg, "error")
            return respond(ok=False, status=400, error=sold_out_msg)
        cart.append(product)
    else:
        not_found_msg = "Product not found."
        if not request.is_json:
            flash(not_found_msg, "error")
        return respond(ok=False, status=404, error=not_found_msg)

    return respond(message="Added to cart")


@app.route("/remove-from-cart", methods=["POST"])
def remove_from_cart():
    # Support JSON (AJAX) and form fallback
    if request.is_json:
        data = request.get_json(silent=True) or {}
        product_id = str(data.get("id")) if data.get("id") is not None else None
    else:
        product_id = request.form.get("id")

    cart = session.get("cart", [])
    # Remove the entire item regardless of quantity
    cart = [it for it in cart if str(it.get("id")) != str(product_id)]

    # Recalculate totals
    items_count = sum(int(it.get("quantity", 1)) for it in cart if str(it.get("quantity", 1)).isdigit())
    cart_total = 0.0
    for it in cart:
        try:
            cart_total += float(it.get("price", 0)) * int(it.get("quantity", 1))
        except Exception:
            continue
    cart_total = round(cart_total, 2)

    session["cart"] = cart
    session["itemsInCart"] = items_count
    session["cartTotal"] = cart_total

    if request.is_json:
        return jsonify({
            "ok": True,
            "items_count": items_count,
            "cart_total": cart_total,
            "removed_id": product_id,
        })

    return redirect(url_for("products"))

@app.route("/get-cart")
def get_cart():
    cart = session.get("cart", [])
    # print(cart)
    return jsonify(session.get("cart", []))
    
@app.route("/increase_cart", methods=["POST"])
def increase_cart():
    cart = session.get("cart", [])
    if request.is_json:
        data = request.get_json(silent=True) or {}
        id = str(data.get("id"))
    else:
        id = request.form.get("id")

    new_qty = None
    for item in cart:
        if item.get("id") == id:
            # Check available stock for this product
            try:
                pid_param = int(id)
            except Exception:
                pid_param = id
            conn = get_db_connection(); cur = conn.cursor()
            pcols = {c.lower() for c in table_columns(conn, 'Products')}
            id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
            qty_col = next((c for c in ('quantity','qty','stock','in_stock') if c in pcols), None)
            available = None
            if qty_col:
                try:
                    cur.execute(f"SELECT {qty_col} AS qty FROM Products WHERE {id_col} = %s", (pid_param,))
                    r = cur.fetchone()
                    if r and r[0] is not None:
                        available = int(r[0])
                except Exception:
                    available = None
            cur.close(); conn.close()

            candidate = int(item.get("quantity", 1)) + 1
            if available is not None and candidate > available:
                new_qty = int(item.get("quantity", 1))  # unchanged
            else:
                item["quantity"] = candidate
                new_qty = item["quantity"]
            break
    session["cart"] = cart

    # If AJAX/JSON, return updated totals without redirect
    if request.is_json:
        items_count = sum(int(i.get("quantity", 1)) for i in cart)
        cart_total = round(sum(float(i.get("price", 0)) * int(i.get("quantity", 1)) for i in cart), 2)
        session["itemsInCart"] = items_count
        session["cartTotal"] = cart_total
        return jsonify({
            "ok": True,
            "id": id,
            "new_quantity": new_qty,
            "items_count": items_count,
            "cart_total": cart_total,
        })

    return redirect(url_for("products"))

            
@app.route("/decrease_cart", methods=["POST"])
def decrease_cart():
    cart = session.get("cart", [])
    if request.is_json:
        data = request.get_json(silent=True) or {}
        id = str(data.get("id"))
    else:
        id = request.form.get("id")

    removed = False
    new_qty = None
    for item in cart:
        if item.get("id") == id:
            item["quantity"] = int(item.get("quantity", 1)) - 1
            if item.get("quantity") <= 0:
                cart.remove(item)
                removed = True
                new_qty = 0
            else:
                new_qty = item["quantity"]
            break

    session["cart"] = cart

    if request.is_json:
        items_count = sum(int(i.get("quantity", 1)) for i in cart)
        cart_total = round(sum(float(i.get("price", 0)) * int(i.get("quantity", 1)) for i in cart), 2)
        session["itemsInCart"] = items_count
        session["cartTotal"] = cart_total
        return jsonify({
            "ok": True,
            "id": id,
            "new_quantity": new_qty,
            "removed": removed,
            "items_count": items_count,
            "cart_total": cart_total,
        })

    return redirect(url_for("products"))

@app.route("/shipping", methods=["GET", "POST"])
def shipping():
    if "email" not in session:
        flash("Please continue as a guest or sign in before checkout.")
        return redirect(url_for("checkout_guest"))

    if request.method == "POST":
        address = request.form.get("address", "").strip()
        if not address:
            flash("Please enter a valid shipping address.")
            return redirect(url_for("shipping"))

        session["shipping_address"] = address
        return redirect(url_for("checkout_stripe"))

    return render_template("shipping.html", user=session["email"])


# @app.route("/checkout_stripe", methods=["POST"])
# def checkout_stripe():
#     cart = session.get("cart", [])
#     email = session.get("email")
    
#     if not cart:
#         return redirect(url_for("products"))
    
    

#     line_items = [
#         {
#             "price_data": {
#                 "currency": "zar",
#                 "product_data": {"name": item["name"]},
#                 "unit_amount": int(item["price"] * 100),  
#             },
#             "quantity": item["quantity"],
#         }
#         for item in cart
#     ]

#     checkout_session = stripe.checkout.Session.create(
#         payment_method_types=["card"],
#         line_items=line_items,
#         mode="payment",
#         success_url=url_for("success", _external=True),
#         cancel_url=url_for("products", _external=True),
#     )
#     print(email)
#     return redirect(checkout_session.url, code=303)


#-----------------------------------------------------------------------------------------------------
# @app.route("/checkout_stripe", methods=["GET", "POST"])
# def checkout_stripe():
#     # block purchase if neither user nor guest info present
#     # if "email" not in session and "guest_checkout" not in session:
#     #     flash("Please continue as a guest or sign in before checkout.")
#     #     return redirect(url_for("checkout_guest"))
    

#     cart = session.get("cart", [])
#     if not cart:
#         flash("Your cart is empty.")
#         return redirect(url_for("products"))

#     line_items = [
#         {
#             "price_data": {
#                 "currency": "zar",
#                 "product_data": {"name": item["name"]},
#                 "unit_amount": int(item["price"] * 100),  # cents
#             },
#             "quantity": int(item.get("quantity", 1)),
#         }
#         for item in cart
#     ]

#     # Optional: attach guest/user info
#     customer_email = session.get("user")  # if you store email in session
#     guest = session.get("guest_checkout")  # dict with first_name, last_name, address

#     checkout_session = stripe.checkout.Session.create(
#         payment_method_types=["card"],
#         line_items=line_items,
#         mode="payment",
#         success_url=url_for("success", _external=True),
#         cancel_url=url_for("products", _external=True),
#         customer_email=customer_email if customer_email else None,
#         metadata=guest if guest else None,
#     )

#     return redirect(checkout_session.url, code=303)
#-------------------------------------------------------------------------------------------------------

@app.route("/checkout_stripe", methods=["GET", "POST"])
def checkout_stripe():
    cart = session.get("cart", [])
    if not cart:
        flash("Your cart is empty.")
        return redirect(url_for("products"))

    # Build line items safely
    def _as_int(v, default=1):
        try:
            return int(v)
        except Exception:
            return default
    def _as_float(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    line_items = [
        {
            "price_data": {
                "currency": "zar",
                "product_data": {"name": item.get("name", "Item")},
                "unit_amount": int(_as_float(item.get("price", 0.0)) * 100),
            },
            "quantity": _as_int(item.get("quantity", 1)),
        }
        for item in cart
    ]

    guest = session.get("guest_checkout") or {}
    user_email = session.get("email") or guest.get("email")
    address = session.get("shipping_address") or guest.get("address")
    phone = session.get("shipping_phone") or guest.get("phone")

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=line_items,
        mode="payment",
        success_url=url_for("success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("products", _external=True),
        customer_email=user_email if user_email else None,
        metadata={
            "email": user_email or "",
            "address": address or "",
            "first_name": (guest.get("first_name") or "") if not session.get("email") else "",
            "last_name": (guest.get("last_name") or "") if not session.get("email") else "",
            "phone": phone or "",
        },
    )

    return redirect(checkout_session.url, code=303)




#_________________________________________________________________________________________________________

@app.route("/checkout")
def checkout():
    # if user is not logged in, show the guest/Signup gate
    if "email" not in session:
        return redirect(url_for("checkout_guest"))
    # ... your existing logged-in checkout flow here ...
    return redirect(url_for("checkout_stripe_entry"))  # example

@app.route("/checkout/guest", methods=["GET"])
def checkout_guest():
    return render_template("checkout_guest.html")

@app.route("/checkout/guest/submit", methods=["POST"])
def checkout_guest_submit():
    # Grab guest info
    email      = request.form.get("email", "").strip()
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()
    phone      = request.form.get("phone", "").strip()
    address    = request.form.get("address", "").strip()

    # Minimal validation
    if not email or "@" not in email or "." not in email:
        flash("Please enter a valid email address.")
        return redirect(url_for("checkout_guest"))
    if not first_name or not last_name or not address or not phone:
        flash("Please fill in all required fields.")
        return redirect(url_for("checkout_guest"))

    # Store in session and proceed to payment (or summary)
    session["guest_checkout"] = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "address": address
    }
    
    session["order"] = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "address": address,
        "products": [],
        "total": 0,
        "quantity": 0
    }

    # 👉 If you want to force Stripe checkout next, redirect to your Stripe route
    return redirect(url_for("checkout_stripe"))
#_________________________________________________________________________________________________________

@app.route("/clearCart", methods=["POST"])
def clearCart():
    # Clear all items
    session["cart"] = []
    session["itemsInCart"] = 0
    session["cartTotal"] = 0.0

    if request.is_json:
        return jsonify({"ok": True, "items_count": 0, "cart_total": 0.0})

    return redirect(url_for("products"))

@app.route("/services")
def services():
    return render_template("services.html")


# ------ Service booking (Stripe checkout using form address) ------
@app.post('/services/book')
def service_book():
    # Collect form fields
    full_name = (request.form.get('full_name') or '').strip()
    email = (request.form.get('email') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    address = (request.form.get('address') or '').strip()
    city = (request.form.get('city') or '').strip()
    postcode = (request.form.get('postcode') or '').strip()
    service_type = (request.form.get('service_type') or '').strip()
    frequency = (request.form.get('frequency') or 'once').strip()
    date = (request.form.get('date') or '').strip()
    time_window = (request.form.get('time_window') or '').strip()
    notes = (request.form.get('notes') or '').strip()
    extras = {
        'extra_fridge': 1 if request.form.get('extra_fridge') else 0,
        'extra_oven': 1 if request.form.get('extra_oven') else 0,
        'extra_windows': 1 if request.form.get('extra_windows') else 0,
        'extra_laundry': 1 if request.form.get('extra_laundry') else 0,
        'extra_pets': 1 if request.form.get('extra_pets') else 0,
    }

    # Basic validation
    if not (full_name and email and phone and address and city and postcode and service_type and date and time_window):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('services'))

    # Price calculation
    base_prices = {
        'Basic Cleaning': 300.0,
        'Deep Cleaning': 600.0,
        'Office Cleaning': 1200.0,
    }
    extras_prices = {
        'extra_fridge': 80.0,
        'extra_oven': 100.0,
        'extra_windows': 120.0,
        'extra_laundry': 70.0,
        'extra_pets': 50.0,
    }
    price = base_prices.get(service_type, 0.0)
    for k, v in extras.items():
        if v:
            price += extras_prices.get(k, 0.0)

    # Save booking record (best-effort, supports minimal Bookings schema)
    booking_id = None
    try:
        conn = get_db_connection()
        cols = {c.lower() for c in table_columns(conn, 'Bookings')}
        cur = conn.cursor()
        # Prepare insert using the columns that exist
        insert_cols = []
        params = []
        values_placeholders = []

        def add(col_name, value):
            if col_name.lower() in cols:
                insert_cols.append(col_name)
                params.append(value)
                values_placeholders.append('%s')

        # Common columns in simple schema
        add('name', full_name)
        add('email', email)
        add('phone', phone)
        full_address = f"{address}, {city}, {postcode}".strip(', ')
        add('address', full_address)
        add('service_type', service_type)
        add('date', date)
        add('time', time_window)
        add('frequency', frequency)
        add('notes', notes)
        add('price', price)
        add('status', 'pending')

        # If no matching columns at all, try a minimal subset
        if not insert_cols:
            insert_cols = ['name', 'address', 'service_type', 'date']
            params = [full_name, full_address, service_type, date]
            values_placeholders = ['%s','%s','%s','%s']

        returning_col = None
        for cand in ('id', 'booking_id'):
            if cand in cols:
                returning_col = cand
                break
        returning_clause = f" RETURNING {returning_col}" if returning_col else ""

        sql = f"INSERT INTO Bookings ({', '.join(insert_cols)}) VALUES ({', '.join(values_placeholders)}){returning_clause}"
        cur.execute(sql, tuple(params))
        if returning_col:
            rid = cur.fetchone()
            if rid and rid[0] is not None:
                booking_id = int(rid[0])
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print('service_book insert error:', e)
        try:
            conn.close()
        except Exception:
            pass

    # Store brief info for success page and Stripe metadata
    session['service_booking'] = {
        'id': booking_id,
        'full_name': full_name,
        'email': email,
        'phone': phone,
        'address': full_address,
        'service_type': service_type,
        'frequency': frequency,
        'date': date,
        'time_window': time_window,
        'notes': notes,
        'extras': extras,
        'price': price,
    }

    # Create Stripe Checkout session for this service
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'zar',
                    'product_data': {'name': f"Cleaning Service - {service_type}"},
                    'unit_amount': int(price * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=url_for('service_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('services', _external=True),
            customer_email=email if email else None,
            metadata={
                'booking_id': str(booking_id or ''),
                'full_name': full_name,
                'phone': phone,
                'address': full_address,
                'service_type': service_type,
                'frequency': frequency,
                'date': date,
                'time_window': time_window,
            }
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        print('stripe create session error (service):', e)
        flash('Failed to start payment. Please try again.', 'error')
        return redirect(url_for('services'))


@app.get('/services/success')
def service_success():
    session_id = request.args.get('session_id')
    if not session_id:
        return redirect(url_for('services'))
    try:
        cs = stripe.checkout.Session.retrieve(session_id)
        if hasattr(cs, 'payment_status') and cs.payment_status not in ('paid', 'no_payment_required'):
            flash('Payment not completed. Please try again.', 'error')
            return redirect(url_for('services'))
    except Exception as e:
        print('service success stripe retrieve error:', e)

    booking = session.get('service_booking') or {}
    # Optionally update status to 'paid' if the column exists
    try:
        if booking.get('id') is not None:
            conn = get_db_connection(); cols = {c.lower() for c in table_columns(conn, 'Bookings')}
            if 'status' in cols:
                cur = conn.cursor()
                cur.execute("UPDATE Bookings SET status = %s WHERE id = %s", ('paid', int(booking['id'])))
                conn.commit(); cur.close(); conn.close()
            else:
                conn.close()
    except Exception as e:
        print('service mark paid error:', e)

    # After successful payment, persist a service order row for admin
    try:
        if booking:
            # Try primary table name 'service_order', then fallbacks
            table_name_candidates = ['service_order', 'ServiceOrders', 'service_orders', 'Service_Order']
            conn = get_db_connection()
            chosen = None
            for t in table_name_candidates:
                try:
                    cols = table_columns(conn, t)
                    if cols:
                        chosen = (t, {c.lower() for c in cols})
                        break
                except Exception:
                    continue
            if chosen is not None:
                tname, cols = chosen
                cur = conn.cursor()
                insert_cols = []
                params = []
                q = []
                def add(col, val):
                    if col.lower() in cols:
                        insert_cols.append(col)
                        params.append(val)
                        q.append('%s')
                add('full_name', booking.get('full_name'))
                # Some schemas use 'name'
                if 'name' in cols and 'full_name' not in [c.lower() for c in insert_cols]:
                    insert_cols.append('name'); params.append(booking.get('full_name')); q.append('%s')
                add('email', booking.get('email'))
                add('phone', booking.get('phone'))
                add('address', booking.get('address'))
                add('service_type', booking.get('service_type'))
                add('frequency', booking.get('frequency'))
                add('date', booking.get('date'))
                # Some schemas might have separate time column names
                if 'time' in cols:
                    add('time', booking.get('time_window'))
                elif 'time_window' in cols:
                    add('time_window', booking.get('time_window'))
                add('notes', booking.get('notes'))
                add('price', booking.get('price'))
                add('status', 'paid')
                # Optional created_at defaults handled by DB; otherwise try set now
                if 'created_at' in cols:
                    try:
                        cur.execute("SELECT CURRENT_TIMESTAMP")
                        now_row = cur.fetchone()
                        now_val = now_row[0] if now_row else None
                        if now_val is not None:
                            insert_cols.append('created_at'); params.append(now_val); q.append('%s')
                    except Exception:
                        pass
                if insert_cols:
                    sql = f"INSERT INTO {tname} ({', '.join(insert_cols)}) VALUES ({', '.join(q)})"
                    cur.execute(sql, tuple(params))
                    conn.commit()
                cur.close(); conn.close()
            else:
                try: conn.close()
                except Exception: pass
    except Exception as e:
        print('service_success insert service_order error:', e)

    # Clear booking session
    session.pop('service_booking', None)
    flash('Your service booking was received. Thank you!', 'success')
    return redirect(url_for('home'))


# ------ Admin: Services list (from Bookings) ------
@app.get('/admin/services/list')
@admin_required
def admin_services_list():
    # Read service orders from 'service_order' (preferred), with fallbacks.
    conn = get_db_connection()
    try:
        table_candidates = ['service_order', 'ServiceOrders', 'service_orders', 'Service_Order']
        chosen = None
        cols = set()
        for t in table_candidates:
            cols = {c.lower() for c in table_columns(conn, t)}
            if cols:
                chosen = t
                break
        if not chosen:
            conn.close()
            return jsonify(ok=True, items=[])

        id_col = table_id_column(conn, chosen, ("id", "service_order_id", "ServiceOrderID", "serviceorderid"))
        select_cols = [f"{id_col} AS id"]
        field_map = []
        for cand, key in (
            ('full_name','full_name'), ('name','full_name'), ('email','email'), ('phone','phone'),
            ('address','address'), ('service_type','service_type'), ('date','date'),
            ('time','time_window'), ('time_window','time_window'), ('frequency','frequency'),
            ('notes','notes'), ('price','price'), ('status','status'), ('created_at','created_at')
        ):
            if cand in cols:
                select_cols.append(cand)
                field_map.append((cand, key))
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT {', '.join(select_cols)} FROM {chosen} ORDER BY {id_col} DESC LIMIT 50")
        except Exception:
            cur.execute(f"SELECT {', '.join(select_cols)} FROM {chosen} ORDER BY {id_col} DESC")
        items = []
        for row in cur.fetchall():
            idx = 0
            rid = row[idx]; idx += 1
            try: rid = int(rid)
            except Exception: pass
            obj = {'id': rid}
            for c, key in field_map:
                val = row[idx] if len(row) > idx else None; idx += 1
                if key == 'price':
                    try: val = float(val) if val is not None else None
                    except Exception: pass
                obj[key] = val
            items.append(obj)
        cur.close(); conn.close()
        return jsonify(ok=True, items=items)
    except Exception as e:
        try: conn.close()
        except Exception: pass
        print('admin_services_list error:', e)
        return jsonify(ok=False, error='server_error'), 500

# @app.route("/checkout", methods=["GET", "POST"])
# def checkout():
#     if request.method == "POST":
#         name = request.form["name"]
#         email = request.form["email"]
#         service = request.form["service"]
#         price = request.form["price"]

#         # Normally you would integrate Stripe here.
#         return f"<h2>Checkout Successful!</h2><p>Thank you {name}, you selected {service} for {price}.</p>"

#     return render_template("checkout.html")


@app.route("/success")
def success():
    # Optional: verify Stripe checkout session
    session_id = request.args.get("session_id")
    if session_id:
        try:
            cs = stripe.checkout.Session.retrieve(session_id)
            if hasattr(cs, "payment_status") and cs.payment_status not in ("paid", "no_payment_required"):
                flash("Payment not completed. Please try again.", "error")
                return redirect(url_for("products"))
        except Exception as e:
            print("Stripe retrieval error:", e)

    cart = session.get("cart", [])
    if not cart:
        flash("Your cart is empty.")
        return redirect(url_for("products"))

    guest = session.get("guest_checkout") or {}
    user_email = session.get("email") or guest.get("email")
    address = session.get("shipping_address") or guest.get("address")
    phone = session.get("shipping_phone") or (guest.get("phone") if guest else None)
    first_name = guest.get("first_name") if guest else None
    last_name = guest.get("last_name") if guest else None

    # If logged in and no guest names, try loading names from Users table
    if session.get("email") and (not first_name or not last_name):
        fn, ln = get_user_names_by_email(session.get("email"))
        first_name = first_name or fn
        last_name = last_name or ln

    conn = get_db_connection()
    cursor = conn.cursor()

    # Normalize items and backfill price/name from DB if needed
    norm_cart = []
    for item in cart:
        try:
            pid = int(item.get("id")) if item.get("id") is not None else None
        except Exception:
            pid = item.get("id")
        try:
            qty = int(item.get("quantity", 1))
        except Exception:
            qty = 1
        try:
            price = float(item.get("price", 0.0))
        except Exception:
            price = 0.0
        if (price is None or price == 0.0) and pid is not None:
            cursor.execute("SELECT price, name FROM Products WHERE id = %s", (pid,))
            row = cursor.fetchone()
            if row:
                price = float(row[0]) if row[0] is not None else 0.0
                if not item.get("name"):
                    item["name"] = row[1]
        norm_cart.append({
            "id": pid if pid is not None else item.get("id"),
            "name": item.get("name", "Item"),
            "quantity": qty,
            "price": price,
        })

    total_amount = sum(i["price"] * i["quantity"] for i in norm_cart)

    try:
        new_order_id = None
        # Ensure phone column exists alongside address
        ensure_orders_phone_column(conn)
        # Dynamically include guest name fields if columns exist
        cols = table_columns(conn, 'Orders')
        base_cols = ['user_email', 'address', 'total_amount', 'order_date']
        insert_cols = ['user_email', 'address', 'total_amount']
        insert_vals = [user_email, address, float(total_amount)]
        # Consider common column names for guest names
        fname_col = None
        lname_col = None
        for cand in ('first_name', 'customer_first_name', 'guest_first_name'):
            if cand in cols:
                fname_col = cand
                break
        for cand in ('last_name', 'customer_last_name', 'guest_last_name'):
            if cand in cols:
                lname_col = cand
                break
        if fname_col:
            insert_cols.append(fname_col)
            insert_vals.append(first_name)
        if lname_col:
            insert_cols.append(lname_col)
            insert_vals.append(last_name)

        # Optional phone column
        phone_col = None
        for cand in ('phone', 'contact_number', 'mobile'):
            if cand in cols:
                phone_col = cand
                break
        if phone_col:
            insert_cols.append(phone_col)
            insert_vals.append(phone)

        order_id_col = table_id_column(conn, 'Orders', ("id", "order_id", "OrderID", "orderid"))
        col_list = ", ".join(insert_cols + ['order_date'])
        placeholders = ", ".join(["%s"] * len(insert_cols)) + ", CURRENT_TIMESTAMP"
        insert_sql = f"INSERT INTO Orders ({col_list}) VALUES ({placeholders}) RETURNING {order_id_col}"
        cursor.execute(insert_sql, tuple(insert_vals))
        row = cursor.fetchone()
        if not row or row[0] is None:
            raise Exception("Failed to retrieve order id")
        new_order_id = int(row[0])

        conn.commit()

        for i in norm_cart:
            cursor.execute(
                "INSERT INTO OrderItems (order_id, product_id, quantity, price) VALUES (%s, %s, %s, %s)",
                (new_order_id, i["id"], i["quantity"], i["price"])
            )
        conn.commit()

        # Decrement product quantities based on ordered amounts
        try:
            pcols = {c.lower() for c in table_columns(conn, 'Products')}
            qty_col = next((c for c in ('quantity', 'qty', 'stock', 'in_stock') if c in pcols), None)
            if qty_col:
                id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
                for i in norm_cart:
                    pid = i.get("id")
                    try:
                        qty_to_sub = int(i.get("quantity", 0))
                    except Exception:
                        qty_to_sub = 0
                    if pid is None or qty_to_sub <= 0:
                        continue
                    cursor.execute(
                        f"UPDATE Products SET {qty_col} = CASE WHEN COALESCE({qty_col},0) - %s < 0 THEN 0 ELSE COALESCE({qty_col},0) - %s END WHERE {id_col} = %s",
                        (qty_to_sub, qty_to_sub, pid)
                    )
                conn.commit()
        except Exception as e:
            print("quantity decrement error:", e)
    except Exception as e:
        print("Database error:", e)
        flash("Error saving order details. Please contact support.")
        return redirect(url_for("products"))
    finally:
        cursor.close()

    # Send confirmation email (best effort)
    try:
        if user_email:
            send_order_confirmation_email(
                user_email,
                order_id=new_order_id,
                first_name=first_name,
                items=norm_cart,
                total_amount=total_amount,
                shipping_address=address,
            )
    except Exception as exc:
        app.logger.warning("Failed to send order confirmation: %s", exc)

    # Clear cart and transient checkout data after successful payment/order
    session.pop("cart", None)
    session.pop("cartTotal", None)
    session.pop("itemsInCart", None)
    session.pop("guest_checkout", None)
    session.pop("shipping_address", None)
    session.pop("order", None)

    flash("Your order was successfully logged. Thank you!", "success")
    return redirect(url_for("home"))

# --------------------------- Admin Views ---------------------------

def _orders_viewed_column(conn):
    cols = table_columns(conn, 'Orders')
    # try common variants
    for c in ("viewed", "is_viewed", "is_explored", "explored", "is_new"):
        if c in cols:
            return c
    return None


def ensure_orders_phone_column(conn):
    """Ensure the Orders table has a phone column alongside address.

    Adds a nullable NVARCHAR/VARCHAR column named 'phone' if missing.
    Safe to call repeatedly; errors are swallowed.
    """
    try:
        cols = table_columns(conn, 'Orders')
        if 'phone' not in cols:
            cur = conn.cursor()
            try:
                cur.execute("ALTER TABLE Orders ADD COLUMN IF NOT EXISTS phone VARCHAR(64)")
                conn.commit()
            except Exception:
                pass
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
    except Exception:
        pass

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db_connection()
    cur = conn.cursor()

    # Recent orders
    viewed_col = _orders_viewed_column(conn)
    id_col = table_id_column(conn, 'Orders', ("id", "order_id", "OrderID", "orderid"))
    orders_cols_raw = table_columns(conn, 'Orders')
    orders_cols = {c.lower(): c for c in orders_cols_raw}

    def find_column(primary_candidates=None, contains=None, exclude=None):
        """Return the actual column name from Orders matching the request."""
        primary_candidates = primary_candidates or []
        exclude = exclude or []
        for cand in primary_candidates:
            if not cand:
                continue
            key = cand.lower()
            if key in orders_cols:
                return orders_cols[key]
        if contains:
            for key, actual in orders_cols.items():
                if contains in key and all(excl not in key for excl in exclude):
                    return actual
        return None

    select_bits = [f"{id_col} AS id"]
    alias_order = ["id"]

    def add_select(alias, column):
        if not column:
            return
        if column.lower() == alias.lower():
            select_bits.append(column)
        else:
            select_bits.append(f"{column} AS {alias}")
        alias_order.append(alias)

    email_col = find_column(["user_email", "email", "customer_email", "contact_email", "email_address"], contains="email")
    address_col = find_column(["address", "shipping_address", "delivery_address", "street_address"], contains="address", exclude=["email"])
    total_col = find_column(["total_amount", "total", "amount", "grand_total", "order_total"], contains="total")
    order_date_actual = find_column(["order_date", "ordered_at", "created_at", "date", "timestamp"], contains="date")
    phone_col = find_column(["phone", "phone_number", "contact_number", "mobile"], contains="phone")
    first_name_col = find_column(["first_name", "firstname", "customer_first_name", "given_name"])
    last_name_col = find_column(["last_name", "lastname", "customer_last_name", "surname", "family_name"])
    full_name_col = find_column(["full_name", "customer_name", "client_name"], contains="name", exclude=["first", "last", "user", "company"])
    payment_status_col = find_column(["payment_status", "status", "order_status"])

    add_select("user_email", email_col)
    add_select("address", address_col)
    add_select("total_amount", total_col)
    add_select("order_date", order_date_actual)
    add_select("phone", phone_col)
    add_select("first_name", first_name_col)
    add_select("last_name", last_name_col)
    add_select("full_name", full_name_col)
    add_select("payment_status", payment_status_col)
    viewed_alias = None
    if viewed_col:
        add_select("viewed_flag", viewed_col)
        viewed_alias = "viewed_flag"

    order_by_col = order_date_actual or id_col
    select_clause = ", ".join(select_bits)
    try:
        cur.execute(f"SELECT {select_clause} FROM Orders ORDER BY {order_by_col} DESC LIMIT 20")
    except Exception:
        cur.execute(f"SELECT {select_clause} FROM Orders ORDER BY {order_by_col} DESC")
    rows = cur.fetchall()

    orders = []
    for r in rows:
        row_data = {}
        for idx, alias in enumerate(alias_order):
            if idx < len(r):
                row_data[alias] = r[idx]
        oid_raw = row_data.get("id")
        try:
            oid = int(oid_raw) if oid_raw is not None else 0
        except Exception:
            oid = oid_raw or 0
        total_raw = row_data.get("total_amount")
        total_amount = 0.0
        if total_raw is not None:
            try:
                total_amount = float(total_raw)
            except Exception:
                try:
                    total_amount = float(str(total_raw))
                except Exception:
                    total_amount = 0.0
        order_date_val = row_data.get("order_date")
        if order_date_val is not None and not isinstance(order_date_val, str):
            try:
                order_date = str(order_date_val)
            except Exception:
                order_date = None
        else:
            order_date = order_date_val
        o = {
            "id": oid,
            "user_email": row_data.get("user_email"),
            "address": row_data.get("address"),
            "total_amount": total_amount,
            "order_date": order_date,
            "phone": row_data.get("phone"),
            "viewed": False,
            "first_name": row_data.get("first_name"),
            "last_name": row_data.get("last_name"),
            "full_name": row_data.get("full_name"),
            "payment_status": row_data.get("payment_status"),
        }
        if o.get("full_name") and not (o.get("first_name") or o.get("last_name")):
            try:
                parts = str(o["full_name"]).strip().split()
                if parts:
                    o["first_name"] = parts[0]
                    if len(parts) > 1:
                        o["last_name"] = " ".join(parts[1:])
            except Exception:
                pass
        if viewed_alias:
            o["viewed"] = bool(row_data.get(viewed_alias))
        else:
            # Session-scoped fallback for marking new
            seen = set(session.get("admin_seen_orders", []))
            o["viewed"] = o["id"] in seen
        # Attach customer name from Users table when available
        if not (o.get("first_name") or o.get("last_name") or o.get("full_name")) and o.get("user_email"):
            try:
                fn, ln = get_user_names_by_email(str(o.get("user_email")))
                if fn:
                    o["first_name"] = fn
                if ln:
                    o["last_name"] = ln
            except Exception:
                pass
        orders.append(o)

    # Aggregate items per order for quick glance
    items_per_order = {}
    order_items_summary = {}
    try:
        cur.execute("SELECT order_id, SUM(quantity) as items FROM OrderItems GROUP BY order_id")
        for (oid, items) in cur.fetchall():
            items_per_order[int(oid)] = int(items)
    except Exception:
        pass

    # Build product name lookup
    product_names = {}
    try:
        prod_cols = {c.lower(): c for c in table_columns(conn, 'Products')}
        if prod_cols:
            prod_id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
            prod_name_col = None
            for cand in ("name", "title", "product_name"):
                if cand in prod_cols:
                    prod_name_col = prod_cols[cand]
                    break
            if not prod_name_col:
                prod_name_col = next((actual for key, actual in prod_cols.items() if "name" in key), None)
            if prod_name_col:
                prod_cur = conn.cursor()
                try:
                    prod_cur.execute(f"SELECT {prod_id_col}, {prod_name_col} FROM Products")
                    for pid, pname in prod_cur.fetchall():
                        if pid is None:
                            continue
                        product_names[str(pid)] = pname
                finally:
                    prod_cur.close()
    except Exception:
        pass

    # Collect order item summaries
    try:
        oi_cols = {c.lower(): c for c in table_columns(conn, 'OrderItems')}
        order_fk = None
        prod_fk = None
        qty_col = None
        if oi_cols:
            for cand in ("order_id", "OrderID", "orderid"):
                if cand.lower() in oi_cols:
                    order_fk = oi_cols[cand.lower()]
                    break
            for cand in ("product_id", "ProductID", "productid"):
                if cand.lower() in oi_cols:
                    prod_fk = oi_cols[cand.lower()]
                    break
            for cand in ("quantity", "qty", "amount"):
                if cand in oi_cols:
                    qty_col = oi_cols[cand]
                    break
        if order_fk and prod_fk:
            select_cols = [f"{order_fk} AS order_id", f"{prod_fk} AS product_id"]
            if qty_col:
                select_cols.append(qty_col)
            cur.execute(f"SELECT {', '.join(select_cols)} FROM OrderItems")
            for row in cur.fetchall():
                if not row:
                    continue
                order_val = row[0]
                prod_val = row[1] if len(row) > 1 else None
                qty_val = 1
                if qty_col and len(row) > 2 and row[2] is not None:
                    try:
                        qty_val = int(row[2])
                    except Exception:
                        qty_val = 1
                try:
                    order_id = int(order_val)
                except Exception:
                    continue
                prod_label = None
                if prod_val is not None:
                    key = str(prod_val)
                    prod_label = product_names.get(key)
                    if prod_label is None:
                        try:
                            prod_label = product_names.get(str(int(prod_val)))
                        except Exception:
                            prod_label = None
                if not prod_label and prod_val is not None:
                    prod_label = f"Product #{prod_val}"
                entry = order_items_summary.setdefault(order_id, [])
                entry.append({"name": prod_label or "Product", "quantity": qty_val})
    except Exception as exc:
        app.logger.debug("order items summary error: %s", exc)

    for o in orders:
        o["items"] = items_per_order.get(o["id"], 0)
        o["items_list"] = order_items_summary.get(o["id"], [])

    # Product stats: quantities and revenue (dynamic column resolution)
    product_stats = []
    try:
        prod_id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
        prod_cols = {c.lower() for c in table_columns(conn, 'Products')}
        if 'name' in prod_cols:
            prod_name_col = 'name'
        else:
            # pick first column containing 'name'
            prod_name_col = next((c for c in prod_cols if 'name' in c), None)
            if not prod_name_col:
                prod_name_col = 'name'  # fallback

        oi_cols = {c.lower() for c in table_columns(conn, 'OrderItems')}
        oi_prod_col = 'product_id' if 'product_id' in oi_cols else (next((c for c in oi_cols if 'product' in c and c.endswith('id')), 'product_id'))

        sql = (
            f"SELECT p.{prod_id_col} AS id, p.{prod_name_col} AS name, "
            f"COALESCE(SUM(oi.quantity), 0) AS qty, COALESCE(SUM(oi.quantity * oi.price), 0) AS revenue "
            f"FROM Products p LEFT JOIN OrderItems oi ON oi.{oi_prod_col} = p.{prod_id_col} "
            f"GROUP BY p.{prod_id_col}, p.{prod_name_col} ORDER BY qty DESC"
        )
        cur.execute(sql)
        for row in cur.fetchall():
            product_stats.append({
                "id": int(row[0]) if row[0] is not None else 0,
                "name": row[1],
                "qty": int(row[2]) if row[2] is not None else 0,
                "revenue": float(row[3]) if row[3] is not None else 0.0,
            })
    except Exception as e:
        print("product stats error:", e)

    # Totals
    totals = {"orders": 0, "revenue": 0.0, "products": 0}
    try:
        cur.execute("SELECT COUNT(1), COALESCE(SUM(total_amount),0) FROM Orders")
        r = cur.fetchone()
        if r:
            totals["orders"] = int(r[0])
            totals["revenue"] = float(r[1])
    except Exception:
        pass
    try:
        cur.execute("SELECT COUNT(1) FROM Products")
        r = cur.fetchone()
        if r:
            totals["products"] = int(r[0])
    except Exception:
        pass

    # Potential users to promote to admin (non-admin accounts sorted by email)
    grantable_users = []
    try:
        user_cols = {c.lower() for c in table_columns(conn, 'Users')}
        if 'email' in user_cols and 'is_admin' in user_cols:
            cur2 = conn.cursor()
            cur2.execute(
                """
                SELECT email
                FROM Users
                WHERE COALESCE(is_admin, FALSE) = FALSE
                ORDER BY LOWER(email)
                """
            )
            grantable_users = [row[0] for row in cur2.fetchall() if row and row[0]]
            cur2.close()
    except Exception:
        grantable_users = []

    conn.close()

    return render_template(
        "admin_dashboard.html",
        orders=orders,
        product_stats=product_stats,
        totals=totals,
        viewed_col=viewed_col,
        grantable_users=grantable_users,
    )


@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    # Only allow when there is no admin in DB yet
    email = session.get("email")
    if not email:
        flash("Please log in to set up admin.", "error")
        return redirect(url_for("login"))

    # Ensure column exists
    try:
        conn = get_db_connection()
        cols = table_columns(conn, 'Users')
        if 'is_admin' not in cols:
            cur = conn.cursor()
            try:
                cur.execute("ALTER TABLE Users ADD is_admin BIT NOT NULL DEFAULT 0")
                conn.commit()
            except Exception:
                pass
            finally:
                cur.close()
        # Check if any admin exists
        cur = conn.cursor()
        any_admin = False
        try:
            cur.execute("SELECT COUNT(1) FROM Users WHERE is_admin = 1")
            row = cur.fetchone(); any_admin = bool(row and int(row[0]) > 0)
        except Exception:
            any_admin = False
        finally:
            cur.close()

        if any_admin:
            conn.close()
            flash("Admin already configured.", "info")
            return redirect(url_for("admin_dashboard"))

        # Promote current user
        cur = conn.cursor()
        try:
            cur.execute("UPDATE Users SET is_admin = 1 WHERE email = %s", (email,))
            conn.commit()
            flash("Your account has been promoted to admin.", "success")
        except Exception as e:
            print("admin setup error:", e)
            flash("Failed to promote user to admin.", "error")
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print("admin setup outer error:", e)
        flash("Failed to set up admin.", "error")
        return redirect(url_for("home"))

    return redirect(url_for("admin_dashboard"))


@app.post("/admin/users/promote")
@admin_required
def admin_promote_user():
    wants_json = request.is_json or request.accept_mimetypes.best == "application/json"
    payload = request.get_json(silent=True) if request.is_json else None
    email = (payload or {}).get("email") if payload else request.form.get("email")
    email = (email or "").strip().lower()

    def finish(ok: bool, message: str, status: int = 200):
        if wants_json:
            return jsonify({"ok": ok, "message": message}), status
        flash(message, "success" if ok else "error")
        return redirect(url_for("admin_dashboard"))

    if not email:
        return finish(False, "Please choose a user to promote.", 400)

    conn = get_db_connection()
    try:
        cols = {c.lower() for c in table_columns(conn, "Users")}
        if "is_admin" not in cols:
            conn.close()
            return finish(False, "Users table is missing the is_admin column.", 500)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE Users
            SET is_admin = TRUE
            WHERE LOWER(email) = %s AND (is_admin IS NULL OR is_admin = FALSE OR is_admin = 0)
            """,
            (email,),
        )
        if cur.rowcount == 0:
            message = "User not found or already an admin."
            result = finish(False, message, 404)
        else:
            conn.commit()
            message = f"{email} now has admin access."
            result = finish(True, message)
        cur.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.exception("admin_promote_user error: %s", e)
        result = finish(False, "Failed to update admin rights.", 500)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


@app.route("/admin/orders/mark-viewed", methods=["POST"])
@admin_required
def admin_mark_viewed():
    order_id = request.form.get("order_id") or (request.json.get("order_id") if request.is_json else None)
    if not order_id:
        return jsonify({"ok": False, "error": "missing order_id"}), 400
    try:
        oid = int(order_id)
    except Exception:
        oid = order_id

    conn = get_db_connection()
    col = _orders_viewed_column(conn)
    id_col = table_id_column(conn, 'Orders', ("id", "order_id", "OrderID", "orderid"))
    if col:
        cur = conn.cursor()
        try:
            cur.execute(f"UPDATE Orders SET {col} = 1 WHERE {id_col} = %s", (oid,))
            conn.commit()
        except Exception:
            pass
        finally:
            cur.close()
        conn.close()
    else:
        # Session-based mark if no column
        seen = set(session.get("admin_seen_orders", []))
        seen.add(int(oid))
        session["admin_seen_orders"] = list(seen)
        conn.close()

    return jsonify({"ok": True, "order_id": oid})


# Return order details (customer + shipping) for admin popup
@app.get("/admin/orders/<int:oid>/details")
@admin_required
def admin_order_details(oid: int):
    conn = get_db_connection()
    try:
        id_col = table_id_column(conn, 'Orders', ("id", "order_id", "OrderID", "orderid"))
        cols = {c.lower() for c in table_columns(conn, 'Orders')}

        # Base fields
        select_cols = [f"{id_col} AS id"]
        for c in ("user_email", "address", "total_amount", "order_date"):
            if c in cols:
                select_cols.append(c)

        # Optional name/phone columns (best-effort)
        first_name_col = next((c for c in ("first_name", "customer_first_name", "guest_first_name") if c in cols), None)
        last_name_col = next((c for c in ("last_name", "customer_last_name", "guest_last_name") if c in cols), None)
        phone_col = next((c for c in ("phone", "contact_number", "mobile") if c in cols), None)
        for c in (first_name_col, last_name_col, phone_col):
            if c:
                select_cols.append(c)

        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(select_cols)} FROM Orders WHERE {id_col} = %s", (oid,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify(ok=False, error="not_found"), 404

        # Map results to dict
        keys = [col.split(" AS ")[-1] if " AS " in col else col for col in select_cols]
        data = {keys[i]: row[i] if i < len(row) else None for i in range(len(keys))}

        # Prefer names from Users table by email
        if data.get("user_email"):
            fn, ln = get_user_names_by_email(str(data.get("user_email")))
            if fn:
                data["first_name"] = fn
            if ln:
                data["last_name"] = ln
        # Convenience: build a single name field if possible
        try:
            fn = str(data.get("first_name") or "").strip()
            ln = str(data.get("last_name") or "").strip()
            full = (fn + " " + ln).strip()
            if full:
                data["name"] = full
        except Exception:
            pass

        # Optionally attach item count
        try:
            oi_id_col = table_id_column(conn, 'OrderItems', ("order_id", "OrderID", "orderid"))
            cur.execute(f"SELECT COUNT(1), COALESCE(SUM(quantity),0) FROM OrderItems WHERE {oi_id_col} = %s", (oid,))
            r = cur.fetchone()
            if r:
                data["lines"] = int(r[0])
                try:
                    data["items"] = int(r[1])
                except Exception:
                    data["items"] = None
        except Exception:
            pass
        finally:
            try:
                cur.close()
            except Exception:
                pass

        # Fetch ordered items (name, qty, price, image if available)
        items = []
        try:
            oi_cols = {c.lower() for c in table_columns(conn, 'OrderItems')}
            if oi_cols:
                oi_order_fk = None
                for cand in ("order_id", "OrderID", "orderid"):
                    if cand.lower() in oi_cols:
                        oi_order_fk = cand
                        break
                oi_product_fk = None
                for cand in ("product_id", "ProductID", "productid"):
                    if cand.lower() in oi_cols:
                        oi_product_fk = cand
                        break
                qty_col = "quantity" if "quantity" in oi_cols else ("qty" if "qty" in oi_cols else None)
                price_col = "price" if "price" in oi_cols else ("unit_price" if "unit_price" in oi_cols else None)

                if oi_order_fk and oi_product_fk:
                    cur = conn.cursor()
                    select_bits = [oi_product_fk]
                    if qty_col: select_bits.append(qty_col)
                    if price_col: select_bits.append(price_col)
                    cur.execute(f"SELECT {', '.join(select_bits)} FROM OrderItems WHERE {oi_order_fk} = %s", (oid,))
                    raw_items = cur.fetchall() or []
                    cur.close()

                    # Build items basic list
                    for r in raw_items:
                        idx = 0
                        pid = r[idx]; idx += 1
                        qty = int(r[idx]) if qty_col and len(r) > (idx-0) else None; idx = idx + (1 if qty_col else 0)
                        up = None
                        if price_col:
                            try:
                                up = float(r[idx])
                            except Exception:
                                up = None
                        item = {
                            "product_id": int(pid) if isinstance(pid, (int,)) else pid,
                            "quantity": qty if qty is not None else 1,
                            "unit_price": up,
                        }
                        if item["unit_price"] is not None:
                            try:
                                item["line_total"] = float(item["unit_price"]) * float(item["quantity"]) 
                            except Exception:
                                pass
                        items.append(item)

                    # Enrich with product details if Products table is present
                    prod_cols = {c.lower() for c in table_columns(conn, 'Products')}
                    if prod_cols:
                        prod_id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
                        name_col = "name" if "name" in prod_cols else ("product_name" if "product_name" in prod_cols else None)
                        image_col = "image" if "image" in prod_cols else None
                        price_col_p = "price" if "price" in prod_cols else None
                        if name_col or image_col or price_col_p:
                            # Map by id
                            by_id = {}
                            for it in items:
                                by_id.setdefault(it["product_id"], it)
                            cur = conn.cursor()
                            for pid in list(by_id.keys()):
                                try:
                                    cols_select = [prod_id_col]
                                    if name_col: cols_select.append(name_col)
                                    if image_col: cols_select.append(image_col)
                                    if price_col_p: cols_select.append(price_col_p)
                                    cur.execute(f"SELECT {', '.join(cols_select)} FROM Products WHERE {prod_id_col} = %s", (pid,))
                                    pr = cur.fetchone()
                                    if not pr: 
                                        continue
                                    idx = 0
                                    _pid = pr[idx]; idx += 1
                                    it = by_id.get(pid)
                                    if name_col and len(pr) >= idx+0:
                                        it["name"] = pr[idx]; idx += 1
                                    if image_col and len(pr) >= idx+0:
                                        it["image"] = pr[idx]; idx += 1
                                    if price_col_p and len(pr) >= idx+0 and it.get("unit_price") is None:
                                        try:
                                            it["unit_price"] = float(pr[idx])
                                        except Exception:
                                            pass
                                except Exception:
                                    continue
                            cur.close()
        except Exception:
            pass

        data["items"] = items

        try:
            conn.close()
        except Exception:
            pass

        return jsonify(ok=True, order=data)
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify(ok=False, error="server_error"), 500


# --------------------------- Admin: Products Management ---------------------------
@app.get('/admin/products/list')
@admin_required
def admin_products_list():
    conn = get_db_connection()
    try:
        cols = {c.lower() for c in table_columns(conn, 'Products')}
        id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
        select_cols = [f"{id_col} AS id"]
        has_name = 'name' in cols; has_price = 'price' in cols; has_image = 'image' in cols
        has_desc = any(c in cols for c in ('description','desc','details'))
        has_qty = any(c in cols for c in ('quantity','qty','stock','in_stock'))
        desc_col = next((c for c in ('description','desc','details') if c in cols), None)
        qty_col = next((c for c in ('quantity','qty','stock','in_stock') if c in cols), None)
        if has_name: select_cols.append('name')
        if has_price: select_cols.append('price')
        if has_image: select_cols.append('image')
        if desc_col: select_cols.append(desc_col)
        if qty_col: select_cols.append(qty_col)
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(select_cols)} FROM Products ORDER BY {id_col} DESC")
        items = []
        for row in cur.fetchall():
            idx = 0
            rid = row[idx]; idx += 1
            try:
                rid = int(rid)
            except Exception:
                pass
            obj = { 'id': rid }
            if has_name:
                obj['name'] = row[idx]; idx += 1
            if has_price:
                try:
                    obj['price'] = float(row[idx]) if row[idx] is not None else None
                except Exception:
                    obj['price'] = None
                idx += 1
            if has_image:
                obj['image'] = row[idx]; idx += 1
            if desc_col:
                obj['description'] = row[idx]; idx += 1
            if qty_col:
                try:
                    obj['quantity'] = int(row[idx]) if row[idx] is not None else None
                except Exception:
                    obj['quantity'] = None
                idx += 1
            items.append(obj)
        cur.close(); conn.close()
        return jsonify(ok=True, items=items, flags={
            'has_name': has_name,
            'has_price': has_price,
            'has_image': has_image,
            'has_description': bool(desc_col),
            'has_quantity': bool(qty_col),
        })
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        print('admin_products_list error:', e)
        return jsonify(ok=False, error='server_error'), 500


@app.route('/admin/products/manage')
@admin_required
def admin_products_manage():
    conn = get_db_connection()
    try:
        cols = {c.lower() for c in table_columns(conn, 'Products')}
        id_col = table_id_column(conn, 'Products', ("id", "product_id", "ProductID", "productid"))
        select_cols = [f"{id_col} AS id"]
        has_name = 'name' in cols; has_price = 'price' in cols; has_image = 'image' in cols
        desc_col = next((c for c in ('description','desc','details') if c in cols), None)
        qty_col = next((c for c in ('quantity','qty','stock','in_stock') if c in cols), None)
        if has_name: select_cols.append('name')
        if has_price: select_cols.append('price')
        if has_image: select_cols.append('image')
        if desc_col: select_cols.append(desc_col)
        if qty_col: select_cols.append(qty_col)
        cur = conn.cursor(); cur.execute(f"SELECT {', '.join(select_cols)} FROM Products ORDER BY {id_col} DESC")
        items = []
        for row in cur.fetchall():
            idx = 0
            rid = row[idx]; idx += 1
            try: rid = int(rid)
            except Exception: pass
            obj = {'id': rid}
            if has_name: obj['name'] = row[idx]; idx += 1
            if has_price:
                try: obj['price'] = float(row[idx]) if row[idx] is not None else None
                except Exception: obj['price'] = None
                idx += 1
            if has_image: obj['image'] = row[idx]; idx += 1
            if desc_col: obj['description'] = row[idx]; idx += 1
            if qty_col:
                try: obj['quantity'] = int(row[idx]) if row[idx] is not None else None
                except Exception: obj['quantity'] = None
                idx += 1
            items.append(obj)
        cur.close(); conn.close()
        return render_template('admin_products.html', products=items, has_image=has_image, has_description=bool(desc_col), has_quantity=bool(qty_col))
    except Exception as e:
        try: conn.close()
        except Exception: pass
        print('admin_products_manage error:', e)
        flash('Failed to load products.', 'error')
        return redirect(url_for('admin_dashboard'))


if __name__ == "__main__":
    app.run(debug=True)

# --- Admin: mark order as completed (removed from active list, kept in history) ---
try:
    app  # type: ignore[name-defined]
    admin_required  # type: ignore[name-defined]
except NameError:
    app = None  # type: ignore[assignment]

if app is not None:
    @app.post('/admin/mark-completed')
    @admin_required
    def admin_mark_completed():  # type: ignore[func-returns-value]
        data = request.get_json(silent=True) or {}
        try:
            order_id = int(data.get('order_id', 0))
        except Exception:
            return jsonify(ok=False, error='invalid'), 400
        if order_id <= 0:
            return jsonify(ok=False, error='missing'), 400
        completed = set(session.get('admin_completed_orders', []))
        completed.add(order_id)
        session['admin_completed_orders'] = list(completed)
        session.modified = True
        return jsonify(ok=True)
