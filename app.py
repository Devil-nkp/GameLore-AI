import os
import json
import time
import requests
import stripe
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from sqlalchemy.sql import func
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
import io

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key")

# --- Database Config ---
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_lore.db")
if db_url.startswith("postgres://"): 
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- Integrations ---
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") 
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

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

# --- Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=3)
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)
    badges = db.Column(db.Text, default="[]") 

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    likes = db.Column(db.Integer, default=0)  # <--- NEW COLUMN
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Helper Logic ---
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

def award_xp(user, amount):
    user.xp += amount
    badges = json.loads(user.badges)
    if user.xp >= 50 and "Scribe" not in badges:
        badges.append("Scribe")
        flash("🏆 Earned Badge: Scribe!", "success")
    user.badges = json.dumps(badges)
    db.session.commit()

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    # Calculate Global Likes for the "Special Line"
    total_likes = db.session.query(func.sum(Generation.likes)).scalar() or 0
    return render_template('home.html', total_likes=total_likes)

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

@app.route('/logout')  # <--- NEW LOGOUT ROUTE
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    badges = json.loads(current_user.badges)
    # Fetch user history
    recent_gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    return render_template('dashboard.html', user=current_user, badges=badges, recent_gens=recent_gens)

@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    if request.method == 'GET':
        return render_template('generate.html')
    
    if not current_user.is_pro and current_user.credits < 1:
        return "<div class='text-red-400'>❌ Out of credits.</div>"

    data = request.form
    result_text = call_groq_api(data.get('type'), data.get('genre'), data.get('details'))
    
    if not current_user.is_pro:
        current_user.credits -= 1
    award_xp(current_user, 10)
    
    gen = Generation(user_id=current_user.id, type=data.get('type'), content=result_text)
    db.session.add(gen)
    db.session.commit()
    
    return render_template('partials/result_card.html', gen=gen)

@app.route('/like/<int:gen_id>', methods=['POST']) # <--- NEW LIKE ROUTE
@login_required
def like_content(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    gen.likes += 1
    db.session.commit()
    # Return just the button part (HTMX swap)
    return f"""
    <button class="flex items-center gap-1 text-pink-500 font-bold" disabled>
        ❤️ {gen.likes}
    </button>
    """

@app.route('/export/<int:gen_id>')
@login_required
def export_unity(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    unity_data = {"id": gen.id, "type": gen.type, "content": gen.content}
    return send_file(io.BytesIO(json.dumps(unity_data, indent=4).encode()), mimetype='application/json', as_attachment=True, download_name=f"gamelore_{gen.id}.json")

@app.route('/share_discord/<int:gen_id>', methods=['POST'])
@login_required
def share_discord(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🚀 **New Creation**\nType: {gen.type}\nLikes: {gen.likes}"})
        return "<span class='text-green-400'>Shared!</span>"
    return "<span class='text-red-400'>Discord Error</span>"

@app.route('/payment')
def payment():
    return render_template('payment.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

