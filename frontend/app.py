import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "visionguard-dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)


ALERT_API_KEY = os.getenv("VISIONGUARD_API_KEY", "")
SMTP_HOST = os.getenv("VISIONGUARD_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("VISIONGUARD_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("VISIONGUARD_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("VISIONGUARD_SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("VISIONGUARD_SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_USE_TLS = os.getenv("VISIONGUARD_SMTP_USE_TLS", "true").lower() == "true"
OBSTRUCTION_EMAIL_COOLDOWN_SECONDS = int(
    os.getenv("VISIONGUARD_OBSTRUCTION_EMAIL_COOLDOWN_SECONDS", "900")
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    notification_email = db.Column(db.String(255), nullable=True)
    email_notifications_enabled = db.Column(db.Boolean, nullable=False, default=False)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "message": self.message,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat(),
        }


class Alert(db.Model):
    __tablename__ = "alerts"

    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.String(255), nullable=False)
    camera_source = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    email_sent = db.Column(db.Boolean, default=False, nullable=False)


class Camera(db.Model):
    __tablename__ = "camera"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, autoincrement=True)
    location = db.Column(db.String(100), nullable=False)
    area = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="Active")


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _bootstrap_schema():
    db.create_all()
    inspector = inspect(db.engine)
    user_columns = {column["name"] for column in inspector.get_columns("user")}

    with db.engine.begin() as connection:
        if "notification_email" not in user_columns:
            connection.execute(text("ALTER TABLE user ADD COLUMN notification_email VARCHAR(255)"))
        if "email_notifications_enabled" not in user_columns:
            connection.execute(
                text(
                    "ALTER TABLE user ADD COLUMN email_notifications_enabled "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )


def _is_alert_request_authorized() -> bool:
    if not ALERT_API_KEY:
        return True

    auth_header = request.headers.get("Authorization", "")
    expected_header = f"Bearer {ALERT_API_KEY}"
    return auth_header == expected_header


def _normalize_alert_type(alert_type: str) -> str:
    if alert_type == "darkness":
        return "obstruction"
    return alert_type


def _send_email(subject: str, body: str, recipients: list[str]) -> bool:
    if not recipients:
        return False
    if not SMTP_HOST or not SMTP_FROM_EMAIL:
        app.logger.warning("Email skipped because SMTP is not configured.")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True
    except Exception as exc:
        app.logger.warning("Email send failed: %s", exc)
        return False


def _create_notification_records(message: str):
    recipients = User.query.filter_by(email_notifications_enabled=True).all()
    for user in recipients:
        db.session.add(Notification(user_id=user.id, message=message))
    return recipients


def _should_send_obstruction_email(camera_source: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(seconds=OBSTRUCTION_EMAIL_COOLDOWN_SECONDS)
    recent_emailed_alert = (
        Alert.query.filter(
            Alert.alert_type == "obstruction",
            Alert.camera_source == camera_source,
            Alert.email_sent.is_(True),
            Alert.created_at >= cutoff,
        )
        .order_by(Alert.created_at.desc())
        .first()
    )
    return recent_emailed_alert is None


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Invalid username or password")

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        hashed_password = generate_password_hash(password, method="pbkdf2:sha256")
        new_user = User(username=username, password=hashed_password)

        db.session.add(new_user)
        db.session.commit()

        flash("Account created! Please log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@app.route("/reports")
@login_required
def reports():
    return render_template("reports.html", active="reports")


@app.route("/cameras", methods=["GET"])
@login_required
def cameras():
    all_cameras = Camera.query.all()
    return render_template("cameras.html", cameras=all_cameras)


@app.route("/add_camera", methods=["POST"])
@login_required
def add_camera():
    location = request.form["location"]
    area = request.form["area"]
    name = request.form["name"]
    type_ = request.form["type"]

    max_number = db.session.query(db.func.max(Camera.number)).scalar() or 0
    next_number = max_number + 1

    new_camera = Camera(
        number=next_number,
        location=location,
        area=area,
        name=name,
        type=type_,
        status="Active",
    )
    db.session.add(new_camera)
    db.session.commit()

    notification_msg = f"New camera added: {name} at {location}, {area}"
    db.session.add(Notification(user_id=current_user.id, message=notification_msg))
    db.session.commit()

    flash("Camera added successfully!", "success")
    return redirect(url_for("cameras"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_user.notification_email = request.form.get("notification_email", "").strip() or None
        current_user.email_notifications_enabled = (
            request.form.get("email_notifications_enabled") == "on"
        )
        db.session.commit()
        flash("Notification settings saved.", "success")
        return redirect(url_for("settings"))

    return render_template("settings.html", active="settings")


@app.route("/locations")
@login_required
def locations():
    return render_template("locations.html", active="locations")


@app.route("/areas")
@login_required
def areas():
    return render_template("areas.html", active="areas")


@app.route("/feed")
@login_required
def feed():
    return render_template("feed.html", active="feed")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.")
    return redirect(url_for("login"))


@app.route("/api/notifications")
@login_required
def get_notifications():
    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return jsonify([notification.to_dict() for notification in notifications])


@app.route("/alerts", methods=["POST"])
def receive_alert():
    if not _is_alert_request_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    alert_type = _normalize_alert_type((payload.get("alert_type") or "").strip())
    detail = (payload.get("detail") or "").strip()
    camera_source = str(payload.get("camera_source") or "").strip()

    if not alert_type or not detail or not camera_source:
        return jsonify({"error": "Missing alert_type, detail, or camera_source"}), 400

    notification_message = f"{alert_type.title()} alert for camera {camera_source}: {detail}"
    recipients = _create_notification_records(notification_message)

    alert = Alert(
        alert_type=alert_type,
        detail=detail,
        camera_source=camera_source,
        email_sent=False,
    )

    should_email = alert_type == "obstruction" and _should_send_obstruction_email(camera_source)
    email_recipients = [
        user.notification_email
        for user in recipients
        if user.notification_email and user.email_notifications_enabled
    ]

    if should_email and email_recipients:
        subject = f"VisionGuard obstruction detected on camera {camera_source}"
        body = (
            "VisionGuard detected a possible camera obstruction.\n\n"
            f"Camera: {camera_source}\n"
            f"Detail: {detail}\n"
            f"Time (UTC): {datetime.utcnow().isoformat()}Z\n"
        )
        alert.email_sent = _send_email(subject, body, email_recipients)

    db.session.add(alert)
    db.session.commit()

    return jsonify(
        {
            "success": True,
            "alert_type": alert_type,
            "email_sent": alert.email_sent,
        }
    )


with app.app_context():
    _bootstrap_schema()


if __name__ == "__main__":
    app.run(debug=True)
