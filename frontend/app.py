from flask import Flask, render_template, jsonify, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# --- Flask app and database setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
db = SQLAlchemy(app)

# --- Flask-Login setup ---
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# --- User model ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)

# ✅ Step 1: Add the Notification model here
class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "message": self.message,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat()
        }

class Camera(db.Model):
    __tablename__ = "camera"   # make sure this matches your DB
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, autoincrement=True)  # optional
    location = db.Column(db.String(100), nullable=False)
    area = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="Active")

# --- User loader for Flask-Login ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))  # if logged in, go to dashboard
    else:
        return redirect(url_for("login"))  # if not logged in, go to login

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password')

    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, password=hashed_password)

        db.session.add(new_user)
        db.session.commit()

        flash('Account created! Please log in.')
        return redirect(url_for('login'))

    return render_template('signup.html')

@app.route('/dashboard')
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

    # assign next camera number
    max_number = db.session.query(db.func.max(Camera.number)).scalar() or 0
    next_number = max_number + 1

    # create new camera
    new_camera = Camera(
        number=next_number,
        location=location,
        area=area,
        name=name,
        type=type_,
        status="Active"
    )
    db.session.add(new_camera)
    db.session.commit()  # commit first so new_camera has an ID

    # create a notification
    notification_msg = f"New camera added: {name} at {location}, {area}"
    new_notification = Notification(
    user_id=current_user.id,
    message=notification_msg
    )
    db.session.add(new_notification)
    db.session.commit()

    flash("Camera added successfully!", "success")
    return redirect(url_for("cameras"))

@app.route("/settings")
@login_required
def settings():
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

@app.route('/logout')
@login_required
def logout():
    logout_user()   # clears the session
    flash('You have been logged out.')
    return redirect(url_for('login'))

@app.route("/api/notifications")
@login_required
def get_notifications():
    notifications = (
        Notification.query
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return jsonify([n.to_dict() for n in notifications])

if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # create the SQLite database automatically
    app.run(debug=True)