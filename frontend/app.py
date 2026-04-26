import os
import smtplib
from collections import defaultdict
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
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
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
default_db_path = os.path.join(app.root_path, "instance", "users.db")
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "visionguard-dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", f"sqlite:///{default_db_path}"
)
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
REPORT_TIMEFRAME_OPTIONS = ("day", "week", "month", "year", "5years", "all")


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    notification_email = db.Column(db.String(255), nullable=True)
    email_notifications_enabled = db.Column(db.Boolean, nullable=False, default=False)
    dark_mode_enabled = db.Column(db.Boolean, nullable=False, default=False)
    cameras = db.relationship("Camera", backref="owner", lazy=True)
    camera_groups = db.relationship("CameraGroup", backref="owner", lazy=True)


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
            "created_at": self.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
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
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    location = db.Column(db.String(100), nullable=False)
    area = db.Column(db.String(100), nullable=False, default="")
    name = db.Column(db.String(100), nullable=False)
    rtsp_url = db.Column(db.String(512), nullable=False, default="")
    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="Active")
    group_id = db.Column(db.Integer, db.ForeignKey("camera_group.id"), nullable=True)


class CameraGroup(db.Model):
    __tablename__ = "camera_group"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    cameras = db.relationship("Camera", backref="group", lazy=True)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _bootstrap_schema():
    db.create_all()
    inspector = inspect(db.engine)
    user_columns = {column["name"] for column in inspector.get_columns("user")}
    camera_columns = {column["name"] for column in inspector.get_columns("camera")}
    camera_group_columns = {column["name"] for column in inspector.get_columns("camera_group")}

    def _try_add_column(statement: str):
        try:
            connection.execute(text(statement))
        except OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    with db.engine.begin() as connection:
        if "notification_email" not in user_columns:
            _try_add_column("ALTER TABLE user ADD COLUMN notification_email VARCHAR(255)")
        if "email_notifications_enabled" not in user_columns:
            _try_add_column(
                "ALTER TABLE user ADD COLUMN email_notifications_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
        if "dark_mode_enabled" not in user_columns:
            _try_add_column(
                "ALTER TABLE user ADD COLUMN dark_mode_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
        if "group_id" not in camera_columns:
            _try_add_column("ALTER TABLE camera ADD COLUMN group_id INTEGER")
        if "user_id" not in camera_columns:
            _try_add_column("ALTER TABLE camera ADD COLUMN user_id INTEGER")
        if "rtsp_url" not in camera_columns:
            _try_add_column("ALTER TABLE camera ADD COLUMN rtsp_url VARCHAR(512) NOT NULL DEFAULT ''")
        if "user_id" not in camera_group_columns:
            _try_add_column("ALTER TABLE camera_group ADD COLUMN user_id INTEGER")

        default_user_id = connection.execute(text("SELECT id FROM user ORDER BY id ASC LIMIT 1")).scalar()
        if default_user_id is not None:
            connection.execute(
                text("UPDATE camera SET user_id = :user_id WHERE user_id IS NULL"),
                {"user_id": default_user_id},
            )
            connection.execute(
                text(
                    "UPDATE camera_group "
                    "SET user_id = COALESCE(("
                    "  SELECT camera.user_id FROM camera "
                    "  WHERE camera.group_id = camera_group.id AND camera.user_id IS NOT NULL "
                    "  ORDER BY camera.id ASC LIMIT 1"
                    "), :user_id) "
                    "WHERE user_id IS NULL"
                ),
                {"user_id": default_user_id},
            )


def _user_camera_query():
    return Camera.query.filter_by(user_id=current_user.id)


def _user_camera_group_query():
    return CameraGroup.query.filter_by(user_id=current_user.id)


def _get_current_user_cameras() -> list[Camera]:
    return _user_camera_query().order_by(Camera.location.asc(), Camera.name.asc()).all()


def _get_current_user_camera_groups() -> list[CameraGroup]:
    return _user_camera_group_query().order_by(CameraGroup.name.asc()).all()


def _get_owned_camera_or_404(camera_id: int) -> Camera:
    return _user_camera_query().filter_by(id=camera_id).first_or_404()


def _get_owned_camera_group_or_404(group_id: int) -> CameraGroup:
    return _user_camera_group_query().filter_by(id=group_id).first_or_404()


def _get_owned_camera_group(group_id: str | None) -> CameraGroup | None:
    if not group_id:
        return None
    return _user_camera_group_query().filter_by(id=group_id).first()


def _serialize_camera(camera: Camera) -> dict:
    return {
        "id": f"cam-{camera.id:03d}",
        "name": camera.name,
        "rtsp_url": camera.rtsp_url,
    }


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


def _build_camera_lookup(cameras: list[Camera]) -> dict[str, Camera]:
    lookup = {}
    for camera in cameras:
        lookup[str(camera.id)] = camera
        if camera.number is not None:
            lookup[str(camera.number)] = camera
        lookup[camera.name.strip().lower()] = camera
    return lookup


def _resolve_camera_from_source(camera_source: str, camera_lookup: dict[str, Camera]) -> Camera | None:
    normalized_source = camera_source.strip()
    if not normalized_source:
        return None

    return camera_lookup.get(normalized_source) or camera_lookup.get(normalized_source.lower())


def _format_alert_type(alert_type: str) -> str:
    return alert_type.replace("_", " ").title()


def _is_camera_online(status: str | None) -> bool:
    normalized_status = (status or "").strip().lower()
    return normalized_status in {"active", "online", "healthy"}


def _build_dashboard_data(cameras: list[Camera], alerts: list[Alert]) -> dict:
    now = datetime.utcnow()
    recent_alerts = [
        alert
        for alert in alerts
        if alert.created_at >= (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    ]

    status_counts = {"Online": 0, "Offline": 0}
    type_counts = defaultdict(int)
    location_status_counts = defaultdict(lambda: {"Online": 0, "Offline": 0})

    for camera in cameras:
        status_label = "Online" if _is_camera_online(camera.status) else "Offline"
        status_counts[status_label] += 1

        camera_type = (camera.type or "").strip() or "Other"
        type_counts[camera_type] += 1

        location = (camera.location or "").strip() or "Unassigned"
        location_status_counts[location][status_label] += 1

    alert_buckets = _generate_time_buckets(now, "week", recent_alerts)
    alert_labels = [label for label, _ in alert_buckets]
    alert_counts_by_type = defaultdict(lambda: {label: 0 for label in alert_labels})
    alert_totals = defaultdict(int)

    for alert in recent_alerts:
        alert_label = _format_alert_type(alert.alert_type)
        bucket_label = _bucket_key(alert.created_at, "week")
        if bucket_label in alert_counts_by_type[alert_label]:
            alert_counts_by_type[alert_label][bucket_label] += 1
            alert_totals[alert_label] += 1

    ordered_alert_types = sorted(
        alert_counts_by_type.keys(),
        key=lambda label: (-alert_totals[label], label.lower()),
    )

    total_cameras = len(cameras)
    online_cameras = status_counts["Online"]
    health_percentage = round((online_cameras / total_cameras) * 100) if total_cameras else 0

    return {
        "camera_status": {
            "labels": list(status_counts.keys()),
            "values": list(status_counts.values()),
        },
        "system_health": {
            "online": online_cameras,
            "offline": status_counts["Offline"],
            "percentage": health_percentage,
            "total": total_cameras,
        },
        "camera_types": {
            "labels": list(type_counts.keys()) or ["No Cameras"],
            "values": list(type_counts.values()) or [0],
        },
        "alerts_over_time": {
            "labels": alert_labels,
            "datasets": [
                {
                    "label": label,
                    "data": [alert_counts_by_type[label][bucket] for bucket in alert_labels],
                }
                for label in ordered_alert_types
            ],
        },
        "cameras_by_location": {
            "labels": sorted(location_status_counts.keys()),
            "online": [
                location_status_counts[location]["Online"]
                for location in sorted(location_status_counts.keys())
            ],
            "offline": [
                location_status_counts[location]["Offline"]
                for location in sorted(location_status_counts.keys())
            ],
        },
    }


def _normalize_report_timeframe(timeframe: str | None) -> str:
    if timeframe in REPORT_TIMEFRAME_OPTIONS:
        return timeframe
    return "month"


def _get_timeframe_start(now: datetime, timeframe: str) -> datetime | None:
    if timeframe == "day":
        return now - timedelta(hours=23)
    if timeframe == "week":
        return now - timedelta(days=6)
    if timeframe == "month":
        return now - timedelta(days=29)
    if timeframe == "year":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=334)
    if timeframe == "5years":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).replace(
            year=now.year - 4
        )
    return None


def _generate_time_buckets(now: datetime, timeframe: str, alerts: list[Alert]) -> list[tuple[str, datetime]]:
    if timeframe == "day":
        start = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        return [
            ((start + timedelta(hours=index)).strftime("%H:%M"), start + timedelta(hours=index))
            for index in range(24)
        ]
    if timeframe == "week":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            ((start + timedelta(days=index)).strftime("%d %b"), start + timedelta(days=index))
            for index in range(7)
        ]
    if timeframe == "month":
        start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            ((start + timedelta(days=index)).strftime("%d %b"), start + timedelta(days=index))
            for index in range(30)
        ]
    if timeframe == "year":
        first_month = datetime(now.year, now.month, 1)
        buckets = []
        cursor = first_month
        for _ in range(11):
            cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
        for _ in range(12):
            buckets.append((cursor.strftime("%b %Y"), cursor))
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)
        return buckets
    if timeframe == "5years":
        start_year = now.year - 4
        return [(str(year), datetime(year, 1, 1)) for year in range(start_year, now.year + 1)]

    if not alerts:
        return [(str(now.year), datetime(now.year, 1, 1))]

    earliest_year = min(alert.created_at.year for alert in alerts)
    return [(str(year), datetime(year, 1, 1)) for year in range(earliest_year, now.year + 1)]


def _bucket_key(timestamp: datetime, timeframe: str) -> str:
    if timeframe == "day":
        return timestamp.strftime("%H:00")
    if timeframe in {"week", "month"}:
        return timestamp.strftime("%d %b")
    if timeframe == "year":
        return timestamp.strftime("%b %Y")
    return str(timestamp.year)


def _build_trend_series(alerts: list[Alert], timeframe: str, now: datetime) -> dict[str, list]:
    timeframe = _normalize_report_timeframe(timeframe)
    timeframe_start = _get_timeframe_start(now, timeframe)
    filtered_alerts = [
        alert for alert in alerts if timeframe_start is None or alert.created_at >= timeframe_start
    ]

    buckets = _generate_time_buckets(now, timeframe, filtered_alerts)
    counts = {label: 0 for label, _ in buckets}
    for alert in filtered_alerts:
        key = _bucket_key(alert.created_at, timeframe)
        if key in counts:
            counts[key] += 1

    labels = [label for label, _ in buckets]
    return {
        "labels": labels,
        "values": [counts[label] for label in labels],
        "total": len(filtered_alerts),
        "latest_alert": filtered_alerts[0] if filtered_alerts else None,
        "timeframe": timeframe,
    }


def _collect_report_data(
    cameras: list[Camera], alerts: list[Alert], timeframe: str
) -> tuple[list[dict], list[dict], list[Alert]]:
    now = datetime.utcnow()
    camera_lookup = _build_camera_lookup(cameras)
    alerts_by_camera_id = defaultdict(list)
    unresolved_alerts = []

    for alert in alerts:
        matched_camera = _resolve_camera_from_source(alert.camera_source, camera_lookup)
        if matched_camera is None:
            unresolved_alerts.append(alert)
            continue
        alerts_by_camera_id[matched_camera.id].append(alert)

    camera_reports = []
    for camera in cameras:
        camera_alerts = alerts_by_camera_id.get(camera.id, [])
        trend = _build_trend_series(camera_alerts, timeframe, now)
        camera_reports.append(
            {
                "camera": camera,
                "alerts": camera_alerts,
                "alert_count": len(camera_alerts),
                "trend_labels": trend["labels"],
                "trend_values": trend["values"],
                "filtered_alert_count": trend["total"],
                "latest_alert": trend["latest_alert"],
                "latest_alert_type": _format_alert_type(trend["latest_alert"].alert_type)
                if trend["latest_alert"]
                else "No incidents",
            }
        )

    camera_reports.sort(
        key=lambda report: (
            -(report["latest_alert"].created_at.timestamp() if report["latest_alert"] else 0),
            report["camera"].name.lower(),
        )
    )

    group_reports = []
    groups = _get_current_user_camera_groups()
    for group in groups:
        group_cameras = sorted(group.cameras, key=lambda camera: (camera.name.lower(), camera.id))
        group_alerts = []
        for camera in group_cameras:
            group_alerts.extend(alerts_by_camera_id.get(camera.id, []))
        group_alerts.sort(key=lambda alert: alert.created_at, reverse=True)
        trend = _build_trend_series(group_alerts, timeframe, now)
        group_reports.append(
            {
                "group": group,
                "cameras": group_cameras,
                "alerts": group_alerts,
                "camera_count": len(group_cameras),
                "trend_labels": trend["labels"],
                "trend_values": trend["values"],
                "filtered_alert_count": trend["total"],
                "latest_alert": trend["latest_alert"],
            }
        )

    group_reports.sort(
        key=lambda report: (
            -(report["latest_alert"].created_at.timestamp() if report["latest_alert"] else 0),
            report["group"].name.lower(),
        )
    )

    return camera_reports, group_reports, unresolved_alerts


def _create_pdf_report(
    report_title: str,
    subject_lines: list[str],
    alerts: list[Alert],
    trend_labels: list[str],
    trend_values: list[int],
) -> BytesIO:
    def escape_pdf_text(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )

    def wrap_text(value: str, width: int = 88) -> list[str]:
        words = value.split()
        if not words:
            return [""]

        lines = []
        current_line = words[0]
        for word in words[1:]:
            candidate = f"{current_line} {word}"
            if len(candidate) <= width:
                current_line = candidate
            else:
                lines.append(current_line)
                current_line = word
        lines.append(current_line)
        return lines

    summary_lines = [
        report_title,
        f"Generated: {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}",
        *subject_lines,
    ]
    incident_lines = ["Recent Incidents"]
    if alerts:
        for alert in alerts[:25]:
            incident_header = (
                f"- {alert.created_at.strftime('%d %b %Y %H:%M')} | "
                f"{_format_alert_type(alert.alert_type)}"
            )
            incident_lines.append(incident_header)
            for detail_line in wrap_text(f"  Detail: {alert.detail}"):
                incident_lines.append(detail_line)
    else:
        incident_lines.append("- No incidents have been recorded for this selection.")

    page_width = 595
    page_height = 842
    left_margin = 48
    top_margin = 48
    line_height = 14
    chart_left = 70
    chart_bottom = 410
    chart_width = 450
    chart_height = 230
    chart_top = chart_bottom + chart_height
    chart_right = chart_left + chart_width
    y_axis_max = max(max(trend_values, default=0), 1)

    def make_text_stream(lines: list[str], start_y: int) -> list[str]:
        commands = ["BT", "/F1 11 Tf", f"1 0 0 1 {left_margin} {start_y} Tm", f"{line_height} TL"]
        for line_number, line in enumerate(lines):
            if line_number == 0:
                commands.append(f"({escape_pdf_text(line)}) Tj")
            else:
                commands.append("T*")
                commands.append(f"({escape_pdf_text(line)}) Tj")
        commands.append("ET")
        return commands

    first_page_commands = make_text_stream(summary_lines, page_height - top_margin)
    first_page_commands.extend(
        [
            "0.85 w",
            "0.82 0.84 0.88 RG",
            f"{chart_left} {chart_bottom} m {chart_left} {chart_top} l S",
            f"{chart_left} {chart_bottom} m {chart_right} {chart_bottom} l S",
        ]
    )

    for step in range(5):
        y_value = int(round((y_axis_max / 4) * step))
        y = chart_bottom + (chart_height / 4) * step
        first_page_commands.extend(
            [
                "0.92 0.93 0.95 RG",
                f"{chart_left} {y:.2f} m {chart_right} {y:.2f} l S",
                "BT",
                "/F1 9 Tf",
                f"1 0 0 1 {chart_left - 28} {y - 3:.2f} Tm",
                f"({escape_pdf_text(str(y_value))}) Tj",
                "ET",
            ]
        )

    point_count = max(len(trend_values), 1)
    x_step = chart_width / max(point_count - 1, 1)
    points = []
    for index, value in enumerate(trend_values or [0]):
        x = chart_left + (x_step * index if point_count > 1 else chart_width / 2)
        y = chart_bottom + (value / y_axis_max) * chart_height
        points.append((x, y))

    if points:
        path_commands = ["0.05 0.43 0.95 RG", "1.8 w"]
        for index, (x, y) in enumerate(points):
            path_commands.append(f"{x:.2f} {y:.2f} {'m' if index == 0 else 'l'}")
        path_commands.append("S")
        first_page_commands.extend(path_commands)

        for x, y in points:
            first_page_commands.extend(
                [
                    "0.05 0.43 0.95 rg",
                    f"{x - 2.5:.2f} {y - 2.5:.2f} 5 5 re f",
                ]
            )

    label_step = max(1, len(trend_labels) // 6)
    for index, label in enumerate(trend_labels):
        if index % label_step != 0 and index != len(trend_labels) - 1:
            continue
        x = chart_left + (x_step * index if point_count > 1 else chart_width / 2)
        first_page_commands.extend(
            [
                "BT",
                "/F1 8 Tf",
                f"1 0 0 1 {x - 10:.2f} {chart_bottom - 18:.2f} Tm",
                f"({escape_pdf_text(label)}) Tj",
                "ET",
            ]
        )

    first_page_commands.extend(
        [
            "BT",
            "/F1 12 Tf",
            f"1 0 0 1 {chart_left} {chart_top + 18} Tm",
            "(Incident Trend) Tj",
            "ET",
        ]
    )

    content_streams = []
    content_streams.append("\n".join(first_page_commands).encode("latin-1", errors="replace"))

    incident_page_capacity = 28
    for index in range(0, len(incident_lines), incident_page_capacity):
        page_lines = incident_lines[index:index + incident_page_capacity]
        commands = make_text_stream(page_lines, 760)
        content_streams.append("\n".join(commands).encode("latin-1", errors="replace"))

    objects = []

    def add_object(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    font_object_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_object_ids = []
    content_object_ids = []
    for stream in content_streams:
        content_object_ids.append(
            add_object(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
        )
        page_object_ids.append(0)

    pages_object_id = add_object(b"")
    for page_index, content_id in enumerate(content_object_ids):
        page_object_ids[page_index] = add_object(
            (
                f"<< /Type /Page /Parent {pages_object_id} 0 R "
                f"/MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects[pages_object_id - 1] = (
        f"<< /Type /Pages /Count {len(page_object_ids)} /Kids [{kids}] >>".encode("ascii")
    )
    catalog_object_id = add_object(f"<< /Type /Catalog /Pages {pages_object_id} 0 R >>".encode("ascii"))

    pdf = BytesIO()
    pdf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, object_data in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.write(object_data)
        pdf.write(b"\nendobj\n")

    xref_start = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_object_id} 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF"
        ).encode("ascii")
    )
    pdf.seek(0)
    return pdf


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
        username = request.form["username"].strip()
        password = request.form["password"]

        if User.query.filter_by(username=username).first() is not None:
            flash("Account already exists.", "danger")
            return render_template("signup.html")

        hashed_password = generate_password_hash(password, method="pbkdf2:sha256")
        new_user = User(username=username, password=hashed_password)

        try:
            db.session.add(new_user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Account already exists.", "danger")
            return render_template("signup.html")

        flash("Account created! Please log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/dashboard")
@login_required
def dashboard():
    cameras = _get_current_user_cameras()
    alerts = Alert.query.order_by(Alert.created_at.asc()).all()
    dashboard_data = _build_dashboard_data(cameras, alerts)
    return render_template("dashboard.html", active="dashboard", dashboard_data=dashboard_data)


@app.route("/reports")
@login_required
def reports():
    cameras = _get_current_user_cameras()
    groups = _get_current_user_camera_groups()

    return render_template(
        "reports.html",
        active="reports",
        cameras=cameras,
        groups=groups,
    )


@app.route("/reports/cameras/<int:camera_id>")
@login_required
def camera_report_detail(camera_id):
    timeframe = _normalize_report_timeframe(request.args.get("timeframe"))
    cameras = _get_current_user_cameras()
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    camera_reports, _, _ = _collect_report_data(cameras, alerts, timeframe)
    selected_report = next((report for report in camera_reports if report["camera"].id == camera_id), None)
    if selected_report is None:
        abort(404, description="Camera not found.")

    return render_template(
        "camera_report_detail.html",
        active="reports",
        timeframe=timeframe,
        timeframe_options=REPORT_TIMEFRAME_OPTIONS,
        report=selected_report,
    )


@app.route("/reports/groups/<int:group_id>")
@login_required
def group_report_detail(group_id):
    timeframe = _normalize_report_timeframe(request.args.get("timeframe"))
    cameras = _get_current_user_cameras()
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    _, group_reports, _ = _collect_report_data(cameras, alerts, timeframe)
    selected_report = next((report for report in group_reports if report["group"].id == group_id), None)
    if selected_report is None:
        abort(404, description="Camera group not found.")

    return render_template(
        "group_report_detail.html",
        active="reports",
        timeframe=timeframe,
        timeframe_options=REPORT_TIMEFRAME_OPTIONS,
        report=selected_report,
    )


@app.route("/reports/camera-report.pdf")
@login_required
def download_camera_report():
    timeframe = _normalize_report_timeframe(request.args.get("timeframe"))
    camera_id = (request.args.get("camera_id") or "").strip()

    if not camera_id:
        abort(400, description="Camera ID is required.")

    cameras = _get_current_user_cameras()
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    camera_reports, _, _ = _collect_report_data(cameras, alerts, timeframe)
    selected_report = next(
        (report for report in camera_reports if str(report["camera"].id) == camera_id),
        None,
    )
    if selected_report is None:
        abort(404, description="Camera not found.")

    pdf_buffer = _create_pdf_report(
        "VisionGuard Camera Report",
        [
            f"Camera: {selected_report['camera'].name}",
            f"Location: {selected_report['camera'].location}",
            f"Type: {selected_report['camera'].type}",
            f"Status: {selected_report['camera'].status}",
            f"Timeframe: {timeframe}",
            f"Incidents in timeframe: {selected_report['filtered_alert_count']}",
        ],
        [
            alert
            for alert in selected_report["alerts"]
            if _get_timeframe_start(datetime.utcnow(), timeframe) is None
            or alert.created_at >= _get_timeframe_start(datetime.utcnow(), timeframe)
        ],
        selected_report["trend_labels"],
        selected_report["trend_values"],
    )
    filename = f"visionguard-camera-{selected_report['camera'].name}-{timeframe}.pdf".replace(" ", "-").lower()
    return send_file(pdf_buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/reports/group-report.pdf")
@login_required
def download_group_report():
    timeframe = _normalize_report_timeframe(request.args.get("timeframe"))
    group_id = (request.args.get("group_id") or "").strip()

    if not group_id:
        abort(400, description="Camera group ID is required.")

    cameras = _get_current_user_cameras()
    alerts = Alert.query.order_by(Alert.created_at.desc()).all()
    _, group_reports, _ = _collect_report_data(cameras, alerts, timeframe)
    selected_report = next(
        (report for report in group_reports if str(report["group"].id) == group_id),
        None,
    )
    if selected_report is None:
        abort(404, description="Camera group not found.")

    timeframe_start = _get_timeframe_start(datetime.utcnow(), timeframe)
    pdf_buffer = _create_pdf_report(
        "VisionGuard Camera Group Report",
        [
            f"Camera group: {selected_report['group'].name}",
            f"Cameras in group: {selected_report['camera_count']}",
            f"Timeframe: {timeframe}",
            f"Incidents in timeframe: {selected_report['filtered_alert_count']}",
        ],
        [
            alert
            for alert in selected_report["alerts"]
            if timeframe_start is None or alert.created_at >= timeframe_start
        ],
        selected_report["trend_labels"],
        selected_report["trend_values"],
    )
    filename = f"visionguard-group-{selected_report['group'].name}-{timeframe}.pdf".replace(" ", "-").lower()
    return send_file(pdf_buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/cameras", methods=["GET"])
@login_required
def cameras():
    all_cameras = _get_current_user_cameras()
    groups = _get_current_user_camera_groups()
    return render_template("cameras.html", active="cameras", cameras=all_cameras, groups=groups)


@app.route("/add_camera", methods=["POST"])
@login_required
def add_camera():
    location = request.form["location"]
    name = request.form["name"]
    rtsp_url = request.form.get("rtsp_url", "").strip()
    type_ = request.form["type"]
    group_id = request.form.get("group_id", "").strip()

    if not rtsp_url:
        flash("RTSP URL is required.", "danger")
        return redirect(url_for("cameras"))

    max_number = db.session.query(db.func.max(Camera.number)).scalar() or 0
    next_number = max_number + 1

    selected_group = None
    if group_id:
        selected_group = _get_owned_camera_group(group_id)
        if selected_group is None:
            flash("Selected camera group was not found.", "danger")
            return redirect(url_for("cameras"))

    new_camera = Camera(
        number=next_number,
        user_id=current_user.id,
        location=location,
        area="",
        name=name,
        rtsp_url=rtsp_url,
        type=type_,
        status="Active",
        group_id=selected_group.id if selected_group else None,
    )
    db.session.add(new_camera)
    db.session.commit()

    notification_msg = f"New camera added: {name} at {location}"
    if selected_group is not None:
        notification_msg += f" in group {selected_group.name}"
    db.session.add(Notification(user_id=current_user.id, message=notification_msg))
    db.session.commit()

    flash("Camera added successfully!", "success")
    return redirect(url_for("cameras"))


@app.route("/cameras/<int:camera_id>/delete", methods=["POST"])
@login_required
def delete_camera(camera_id):
    camera = _get_owned_camera_or_404(camera_id)
    camera_name = camera.name

    db.session.delete(camera)
    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"Camera deleted: {camera_name}",
        )
    )
    db.session.commit()

    flash("Camera deleted successfully.", "success")
    return redirect(url_for("cameras"))


@app.route("/cameras/<int:camera_id>/edit", methods=["POST"])
@login_required
def edit_camera(camera_id):
    camera = _get_owned_camera_or_404(camera_id)

    location = request.form.get("location", "").strip()
    name = request.form.get("name", "").strip()
    rtsp_url = request.form.get("rtsp_url", "").strip()
    type_ = request.form.get("type", "").strip()
    status = request.form.get("status", "").strip()
    group_id = request.form.get("group_id", "").strip()

    if not location or not name or not rtsp_url or not type_ or not status:
        flash("Location, name, RTSP URL, type, and status are required.", "danger")
        return redirect(url_for("cameras"))

    selected_group = None
    if group_id:
        selected_group = _get_owned_camera_group(group_id)
        if selected_group is None:
            flash("Selected camera group was not found.", "danger")
            return redirect(url_for("cameras"))

    camera.location = location
    camera.name = name
    camera.rtsp_url = rtsp_url
    camera.type = type_
    camera.status = status
    camera.group_id = selected_group.id if selected_group else None

    notification_message = f"Camera updated: {camera.name}"
    if selected_group is not None:
        notification_message += f" in group {selected_group.name}"

    db.session.add(Notification(user_id=current_user.id, message=notification_message))
    db.session.commit()

    flash("Camera updated successfully.", "success")
    return redirect(url_for("cameras"))


@app.route("/cameras/bulk-delete", methods=["POST"])
@login_required
def bulk_delete_cameras():
    camera_ids = request.form.getlist("camera_ids")
    if not camera_ids:
        flash("Select at least one camera to delete.", "danger")
        return redirect(url_for("cameras"))

    cameras_to_delete = _user_camera_query().filter(Camera.id.in_(camera_ids)).all()
    if not cameras_to_delete:
        flash("No valid cameras were selected.", "danger")
        return redirect(url_for("cameras"))

    deleted_count = len(cameras_to_delete)
    for camera in cameras_to_delete:
        db.session.delete(camera)

    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"{deleted_count} camera{'s' if deleted_count != 1 else ''} deleted",
        )
    )
    db.session.commit()

    flash(f"Deleted {deleted_count} camera{'s' if deleted_count != 1 else ''}.", "success")
    return redirect(url_for("cameras"))


@app.route("/cameras/bulk-edit", methods=["POST"])
@login_required
def bulk_edit_cameras():
    camera_ids = request.form.getlist("camera_ids")
    if not camera_ids:
        flash("Select at least one camera to edit.", "danger")
        return redirect(url_for("cameras"))

    cameras_to_update = _user_camera_query().filter(Camera.id.in_(camera_ids)).all()
    if not cameras_to_update:
        flash("No valid cameras were selected.", "danger")
        return redirect(url_for("cameras"))

    location = request.form.get("location", "").strip()
    type_ = request.form.get("type", "").strip()
    status = request.form.get("status", "").strip()
    group_id = request.form.get("group_id", "").strip()

    selected_group = None
    if group_id:
        if group_id == "__ungrouped__":
            selected_group = "__ungrouped__"
        else:
            selected_group = _get_owned_camera_group(group_id)
            if selected_group is None:
                flash("Selected camera group was not found.", "danger")
                return redirect(url_for("cameras"))

    if not any([location, type_, status, group_id]):
        flash("Provide at least one field to update.", "danger")
        return redirect(url_for("cameras"))

    for camera in cameras_to_update:
        if location:
            camera.location = location
        if type_:
            camera.type = type_
        if status:
            camera.status = status
        if group_id:
            camera.group_id = None if selected_group == "__ungrouped__" else selected_group.id

    updated_count = len(cameras_to_update)
    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"{updated_count} camera{'s' if updated_count != 1 else ''} updated",
        )
    )
    db.session.commit()

    flash(f"Updated {updated_count} camera{'s' if updated_count != 1 else ''}.", "success")
    return redirect(url_for("cameras"))


@app.route("/camera-groups", methods=["GET"])
@login_required
def camera_groups():
    groups = _get_current_user_camera_groups()
    cameras = _get_current_user_cameras()
    ungrouped_cameras = [camera for camera in cameras if camera.group_id is None]
    return render_template(
        "camera_groups.html",
        active="camera-groups",
        groups=groups,
        cameras=ungrouped_cameras,
    )


@app.route("/camera-groups", methods=["POST"])
@login_required
def create_camera_group():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None
    selected_camera_ids = request.form.getlist("camera_ids")

    if not name:
        flash("Camera group name is required.", "danger")
        return redirect(url_for("camera_groups"))

    existing_group = _user_camera_group_query().filter(
        db.func.lower(CameraGroup.name) == name.lower()
    ).first()
    if existing_group is not None:
        flash("A camera group with that name already exists.", "danger")
        return redirect(url_for("camera_groups"))

    new_group = CameraGroup(user_id=current_user.id, name=name, description=description)
    db.session.add(new_group)
    db.session.flush()

    if selected_camera_ids:
        cameras_to_assign = _user_camera_query().filter(Camera.id.in_(selected_camera_ids)).all()
        for camera in cameras_to_assign:
            camera.group_id = new_group.id

    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"Camera group created: {name}",
        )
    )
    db.session.commit()

    flash("Camera group created successfully.", "success")
    return redirect(url_for("camera_groups"))


@app.route("/camera-groups/<int:group_id>/edit", methods=["POST"])
@login_required
def edit_camera_group(group_id):
    group = _get_owned_camera_group_or_404(group_id)
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip() or None

    if not name:
        flash("Camera group name is required.", "danger")
        return redirect(url_for("camera_groups"))

    existing_group = _user_camera_group_query().filter(
        db.func.lower(CameraGroup.name) == name.lower(),
        CameraGroup.id != group.id,
    ).first()
    if existing_group is not None:
        flash("A camera group with that name already exists.", "danger")
        return redirect(url_for("camera_groups"))

    group.name = name
    group.description = description
    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"Camera group updated: {group.name}",
        )
    )
    db.session.commit()

    flash("Camera group updated successfully.", "success")
    return redirect(url_for("camera_groups"))


@app.route("/camera-groups/<int:group_id>/delete", methods=["POST"])
@login_required
def delete_camera_group(group_id):
    group = _get_owned_camera_group_or_404(group_id)
    group_name = group.name

    for camera in group.cameras:
        camera.group_id = None

    db.session.delete(group)
    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"Camera group deleted: {group_name}",
        )
    )
    db.session.commit()

    flash("Camera group deleted successfully.", "success")
    return redirect(url_for("camera_groups"))


@app.route("/camera-groups/bulk-delete", methods=["POST"])
@login_required
def bulk_delete_camera_groups():
    group_ids = request.form.getlist("group_ids")
    if not group_ids:
        flash("Select at least one camera group to delete.", "danger")
        return redirect(url_for("camera_groups"))

    groups_to_delete = _user_camera_group_query().filter(CameraGroup.id.in_(group_ids)).all()
    if not groups_to_delete:
        flash("No valid camera groups were selected.", "danger")
        return redirect(url_for("camera_groups"))

    deleted_count = len(groups_to_delete)
    for group in groups_to_delete:
        for camera in group.cameras:
            camera.group_id = None
        db.session.delete(group)

    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"{deleted_count} camera group{'s' if deleted_count != 1 else ''} deleted",
        )
    )
    db.session.commit()

    flash(f"Deleted {deleted_count} camera group{'s' if deleted_count != 1 else ''}.", "success")
    return redirect(url_for("camera_groups"))


@app.route("/camera-groups/bulk-edit", methods=["POST"])
@login_required
def bulk_edit_camera_groups():
    group_ids = request.form.getlist("group_ids")
    if not group_ids:
        flash("Select at least one camera group to edit.", "danger")
        return redirect(url_for("camera_groups"))

    groups_to_update = _user_camera_group_query().filter(CameraGroup.id.in_(group_ids)).all()
    if not groups_to_update:
        flash("No valid camera groups were selected.", "danger")
        return redirect(url_for("camera_groups"))

    description = request.form.get("description", "").strip()
    if not description:
        flash("Provide a description to apply to the selected camera groups.", "danger")
        return redirect(url_for("camera_groups"))

    for group in groups_to_update:
        group.description = description

    updated_count = len(groups_to_update)
    db.session.add(
        Notification(
            user_id=current_user.id,
            message=f"{updated_count} camera group{'s' if updated_count != 1 else ''} updated",
        )
    )
    db.session.commit()

    flash(f"Updated {updated_count} camera group{'s' if updated_count != 1 else ''}.", "success")
    return redirect(url_for("camera_groups"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_user.notification_email = request.form.get("notification_email", "").strip() or None
        current_user.email_notifications_enabled = (
            request.form.get("email_notifications_enabled") == "on"
        )
        current_user.dark_mode_enabled = request.form.get("dark_mode_enabled") == "on"
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


@app.route("/api/cameras")
@login_required
def get_cameras():
    cameras = _get_current_user_cameras()
    return jsonify([_serialize_camera(camera) for camera in cameras])


@app.route("/api/notifications/<int:notification_id>", methods=["DELETE"])
@login_required
def delete_notification(notification_id):
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first_or_404()
    db.session.delete(notification)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/notifications", methods=["DELETE"])
@login_required
def delete_all_notifications():
    Notification.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"success": True})


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
    app.run(debug=os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes"})
