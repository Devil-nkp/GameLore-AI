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

# Load .env variables (for local testing)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key")

# --- Database Config (Fix for Render) ---
# Use Render's Postgres URL if available, otherwise fallback to local SQLite
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_lore.db")
if db_url.startswith("postgres://"): 
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- 3rd Party Integrations ---
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") 
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- OAuth Setup ---
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

# --- Database Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=3) # Start with 3 free credits
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)
    badges = db.Column(db.Text, default="[]") 

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- CRITICAL FIX: Create Tables on Startup ---
# This ensures tables exist even when running on Render/Gunicorn
with app.app_context():
    db.create_all()

# --- Helper Logic ---
def award_xp(user, amount):
    user.xp += amount
    badges = json.loads(user.badges)
    
    # Simple Badge Logic
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

def call_groq_api(content_type, genre, details):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"Generate a {content_type} for a {genre} game. Details: {details}. Format as Markdown."
    
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    try:
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 200:
            return res.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Groq Error: {e}")
    return "AI Generation failed. Please try again."

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('home.html')

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
        user = User(email=email, name=user_info.get('name', 'Dev'))
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
    
    # Check Credits
    if not current_user.is_pro and current_user.credits < 1:
        return "<div class='text-red-400 p-4 border border-red-600 rounded bg-red-900/20'>❌ Out of credits. Please upgrade in Dashboard.</div>"

    data = request.form
    result_text = call_groq_api(data.get('type'), data.get('genre'), data.get('details'))
    
    # Deduct Credit & Award XP
    if not current_user.is_pro:
        current_user.credits -= 1
    award_xp(current_user, 10)
    
    # Save Generation
    gen = Generation(user_id=current_user.id, type=data.get('type'), content=result_text)
    db.session.add(gen)
    db.session.commit()
    
    return render_template('partials/result_card.html', gen=gen)

@app.route('/export/<int:gen_id>')
@login_required
def export_unity(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    
    unity_data = {"id": gen.id, "type": gen.type, "content": gen.content}
    return send_file(
        io.BytesIO(json.dumps(unity_data, indent=4).encode()),
        mimetype='application/json',
        as_attachment=True,
        download_name=f"gamelore_{gen.id}.json"
    )

@app.route('/share_discord/<int:gen_id>', methods=['POST'])
@login_required
def share_discord(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if DISCORD_WEBHOOK_URL:
        payload = {"content": f"🚀 **New Creation by {current_user.name}**\nType: {gen.type}\nCheck it out on GameLore AI!"}
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return "<span class='text-green-400 text-sm font-bold'>Shared to Discord!</span>"
    return "<span class='text-red-400 text-sm'>Discord not configured.</span>"

@app.route('/payment')
@login_required
def payment():
    # Placeholder for Payment Page
    return render_template('payment.html', key=os.getenv("STRIPE_PUBLISHABLE_KEY"))

# --- Run Server ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

