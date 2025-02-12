import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import func
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone
import logging
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "lottery-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

logging.basicConfig(level=logging.DEBUG)

# Import models after db initialization
from models import User, LotteryDraw, Feedback

def create_admin_if_not_exists():
    try:
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(
                username='admin',
                password_hash=User.set_password('admin123')
            )
            db.session.add(admin)
            db.session.commit()
            logging.info("Admin user created successfully")
        else:
            logging.info("Admin user already exists")
    except Exception as e:
        logging.error(f"Error creating admin user: {str(e)}")
        db.session.rollback()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    latest_draw = LotteryDraw.query.order_by(LotteryDraw.date.desc()).first()
    first_prize = latest_draw.first_prize if latest_draw else []
    second_prizes = [latest_draw.second_prizes[i:i+4] for i in range(0, len(latest_draw.second_prizes), 4)] if latest_draw else []
    feedback_messages = Feedback.query.order_by(Feedback.date.desc()).limit(5).all()
    return render_template('index.html',
                         first_prize=first_prize,
                         second_prizes=second_prizes,
                         next_draw=get_next_draw_time(),
                         feedback_messages=feedback_messages)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        logging.debug(f"Login attempt for username: {username}")
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            logging.info(f"User {username} logged in successfully")
            return redirect(url_for('admin'))

        logging.warning(f"Failed login attempt for username: {username}")
        flash('Invalid username or password')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/admin')
@login_required
def admin():
    return render_template('admin.html')

@app.route('/history')
def history():
    draws = LotteryDraw.query.order_by(LotteryDraw.date.desc()).all()
    history_data = [
        {
            'date': draw.date.strftime('%d.%m.%Y 2:00 PM'),
            'first_prize': draw.first_prize,
            'second_prizes': [draw.second_prizes[i:i+4] for i in range(0, len(draw.second_prizes), 4)]
        }
        for draw in draws
    ]
    return render_template('history.html', history=history_data)

@app.route('/delete-entry', methods=['POST'])
@login_required
def delete_entry():
    if current_user.username != 'admin':
        flash('Only admin can delete entries', 'error')
        return redirect(url_for('history'))
    entry_date = datetime.strptime(request.form['entry_date'], '%d.%m.%Y 2:00 PM')
    draw = LotteryDraw.query.filter(
        func.date_trunc('day', LotteryDraw.date) == entry_date.date()
    ).first()
    
    if draw:
        db.session.delete(draw)
        db.session.commit()
        flash('Entry deleted successfully', 'success')
    else:
        flash('Entry not found', 'error')
    
    return redirect(url_for('history'))

@app.route('/api/set-numbers', methods=['POST'])
@login_required
def set_numbers():
    data = request.json
    first_prize = data.get('first_prize', [])
    second_prizes = data.get('second_prizes', [])

    if not LotteryDraw.validate_numbers(first_prize):
        return jsonify({'error': 'Invalid first prize numbers'}), 400

    if not LotteryDraw.validate_numbers(second_prizes, count=3):
        return jsonify({'error': 'Invalid second prize numbers'}), 400

    draw = LotteryDraw(
        first_prize=first_prize,
        second_prizes=second_prizes,
        date=datetime.now(timezone('US/Pacific'))  # Set the current time as draw time
    )
    db.session.add(draw)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    try:
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')
        
        if not all([name, email, message]):
            flash('Please fill in all fields', 'error')
            return redirect(url_for('index'))
            
        feedback = Feedback(name=name, email=email, message=message)
        db.session.add(feedback)
        db.session.commit()
        
        flash('Thank you for your feedback!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while submitting feedback', 'error')
        logging.error(f"Feedback submission error: {str(e)}")
    
    return redirect(url_for('index'))

def get_next_draw_time():
    pst = timezone('US/Pacific')
    now = datetime.now(pst)
    latest_draw = LotteryDraw.query.order_by(LotteryDraw.date.desc()).first()

    if latest_draw:
        # If we have a draw today, next draw is tomorrow
        if latest_draw.date.date() == now.date():
            next_draw = now.replace(day=now.day + 1, hour=14, minute=0, second=0, microsecond=0)
        # If no draw today and it's before 2 PM, draw is today
        elif now.hour < 14:
            next_draw = now.replace(hour=14, minute=0, second=0, microsecond=0)
        # Otherwise, draw is tomorrow
        else:
            next_draw = now.replace(day=now.day + 1, hour=14, minute=0, second=0, microsecond=0)
    else:
        # If no draws yet, next draw is today at 2 PM if before 2 PM, otherwise tomorrow
        if now.hour < 14:
            next_draw = now.replace(hour=14, minute=0, second=0, microsecond=0)
        else:
            next_draw = now.replace(day=now.day + 1, hour=14, minute=0, second=0, microsecond=0)

    return next_draw.strftime('%d.%m.%Y')

# Initialize database and create admin user
with app.app_context():
    db.create_all()
    create_admin_if_not_exists()

# Initialize scheduler
scheduler = BackgroundScheduler(timezone=timezone('US/Pacific'))
scheduler.start()
