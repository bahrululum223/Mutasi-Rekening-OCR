import os
import uuid
import json
import secrets
from authlib.integrations.flask_client import OAuth
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from functools import wraps
from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Flask, render_template, request, send_file, redirect, url_for, flash, session, abort
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from ocr_engine import extract_document
from transaction_parser import parse_transactions
from exporter import export_transactions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
JOB_DIR = os.path.join(BASE_DIR, "jobs")
for folder in (UPLOAD_DIR, OUTPUT_DIR, JOB_DIR):
    os.makedirs(folder, exist_ok=True)

def find_template_dir(base_dir):
    expected = os.path.join(base_dir, "templates")
    if os.path.isfile(os.path.join(expected, "index.html")):
        return expected
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in {".git", "venv", ".venv", "__pycache__", "site-packages", "jobs", "uploads", "output"}]
        if "index.html" in files and os.path.basename(root).lower() == "templates":
            return root
    return expected

app = Flask(__name__, template_folder=find_template_dir(BASE_DIR))
# Render sits behind a trusted reverse proxy; honor forwarded HTTPS when building OAuth callback URLs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "")
if not app.secret_key:
    # Allow local development, but configure SECRET_KEY in hosting environment.
    app.secret_key = "local-dev-only-change-this-secret-key"
app.config.update(
    MAX_CONTENT_LENGTH=30 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=3600,
)
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tif", "tiff"}

# Google OAuth configuration: any Google account with a verified email may sign in.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID or None,
    client_secret=GOOGLE_CLIENT_SECRET or None,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("office_user"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def job_path(job_id):
    if not job_id or not all(ch in "0123456789abcdef" for ch in job_id) or len(job_id) != 32:
        abort(404)
    return os.path.join(JOB_DIR, f"{job_id}.json")

def read_job(job_id):
    path = job_path(job_id)
    if not os.path.isfile(path):
        abort(404)
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    # Jobs belong to the authenticated account that created them.
    if data.get("username") != session.get("office_user"):
        abort(403)
    return data

def save_job(job_id, data):
    with open(job_path(job_id), "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, default=str)

def numeric_value(raw, field_label):
    """Accept common nominal formats without intentional rounding."""
    raw = (raw or "").strip().replace(" ", "")
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(".") > raw.rfind(","):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]) and parts[0].lstrip("-").isdigit():
            raw = "".join(parts)
        elif len(parts) == 2:
            raw = parts[0] + "." + parts[1]
        else:
            raw = "".join(parts)
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        value = Decimal(raw)
        if not value.is_finite():
            raise InvalidOperation
        return value
    except (InvalidOperation, ValueError):
        raise ValueError(f"Nilai {field_label} harus berupa angka yang valid.")


@app.route("/login", methods=["GET"])
def login():
    if session.get("office_user"):
        return redirect(url_for("index"))
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("Google Login belum dikonfigurasi. Hubungi administrator.")
        return render_template("login.html"), 503
    return render_template("login.html")

@app.route("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("Google Login belum dikonfigurasi. Hubungi administrator.")
        return redirect(url_for("login"))
    nonce = secrets.token_urlsafe(32)
    session["google_oauth_nonce"] = nonce
    redirect_uri = GOOGLE_REDIRECT_URI or url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(
        redirect_uri, nonce=nonce, prompt="select_account"
    )

@app.route("/auth/google/callback")
def google_callback():
    try:
        nonce = session.pop("google_oauth_nonce", None)
        if not nonce:
            raise ValueError("Sesi login Google tidak valid atau telah kedaluwarsa.")
        token = oauth.google.authorize_access_token()
        raw_id_token = token.get("id_token")
        if not raw_id_token:
            raise ValueError("Google tidak mengembalikan ID token.")
        claims = google_id_token.verify_oauth2_token(
            raw_id_token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        if claims.get("nonce") != nonce:
            raise ValueError("Validasi nonce Google gagal.")
        email = str(claims.get("email", "")).strip().lower()
        if not email or claims.get("email_verified") is not True:
            raise ValueError("Gunakan akun Google dengan alamat email yang sudah terverifikasi.")
        session.clear()
        session.permanent = True
        session["office_user"] = email
        session["office_email"] = email
        return redirect(url_for("index"))
    except Exception as exc:
        app.logger.warning("Google sign-in failed: %s", exc)
        session.clear()
        flash(str(exc) if isinstance(exc, ValueError) else "Login Google gagal. Silakan coba lagi atau hubungi admin.")
        return redirect(url_for("login"))

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("Anda telah keluar dari akun kantor.")
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    transactions, downloads, method = [], {}, ""
    current_job = session.get("job_id")
    if request.method == "POST" and request.form.get("action") == "save_review":
        if not current_job:
            flash("Belum ada hasil ekstraksi untuk ditinjau. Unggah dokumen terlebih dahulu.")
            return redirect(url_for("index"))
        try:
            data = read_job(current_job)
            original_rows = data.get("transactions", [])
            updated = []
            for i, row in enumerate(original_rows):
                updated.append({
                    "Date": request.form.get(f"date_{i}", "").strip(),
                    "Description": request.form.get(f"description_{i}", "").strip(),
                    "Debit": numeric_value(request.form.get(f"debit_{i}"), "debit"),
                    "Credit": numeric_value(request.form.get(f"credit_{i}"), "kredit"),
                    "Balance": numeric_value(request.form.get(f"balance_{i}"), "saldo"),
                    "Review": request.form.get(f"review_{i}", "Periksa").strip()[:100] or "Periksa",
                })
            data["transactions"] = updated
            save_job(current_job, data)
            summary = {
                "count": len(updated),
                "debit": sum((row["Debit"] or 0) for row in updated),
                "credit": sum((row["Credit"] or 0) for row in updated),
                "closing_balance": next((row["Balance"] for row in reversed(updated) if row["Balance"] is not None), 0),
            }
            export_transactions(updated, OUTPUT_DIR, summary=summary, filename_prefix=current_job)
            flash("Perubahan tersimpan. Hasil Excel dan CSV sudah diperbarui.")
            return redirect(url_for("index") + "#review-section")
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("index") + "#review-section")
    if request.method == "POST":
        uploaded = request.files.get("statement")
        if not uploaded or not uploaded.filename:
            flash("Pilih file mutasi rekening terlebih dahulu.")
            return redirect(url_for("index"))
        if not allowed_file(uploaded.filename):
            flash("Format yang didukung: PDF, JPG, JPEG, PNG, TIFF.")
            return redirect(url_for("index"))
        job_id = uuid.uuid4().hex
        original = secure_filename(uploaded.filename)
        _, ext = os.path.splitext(original)
        path = os.path.join(UPLOAD_DIR, f"{job_id}{ext.lower()}")
        uploaded.save(path)
        try:
            text, method = extract_document(path)
            transactions, summary = parse_transactions(text, filepath=path)
            if not transactions:
                flash("Teks dokumen terbaca, tetapi transaksi belum dikenali. Periksa format dokumen.")
            else:
                for row in transactions:
                    row["Date"] = str(row.get("Date") or "")
                    row["Description"] = str(row.get("Description") or "")
                    # Every newly extracted transaction must start unapproved,
                    # regardless of parser-internal diagnostic status.
                    row["Review"] = "Perlu diperiksa"
                data = {"username": session["office_user"], "transactions": transactions, "method": method, "original_name": original, "created_at": datetime.utcnow().isoformat()}
                save_job(job_id, data)
                session["job_id"] = job_id
                current_job = job_id
                flagged = sum(1 for row in transactions if row.get("Review") not in ("OK", "Approve"))
                flash(f"Berhasil membaca {len(transactions)} baris menggunakan {method}. {flagged} baris perlu ditinjau.")
        except Exception:
            app.logger.exception("Gagal memproses dokumen")
            flash("Gagal memproses dokumen. Periksa log server atau coba file lain.")
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    if current_job:
        path = job_path(current_job)
        if os.path.isfile(path):
            try:
                data = read_job(current_job)
                transactions, method = data.get("transactions", []), data.get("method", "")
                prefix = current_job
                downloads = {"excel": f"{prefix}_mutasi_rekening.xlsx", "csv": f"{prefix}_mutasi_rekening.csv"}
            except (OSError, json.JSONDecodeError):
                pass
    return render_template("index.html", transactions=transactions, downloads=downloads, method=method)

@app.route("/save-row/<int:row_index>", methods=["POST"])
@login_required
def save_row(row_index):
    job_id = session.get("job_id")
    if not job_id:
        flash("Belum ada hasil ekstraksi untuk disimpan.")
        return redirect(url_for("index") + "#review-section")
    try:
        data = read_job(job_id)
        rows = data.get("transactions", [])
        if row_index < 0 or row_index >= len(rows):
            abort(404)
        row = rows[row_index]
        row["Date"] = request.form.get("date", "").strip()
        row["Description"] = request.form.get("description", "").strip()
        row["Debit"] = numeric_value(request.form.get("debit"), "debit")
        row["Credit"] = numeric_value(request.form.get("credit"), "kredit")
        row["Balance"] = numeric_value(request.form.get("balance"), "saldo")
        row.pop("Review", None)
        row["Reviewed"] = request.form.get("reviewed") == "yes"
        # Save-only workflow: remove review status; row remains editable.
        row.pop("Review", None)

        data["transactions"] = rows
        save_job(job_id, data)
        summary = {
            "count": len(rows),
            "debit": sum((Decimal(str(r.get("Debit") or 0)) for r in rows), Decimal("0")),
            "credit": sum((Decimal(str(r.get("Credit") or 0)) for r in rows), Decimal("0")),
            "closing_balance": next((r["Balance"] for r in reversed(rows) if r.get("Balance") is not None), Decimal("0")),
        }
        export_transactions(rows, OUTPUT_DIR, summary=summary, filename_prefix=job_id)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "message": "Tersimpan"}, 200
        flash("Perubahan transaksi berhasil disimpan. Data tetap dapat diedit kembali.")
    except ValueError as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": False, "error": str(exc)}, 400
        flash(str(exc))
    return redirect(url_for("index") + "#review-section")


@app.route("/review", methods=["GET", "POST"])
@login_required
def review():
    # Keep old bookmarks/links working, but show review in the main page.
    return redirect(url_for("index") + "#review-section")

@app.route("/download/<filename>")
@login_required
def download(filename):
    safe = os.path.basename(filename)
    job_id = session.get("job_id")
    if not job_id or not safe.startswith(job_id + "_"):
        flash("Link download tidak valid atau sesi sudah berakhir. Proses dokumen kembali.")
        return redirect(url_for("index"))
    # Confirm the job belongs to the current account before serving its exports.
    read_job(job_id)
    path = os.path.join(OUTPUT_DIR, safe)
    if not os.path.isfile(path):
        data = read_job(job_id)
        rows = data.get("transactions", [])
        summary = {"count": len(rows), "debit": sum((r.get("Debit") or 0) for r in rows), "credit": sum((r.get("Credit") or 0) for r in rows), "closing_balance": next((r.get("Balance") for r in reversed(rows) if r.get("Balance") is not None), 0)}
        export_transactions(rows, OUTPUT_DIR, summary=summary, filename_prefix=job_id)
    return send_file(path, as_attachment=True)

@app.errorhandler(413)
def too_large(_error):
    flash("Ukuran file melebihi batas 30 MB.")
    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
