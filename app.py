import os
import json
import time
import requests
import stripe
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
import io

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key")

# --- Config ---
# DB: Use Render's Postgres or local SQLite
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_lore.db")
if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- 3rd Party Integrations ---
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") # For community sharing
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- OAuth Setup (Simplified) ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    access_token_url='https://accounts.google.com/o/oauth2/token',
    access_token_params=None,
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    client_kwargs={'scope': 'email profile'},
)

# --- Models with Gamification ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=1)
    xp = db.Column(db.Integer, default=0) # Gamification XP
    is_pro = db.Column(db.Boolean, default=False)
    generations = db.relationship('Generation', backref='author', lazy=True)
    badges = db.Column(db.Text, default="[]") # JSON list of badges ["Novice", "Creator"]

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    meta_data = db.Column(db.Text) # JSON for Unity stats
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Helper: Gamification Logic ---
def award_xp(user, amount):
    user.xp += amount
    badges = json.loads(user.badges)
    
    # Check Logic: Award Badges based on XP
    new_badge = None
    if user.xp >= 50 and "Scribe" not in badges:
        new_badge = "Scribe"
    elif user.xp >= 200 and "World Builder" not in badges:
        new_badge = "World Builder"
        
    if new_badge:
        badges.append(new_badge)
        user.badges = json.dumps(badges)
        flash(f"🏆 Level Up! You earned the '{new_badge}' Badge!", "success")
    
    db.session.commit()

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('home.html')

# OAuth Login
@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_auth', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google')
def google_auth():
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()
    email = user_info['email']
    
    user = User.query.filter_by(email=email).first()
    if not user:
        # Create new user
        user = User(email=email, name=user_info.get('name', 'Dev'), credits=3) # generous start
        db.session.add(user)
        db.session.commit()
    
    login_user(user)
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    badges = json.loads(current_user.badges)
    recent_gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).limit(5).all()
    return render_template('dashboard.html', user=current_user, badges=badges, recent_gens=recent_gens)

@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    if request.method == 'GET':
        return render_template('generate.html')
    
    # HTMX POST Handling
    if not current_user.is_pro and current_user.credits < 1:
        return "<div class='text-red-500'>❌ Out of credits. Please upgrade.</div>"

    data = request.form
    prompt = f"Generate a Unity-ready {data.get('type')} for a {data.get('genre')} game. Details: {data.get('details')}."
    
    # Call Groq (Mocked for brevity, use real call from previous code)
    # response = call_groq(prompt)... 
    # Simulated result:
    ai_content = f"## {data.get('type').upper()}\n**Concept:** A {data.get('genre')} masterpiece.\n\n{data.get('details')}..."
    
    # Deduct credit & Award XP
    if not current_user.is_pro:
        current_user.credits -= 1
    award_xp(current_user, 10) # +10 XP per gen
    
    # Save
    gen = Generation(user_id=current_user.id, type=data.get('type'), content=ai_content)
    db.session.add(gen)
    db.session.commit()
    
    # Return HTMX Partial
    return render_template('partials/result_card.html', gen=gen)

@app.route('/export/<int:gen_id>')
@login_required
def export_unity(gen_id):
    """Exports content as a JSON file for Unity/Godot"""
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    
    # Format for Unity
    unity_data = {
        "id": gen.id,
        "type": gen.type,
        "content": gen.content,
        "exported_at": str(datetime.now())
    }
    
    return send_file(
        io.BytesIO(json.dumps(unity_data, indent=4).encode()),
        mimetype='application/json',
        as_attachment=True,
        download_name=f"gamelore_{gen.id}.json"
    )

@app.route('/share_discord/<int:gen_id>', methods=['POST'])
@login_required
def share_discord(gen_id):
    """Viral Hook: Post to Community Discord"""
    gen = Generation.query.get_or_404(gen_id)
    
    payload = {
        "content": f"🚀 **New Creation by {current_user.name}**\nType: {gen.type}\nCheck it out on GameLore AI!"
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)
    return "<span class='text-green-500 text-sm'>Shared to Discord!</span>"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)
