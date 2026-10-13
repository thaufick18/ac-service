import csv
import io
import re
import secrets
from contextlib import contextmanager
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from getpass import getpass

import click
import mysql.connector
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from mysql.connector import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config

app = Flask(__name__)
app.config.from_object(Config)
if not app.config.get("SECRET_KEY"):
    raise RuntimeError("Set a long random SECRET_KEY in your environment or .env file.")
csrf = CSRFProtect(app)

STATUSES = (
    "Pending",
    "Assigned",
    "Technician On the Way",
    "In Progress",
    "Completed",
    "Cancelled",
)
AC_TYPES = ("Split AC", "Window AC", "Central AC")
PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,30}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ResultCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    @staticmethod
    def normalize_row(row):
        if row:
            for key, value in row.items():
                if isinstance(value, timedelta):
                    seconds = int(value.total_seconds()) % 86400
                    row[key] = time(seconds // 3600, seconds % 3600 // 60, seconds % 60, value.microseconds)
        return row

    def fetchone(self):
        return self.normalize_row(self._cursor.fetchone())

    def fetchall(self):
        return [self.normalize_row(row) for row in self._cursor.fetchall()]


@app.before_request
def load_session_user():
    user_id = session.get("user_id")
    if not user_id:
        return
    with database() as (_, cursor):
        cursor.execute(
            "SELECT id, name, email, phone, address, role, active FROM users WHERE id = %s",
            (user_id,),
        )
        user = cursor.fetchone()
    if not user or not user["active"]:
        session.clear()
        g.user_invalid = True
        flash("Your account is unavailable. Please contact support.", "danger")
        return
    g.user = user


@contextmanager
def database():
    connection = mysql.connector.connect(**app.config["MYSQL_CONFIG"])
    cursor = ResultCursor(connection.cursor(dictionary=True))
    try:
        yield connection, cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def valid_email(value):
    return bool(EMAIL_RE.fullmatch(value or "")) and len(value) <= 254


def valid_phone(value):
    return bool(PHONE_RE.fullmatch(value or ""))


def parse_money(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        return None
    if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99"):
        return None
    return amount.quantize(Decimal("0.01"))


def customer_or_404(request_id, customer_id):
    with database() as (_, cursor):
        cursor.execute(
            "SELECT * FROM service_requests WHERE id = %s AND customer_id = %s",
            (request_id, customer_id),
        )
        result = cursor.fetchone()
    if not result:
        abort(404)
    return result


def login_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user_id = session.get("user_id")
            user = getattr(g, "user", None)
            if not user_id or not user:
                if not getattr(g, "user_invalid", False):
                    flash("Please sign in to continue.", "warning")
                return redirect(url_for("login", next=request.path))
            if not user["active"]:
                session.clear()
                flash("Your account is unavailable. Please contact support.", "danger")
                return redirect(url_for("login"))
            if roles and user["role"] not in roles:
                abort(403)
            g.user = user
            return view(*args, **kwargs)

        return wrapped

    return decorate


def role_home(role):
    return {
        "customer": "customer_dashboard",
        "technician": "technician_dashboard",
        "admin": "admin_dashboard",
    }.get(role, "index")


def sync_payment(cursor, request_id, amount):
    cursor.execute(
        """INSERT INTO payments (service_request_id, amount, status)
           VALUES (%s, %s, 'Pending')
           ON DUPLICATE KEY UPDATE amount = VALUES(amount)""",
        (request_id, amount),
    )


@app.context_processor
def common_template_data():
    return {
        "current_user": getattr(g, "user", None),
        "service_statuses": STATUSES,
        "role_home": role_home,
        "now_year": date.today().year,
        "today": date.today().isoformat(),
    }


@app.route("/")
def index():
    with database() as (_, cursor):
        cursor.execute(
            "SELECT id, service_name, description, estimated_price FROM services WHERE active = 1 ORDER BY id"
        )
        services = cursor.fetchall()
    return render_template("index.html", services=services)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "")
        errors = []
        if not name or len(name) > 120:
            errors.append("Enter your name (up to 120 characters).")
        if not valid_email(email):
            errors.append("Enter a valid email address.")
        if not valid_phone(phone):
            errors.append("Enter a valid phone number.")
        if not address or len(address) > 500:
            errors.append("Enter your service address (up to 500 characters).")
        if len(password) < 10 or len(password) > 128:
            errors.append("Your password must be between 10 and 128 characters.")
        if errors:
            for message in errors:
                flash(message, "danger")
            return render_template("register.html")
        try:
            with database() as (_, cursor):
                cursor.execute(
                    "INSERT INTO users (name, email, phone, address, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'customer')",
                    (name, email, phone, address, generate_password_hash(password)),
                )
        except IntegrityError:
            flash("An account with that email already exists.", "danger")
            return render_template("register.html")
        flash("Your account is ready. Sign in to book a repair.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with database() as (_, cursor):
            cursor.execute(
                "SELECT id, name, email, password_hash, role, active FROM users WHERE email = %s",
                (email,),
            )
            user = cursor.fetchone()
        if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
            flash("Email or password is incorrect.", "danger")
            return render_template("login.html")
        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['name'].split()[0]}.", "success")
        return redirect(url_for(role_home(user["role"])))
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("index"))


@app.route("/book", methods=["GET", "POST"])
@login_required("customer")
def book_service():
    with database() as (_, cursor):
        cursor.execute("SELECT id, service_name, estimated_price FROM services WHERE active = 1 ORDER BY id")
        services = cursor.fetchall()
        cursor.execute("SELECT name, phone, address FROM users WHERE id = %s", (g.user["id"],))
        profile = cursor.fetchone()
    if request.method == "POST":
        ac_type = request.form.get("ac_type", "")
        ac_brand = request.form.get("ac_brand", "").strip()
        service_address = request.form.get("service_address", "").strip()
        problem = request.form.get("problem_description", "").strip()
        preferred_date = request.form.get("preferred_date", "")
        preferred_time = request.form.get("preferred_time", "")
        service_id = request.form.get("service_id", "")
        errors = []
        if ac_type not in AC_TYPES:
            errors.append("Choose a valid AC type.")
        if not ac_brand or len(ac_brand) > 100:
            errors.append("Enter the AC brand (up to 100 characters).")
        if not service_address or len(service_address) > 500:
            errors.append("Enter the service address (up to 500 characters).")
        if not problem or len(problem) > 3000:
            errors.append("Describe the problem (up to 3,000 characters).")
        try:
            appointment_date = date.fromisoformat(preferred_date)
            if appointment_date < date.today():
                errors.append("Choose a date that is today or later.")
        except ValueError:
            errors.append("Choose a valid preferred date.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", preferred_time):
            errors.append("Choose a valid preferred time.")
        service = next((item for item in services if str(item["id"]) == service_id), None)
        if not service:
            errors.append("Choose an available service.")
        if errors:
            for message in errors:
                flash(message, "danger")
            return render_template("book_service.html", services=services, profile=profile)
        request_code = f"AC-{secrets.token_hex(5).upper()}"
        with database() as (_, cursor):
            cursor.execute(
                """INSERT INTO service_requests
                   (request_code, customer_id, service_id, ac_type, ac_brand, problem_description,
                    service_address, preferred_date, preferred_time, estimated_price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    request_code,
                    g.user["id"],
                    service["id"],
                    ac_type,
                    ac_brand,
                    problem,
                    service_address,
                    preferred_date,
                    preferred_time,
                    service["estimated_price"],
                ),
            )
        return render_template("book_service.html", services=services, profile=profile, confirmation=request_code)
    return render_template("book_service.html", services=services, profile=profile)


@app.route("/track", methods=["GET", "POST"])
@login_required("customer")
def track_service():
    result = None
    code = request.values.get("request_code", "").strip().upper()
    if code:
        with database() as (_, cursor):
            cursor.execute(
                """SELECT r.*, s.service_name, tuser.name AS technician_name,
                          tuser.phone AS technician_phone, t.specialization
                   FROM service_requests r
                   JOIN services s ON s.id = r.service_id
                   LEFT JOIN technicians t ON t.id = r.technician_id
                   LEFT JOIN users tuser ON tuser.id = t.user_id
                   WHERE r.request_code = %s AND r.customer_id = %s""",
                (code, g.user["id"]),
            )
            result = cursor.fetchone()
        if not result:
            flash("No service request was found for that ID on your account.", "warning")
    return render_template("track_service.html", result=result, request_code=code)


@app.route("/my-requests")
@login_required("customer")
def my_requests():
    with database() as (_, cursor):
        cursor.execute(
            """SELECT r.id, r.request_code, s.service_name, r.created_at, r.preferred_date,
                      r.preferred_time, r.status, r.estimated_price, r.final_price
               FROM service_requests r JOIN services s ON s.id = r.service_id
               WHERE r.customer_id = %s ORDER BY r.created_at DESC""",
            (g.user["id"],),
        )
        requests_list = cursor.fetchall()
    return render_template("my_requests.html", requests=requests_list)


@app.route("/requests/<int:request_id>/cancel", methods=["POST"])
@login_required("customer")
def cancel_request(request_id):
    with database() as (_, cursor):
        cursor.execute(
            "UPDATE service_requests SET status = 'Cancelled' WHERE id = %s AND customer_id = %s AND status = 'Pending'",
            (request_id, g.user["id"]),
        )
        changed = cursor.rowcount
    if changed:
        flash("Your pending request was cancelled.", "success")
    else:
        flash("Only your pending requests can be cancelled.", "warning")
    return redirect(url_for("my_requests"))


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        message = request.form.get("message", "").strip()
        errors = []
        if not name or len(name) > 120:
            errors.append("Enter your name.")
        if not valid_email(email):
            errors.append("Enter a valid email address.")
        if not valid_phone(phone):
            errors.append("Enter a valid phone number.")
        if not message or len(message) > 5000:
            errors.append("Enter a message (up to 5,000 characters).")
        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("contact.html")
        with database() as (_, cursor):
            cursor.execute(
                "INSERT INTO contact_messages (name, email, phone, message) VALUES (%s, %s, %s, %s)",
                (name, email, phone, message),
            )
        flash("Thanks for reaching out. Our service team will be in touch.", "success")
        return redirect(url_for("contact"))
    return render_template("contact.html")


@app.route("/customer")
@login_required("customer")
def customer_dashboard():
    with database() as (_, cursor):
        cursor.execute(
            """SELECT r.request_code, s.service_name, r.status, r.preferred_date,
                      r.estimated_price FROM service_requests r
               JOIN services s ON s.id = r.service_id WHERE r.customer_id = %s
               ORDER BY r.created_at DESC LIMIT 5""",
            (g.user["id"],),
        )
        recent_requests = cursor.fetchall()
        cursor.execute(
            "SELECT COUNT(*) AS total, SUM(status = 'Completed') AS completed FROM service_requests WHERE customer_id = %s",
            (g.user["id"],),
        )
        summary = cursor.fetchone()
    return render_template("customer_dashboard.html", recent_requests=recent_requests, summary=summary)


@app.route("/technician")
@login_required("technician")
def technician_dashboard():
    with database() as (_, cursor):
        cursor.execute("SELECT id, specialization, availability FROM technicians WHERE user_id = %s", (g.user["id"],))
        technician = cursor.fetchone()
        if not technician:
            abort(403)
        cursor.execute(
            """SELECT r.*, s.service_name, c.name AS customer_name, c.phone AS customer_phone
               FROM service_requests r JOIN services s ON s.id = r.service_id
               JOIN users c ON c.id = r.customer_id
               WHERE r.technician_id = %s AND r.status <> 'Completed' AND r.status <> 'Cancelled'
               ORDER BY r.preferred_date, r.preferred_time""",
            (technician["id"],),
        )
        jobs = cursor.fetchall()
        cursor.execute(
            """SELECT r.request_code, s.service_name, c.name AS customer_name, r.preferred_date,
                      r.final_price, r.updated_at FROM service_requests r
               JOIN services s ON s.id = r.service_id JOIN users c ON c.id = r.customer_id
               WHERE r.technician_id = %s AND r.status = 'Completed' ORDER BY r.updated_at DESC LIMIT 10""",
            (technician["id"],),
        )
        history = cursor.fetchall()
    return render_template("technician_dashboard.html", technician=technician, jobs=jobs, history=history)


@app.route("/technician/jobs/<int:request_id>/update", methods=["POST"])
@login_required("technician")
def update_technician_job(request_id):
    target_status = request.form.get("status", "")
    notes = request.form.get("repair_notes", "").strip()
    final_price_raw = request.form.get("final_price", "").strip()
    transitions = {
        "Assigned": {"Technician On the Way"},
        "Technician On the Way": {"In Progress"},
        "In Progress": {"Completed"},
    }
    with database() as (_, cursor):
        cursor.execute("SELECT id FROM technicians WHERE user_id = %s", (g.user["id"],))
        technician = cursor.fetchone()
        if not technician:
            abort(403)
        cursor.execute(
            "SELECT status FROM service_requests WHERE id = %s AND technician_id = %s",
            (request_id, technician["id"]),
        )
        job = cursor.fetchone()
        if not job:
            abort(404)
        if target_status not in transitions.get(job["status"], set()):
            flash("That job status transition is not allowed.", "danger")
            return redirect(url_for("technician_dashboard"))
        if len(notes) > 5000:
            flash("Repair notes must be 5,000 characters or fewer.", "danger")
            return redirect(url_for("technician_dashboard"))
        final_price = None
        if target_status == "Completed":
            final_price = parse_money(final_price_raw)
            if final_price is None:
                flash("Enter a valid final service charge before completing the job.", "danger")
                return redirect(url_for("technician_dashboard"))
        if target_status == "Completed":
            cursor.execute(
                "UPDATE service_requests SET status = %s, repair_notes = %s, final_price = %s WHERE id = %s AND technician_id = %s",
                (target_status, notes, final_price, request_id, technician["id"]),
            )
            sync_payment(cursor, request_id, final_price)
            cursor.execute(
                """UPDATE technicians SET availability = 'available' WHERE id = %s
                   AND NOT EXISTS (SELECT 1 FROM service_requests
                     WHERE technician_id = %s AND status NOT IN ('Completed', 'Cancelled'))""",
                (technician["id"], technician["id"]),
            )
        else:
            cursor.execute(
                "UPDATE service_requests SET status = %s, repair_notes = %s WHERE id = %s AND technician_id = %s",
                (target_status, notes, request_id, technician["id"]),
            )
    flash("Repair job updated.", "success")
    return redirect(url_for("technician_dashboard"))


@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    with database() as (_, cursor):
        cursor.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'customer' AND active = 1")
        customers = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'technician' AND active = 1")
        technicians_count = cursor.fetchone()["total"]
        cursor.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(status = 'Pending'), 0) AS pending,
                      COALESCE(SUM(status = 'In Progress'), 0) AS in_progress,
                      COALESCE(SUM(status = 'Completed'), 0) AS completed,
                      COALESCE(SUM(status = 'Cancelled'), 0) AS cancelled
               FROM service_requests"""
        )
        request_stats = cursor.fetchone()
        cursor.execute("SELECT COALESCE(SUM(amount), 0) AS revenue FROM payments WHERE status = 'Received'")
        revenue = cursor.fetchone()["revenue"]
        cursor.execute(
            """SELECT r.request_code, r.status, r.created_at, r.estimated_price,
                      s.service_name, u.name AS customer_name
               FROM service_requests r JOIN services s ON s.id = r.service_id
               JOIN users u ON u.id = r.customer_id ORDER BY r.created_at DESC LIMIT 8"""
        )
        recent_requests = cursor.fetchall()
        cursor.execute("SELECT id, name, email, phone, created_at FROM users WHERE role = 'customer' ORDER BY created_at DESC LIMIT 8")
        recent_customers = cursor.fetchall()
        cursor.execute("SELECT id, name, email, phone, message, created_at FROM contact_messages ORDER BY created_at DESC LIMIT 5")
        messages = cursor.fetchall()
    return render_template(
        "admin_dashboard.html",
        customers=customers,
        technicians_count=technicians_count,
        request_stats=request_stats,
        revenue=revenue,
        recent_requests=recent_requests,
        recent_customers=recent_customers,
        messages=messages,
    )


@app.route("/admin/requests", methods=["GET", "POST"])
@login_required("admin")
def manage_requests():
    if request.method == "POST":
        request_id = request.form.get("request_id", type=int)
        action = request.form.get("action")
        if not request_id:
            abort(400)
        with database() as (_, cursor):
            cursor.execute("SELECT id, technician_id, status, final_price FROM service_requests WHERE id = %s", (request_id,))
            record = cursor.fetchone()
            if not record:
                abort(404)
            if action == "assign":
                technician_id = request.form.get("technician_id", type=int)
                cursor.execute(
                    "SELECT t.id FROM technicians t JOIN users u ON u.id = t.user_id WHERE t.id = %s AND u.active = 1 AND t.availability <> 'unavailable'",
                    (technician_id,),
                )
                if not cursor.fetchone():
                    flash("Choose an active technician.", "danger")
                elif record["status"] in ("Completed", "Cancelled"):
                    flash("Closed requests cannot be assigned.", "warning")
                else:
                    if record["technician_id"] and record["technician_id"] != technician_id:
                        cursor.execute(
                            """UPDATE technicians SET availability = 'available' WHERE id = %s
                               AND NOT EXISTS (SELECT 1 FROM service_requests
                                 WHERE technician_id = %s AND id <> %s
                                 AND status NOT IN ('Completed', 'Cancelled'))""",
                            (record["technician_id"], record["technician_id"], request_id),
                        )
                    cursor.execute(
                        "UPDATE service_requests SET technician_id = %s, status = 'Assigned' WHERE id = %s",
                        (technician_id, request_id),
                    )
                    cursor.execute("UPDATE technicians SET availability = 'busy' WHERE id = %s", (technician_id,))
                    flash("Technician assigned to the request.", "success")
            elif action == "update":
                status = request.form.get("status", "")
                estimated_price = parse_money(request.form.get("estimated_price", ""))
                final_raw = request.form.get("final_price", "").strip()
                final_price = parse_money(final_raw) if final_raw else None
                if (
                    status not in STATUSES
                    or estimated_price is None
                    or (final_raw and final_price is None)
                    or (status == "Completed" and final_price is None and record["final_price"] is None)
                ):
                    flash("Check the status and price values.", "danger")
                else:
                    cursor.execute(
                        "UPDATE service_requests SET status = %s, estimated_price = %s, final_price = COALESCE(%s, final_price) WHERE id = %s",
                        (status, estimated_price, final_price, request_id),
                    )
                    effective_final_price = final_price if final_price is not None else record["final_price"]
                    if status == "Completed" and effective_final_price is not None:
                        sync_payment(cursor, request_id, effective_final_price)
                    if status in ("Completed", "Cancelled") and record["technician_id"]:
                        cursor.execute(
                            """UPDATE technicians SET availability = 'available' WHERE id = %s
                               AND NOT EXISTS (SELECT 1 FROM service_requests
                                 WHERE technician_id = %s AND status NOT IN ('Completed', 'Cancelled'))""",
                            (record["technician_id"], record["technician_id"]),
                        )
                    flash("Request details updated.", "success")
            elif action == "payment":
                cursor.execute("SELECT final_price, status FROM service_requests WHERE id = %s", (request_id,))
                payment_request = cursor.fetchone()
                payment_status = request.form.get("payment_status")
                if payment_request["final_price"] is None or payment_request["status"] != "Completed" or payment_status not in ("Received", "Pending", "Refunded"):
                    flash("A payment can only be recorded for a completed request with a final charge.", "warning")
                else:
                    cursor.execute(
                        """INSERT INTO payments (service_request_id, amount, status, recorded_at)
                           VALUES (%s, %s, %s, IF(%s = 'Received', CURRENT_TIMESTAMP, NULL))
                           ON DUPLICATE KEY UPDATE amount = VALUES(amount), status = VALUES(status),
                           recorded_at = IF(VALUES(status) = 'Received', CURRENT_TIMESTAMP, NULL)""",
                        (request_id, payment_request["final_price"], payment_status, payment_status),
                    )
                    flash("Payment record updated.", "success")
            else:
                abort(400)
        return redirect(url_for("manage_requests", status=request.args.get("status", ""), q=request.args.get("q", "")))

    if request.args.get("report") == "1":
        return redirect(url_for("service_report"))
    status_filter = request.args.get("status", "")
    search = request.args.get("q", "").strip()[:100]
    clauses = []
    parameters = []
    if status_filter in STATUSES:
        clauses.append("r.status = %s")
        parameters.append(status_filter)
    if search:
        clauses.append("(r.request_code LIKE %s OR cu.name LIKE %s OR cu.email LIKE %s OR s.service_name LIKE %s)")
        parameters.extend([f"%{search}%"] * 4)
    where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
    with database() as (_, cursor):
        cursor.execute(
                    """SELECT r.*, s.service_name, cu.name AS customer_name, cu.email AS customer_email,
                              cu.address AS customer_address,
                      tu.name AS technician_name, p.status AS payment_status
               FROM service_requests r JOIN services s ON s.id = r.service_id
               JOIN users cu ON cu.id = r.customer_id
               LEFT JOIN technicians t ON t.id = r.technician_id
               LEFT JOIN users tu ON tu.id = t.user_id
               LEFT JOIN payments p ON p.service_request_id = r.id""" + where_clause + " ORDER BY r.created_at DESC",
            tuple(parameters),
        )
        requests_list = cursor.fetchall()
        cursor.execute(
            "SELECT t.id, u.name, t.specialization FROM technicians t JOIN users u ON u.id = t.user_id WHERE u.active = 1 ORDER BY u.name"
        )
        technicians = cursor.fetchall()
    return render_template("manage_requests.html", requests=requests_list, technicians=technicians, statuses=STATUSES, selected_status=status_filter, search=search)


@app.route("/admin/report.csv")
@login_required("admin")
def service_report():
    with database() as (_, cursor):
        cursor.execute(
            """SELECT r.request_code, c.name AS customer_name, c.email, s.service_name,
                      r.status, r.created_at, r.preferred_date, r.estimated_price,
                      r.final_price, t.name AS technician_name, p.status AS payment_status,
                      p.amount AS payment_amount
               FROM service_requests r JOIN users c ON c.id = r.customer_id
               JOIN services s ON s.id = r.service_id
               LEFT JOIN technicians tech ON tech.id = r.technician_id
               LEFT JOIN users t ON t.id = tech.user_id
               LEFT JOIN payments p ON p.service_request_id = r.id
               ORDER BY r.created_at DESC"""
        )
        report_rows = cursor.fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Service ID", "Customer", "Customer email", "Service", "Status", "Booked", "Appointment", "Estimate", "Final charge", "Technician", "Payment status", "Payment amount"))
    for item in report_rows:
        values = [
            item["request_code"],
            item["customer_name"],
            item["email"],
            item["service_name"],
            item["status"],
            item["created_at"],
            item["preferred_date"],
            item["estimated_price"],
            item["final_price"],
            item["technician_name"],
            item["payment_status"],
            item["payment_amount"],
        ]
        writer.writerow([csv_safe(value) for value in values])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=coolcare-service-report.csv",
            "X-Content-Type-Options": "nosniff",
        },
    )


def csv_safe(value):
    if value is None:
        return ""
    text = str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


@app.route("/admin/technicians", methods=["GET", "POST"])
@login_required("admin")
def manage_technicians():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            specialization = request.form.get("specialization", "").strip()
            password = request.form.get("password", "")
            if not name or len(name) > 120 or not valid_email(email) or not valid_phone(phone) or not address or len(address) > 500 or not specialization or len(specialization) > 160 or len(password) < 12 or len(password) > 128:
                flash("Provide valid profile details and a password of at least 12 characters.", "danger")
            else:
                try:
                    with database() as (_, cursor):
                        cursor.execute(
                            "SELECT id FROM technicians WHERE id = %s AND user_id = %s",
                            (technician_id, user_id),
                        )
                        if not cursor.fetchone():
                            abort(404)
                        cursor.execute(
                            "INSERT INTO users (name, email, phone, address, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'technician')",
                            (name, email, phone, address, generate_password_hash(password)),
                        )
                        user_id = cursor.lastrowid
                        cursor.execute(
                            "INSERT INTO technicians (user_id, specialization) VALUES (%s, %s)",
                            (user_id, specialization),
                        )
                    flash("Technician account created.", "success")
                except IntegrityError:
                    flash("That email address is already in use.", "danger")
        elif action == "update":
            technician_id = request.form.get("technician_id", type=int)
            user_id = request.form.get("user_id", type=int)
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            specialization = request.form.get("specialization", "").strip()
            availability = request.form.get("availability", "")
            active = request.form.get("active") == "1"
            if not technician_id or not user_id or not name or len(name) > 120 or not valid_email(email) or not valid_phone(phone) or not address or len(address) > 500 or not specialization or len(specialization) > 160 or availability not in ("available", "busy", "unavailable"):
                flash("Check technician details before saving.", "danger")
            else:
                try:
                    with database() as (_, cursor):
                        cursor.execute(
                            "UPDATE users SET name = %s, email = %s, phone = %s, address = %s, active = %s WHERE id = %s AND role = 'technician'",
                            (name, email, phone, address, active, user_id),
                        )
                        cursor.execute(
                            "UPDATE technicians SET specialization = %s, availability = %s WHERE id = %s AND user_id = %s",
                            (specialization, availability, technician_id, user_id),
                        )
                    flash("Technician profile updated.", "success")
                except IntegrityError:
                    flash("That email address is already in use.", "danger")
        else:
            abort(400)
        return redirect(url_for("manage_technicians"))
    with database() as (_, cursor):
        cursor.execute(
            """SELECT t.id, t.user_id, t.specialization, t.availability, u.name, u.email,
                      u.phone, u.address, u.active, u.created_at,
                      (SELECT COUNT(*) FROM service_requests r WHERE r.technician_id = t.id AND r.status NOT IN ('Completed', 'Cancelled')) AS open_jobs
               FROM technicians t JOIN users u ON u.id = t.user_id ORDER BY u.name"""
        )
        technicians = cursor.fetchall()
    return render_template("manage_technicians.html", technicians=technicians)


@app.route("/admin/services", methods=["GET", "POST"])
@login_required("admin")
def manage_services():
    if request.method == "POST":
        action = request.form.get("action")
        name = request.form.get("service_name", "").strip()
        description = request.form.get("description", "").strip()
        price = parse_money(request.form.get("estimated_price", ""))
        active = request.form.get("active") == "1"
        service_id = request.form.get("service_id", type=int)
        if not name or len(name) > 100 or not description or len(description) > 500 or price is None:
            flash("Enter a service name, description and valid non-negative price.", "danger")
        else:
            try:
                with database() as (_, cursor):
                    if action == "create":
                        cursor.execute(
                            "INSERT INTO services (service_name, description, estimated_price, active) VALUES (%s, %s, %s, %s)",
                            (name, description, price, active),
                        )
                    elif action == "update" and service_id:
                        cursor.execute(
                            "UPDATE services SET service_name = %s, description = %s, estimated_price = %s, active = %s WHERE id = %s",
                            (name, description, price, active, service_id),
                        )
                    else:
                        abort(400)
                flash("Service catalog updated.", "success")
            except IntegrityError:
                flash("A service with that name already exists.", "danger")
        return redirect(url_for("manage_services"))
    with database() as (_, cursor):
        cursor.execute("SELECT id, service_name, description, estimated_price, active FROM services ORDER BY service_name")
        services = cursor.fetchall()
    return render_template("manage_services.html", services=services)


@app.cli.command("init-admin")
def init_admin():
    """Create the first administrator interactively; never stores a default password."""
    name = click.prompt("Administrator full name")
    email = click.prompt("Administrator email").strip().lower()
    phone = click.prompt("Administrator phone")
    address = click.prompt("Administrator address")
    password = getpass("Administrator password (12+ characters): ")
    if not name.strip() or not valid_email(email) or not valid_phone(phone) or not address.strip() or len(password) < 12:
        raise click.ClickException("Invalid profile or password. No account was created.")
    try:
        with database() as (_, cursor):
            cursor.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'admin'")
            if cursor.fetchone()["total"]:
                raise click.ClickException("An administrator already exists; use the admin dashboard to manage accounts.")
            cursor.execute(
                "INSERT INTO users (name, email, phone, address, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'admin')",
                (name.strip(), email, phone, address.strip(), generate_password_hash(password)),
            )
    except IntegrityError as error:
        raise click.ClickException("That email address is already registered.") from error
    click.echo("Administrator account created.")


@app.cli.command("create-demo-accounts")
def create_demo_accounts():
    """Create local customer and technician accounts with prompted passwords."""
    customer_email = "customer.demo@example.test"
    technician_email = "technician.demo@example.test"
    customer_password = getpass(f"Password for {customer_email} (12+ characters): ")
    technician_password = getpass(f"Password for {technician_email} (12+ characters): ")
    if len(customer_password) < 12 or len(technician_password) < 12:
        raise click.ClickException("Both passwords must be at least 12 characters.")
    with database() as (_, cursor):
        cursor.execute("SELECT id FROM users WHERE email = %s", (customer_email,))
        customer_exists = cursor.fetchone()
        if not customer_exists:
            cursor.execute(
                """INSERT INTO users (name, email, phone, address, password_hash, role)
                   VALUES ('Demo Customer', %s, '+1 800 555 0101', '100 Sample Street', %s, 'customer')""",
                (customer_email, generate_password_hash(customer_password)),
            )
        cursor.execute("SELECT id FROM users WHERE email = %s", (technician_email,))
        technician_user = cursor.fetchone()
        if not technician_user:
            cursor.execute(
                """INSERT INTO users (name, email, phone, address, password_hash, role)
                   VALUES ('Demo Technician', %s, '+1 800 555 0102', '200 Sample Street', %s, 'technician')""",
                (technician_email, generate_password_hash(technician_password)),
            )
            technician_user_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO technicians (user_id, specialization) VALUES (%s, 'Residential air conditioning')",
                (technician_user_id,),
            )
    click.echo(f"Local accounts ready: {customer_email} and {technician_email}.")


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have access to that page."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="We could not find that page or service request."), 404


@app.errorhandler(500)
def internal_error(error):
    app.logger.error("Unhandled application error: %s", error)
    return render_template("error.html", code=500, message="Something went wrong. Please try again."), 500


if __name__ == "__main__":
    app.run()
