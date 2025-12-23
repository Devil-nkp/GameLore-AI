import os
import json
import random
import re
import requests
import stripe
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from sqlalchemy.sql import func
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
import io

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key")

# --- Database & Config ---
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_lore.db")
if db_url.startswith("postgres://"): 
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- Keys ---
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") 
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY") # NEW: For Video Search

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
    credits = db.Column(db.Integer, default=5)
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)
    badges = db.Column(db.Text, default="[]") 

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    images = db.Column(db.Text, default="[]") 
    video_url = db.Column(db.String(500)) # NEW: Video Link
    likes = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Helper Functions ---

def clean_text(text):
    """Removes markdown symbols (*, #, -) for clean output"""
    text = re.sub(r'\*\*|__', '', text)  # Remove bold
    text = re.sub(r'#+', '', text)       # Remove headers
    text = re.sub(r'^\s*-\s+', '', text, flags=re.MULTILINE) # Remove list dashes
    return text.strip()

def get_video_preview(query):
    """Fetches a relevant video URL from Pexels API"""
    if not PEXELS_API_KEY: return None
    try:
        headers = {"Authorization": PEXELS_API_KEY}
        url = f"https://api.pexels.com/videos/search?query={query}&per_page=1&orientation=landscape"
        res = requests.get(url, headers=headers)
        data = res.json()
        if data['videos']:
            # Get the HD video file link
            video_files = data['videos'][0]['video_files']
            # Find a medium quality one for speed
            best_video = next((v for v in video_files if v['width'] >= 1280), video_files[0])
            return best_video['link']
    except Exception as e:
        print(f"Video Error: {e}")
    return None

def generate_text_groq(content_type, genre, details):
    """Faster Llama 3.1 generation"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    # Prompt optimized for speed and cleanliness
    system_prompt = (
        "You are a Game Writer. Output ONLY plain text paragraphs. "
        "Do NOT use asterisks, hashes, or bullet points. "
        "Be concise, creative, and fast."
    )
    user_prompt = f"Write a {content_type} description for a {genre} game. Context: {details}."
    
    data = {
        "model": "llama-3.1-70b-versatile", # Faster model
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 600 # Limit length for speed
    }
    
    try:
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 200:
            raw_text = res.json()['choices'][0]['message']['content']
            return clean_text(raw_text)
    except Exception as e:
        print(f"Groq Error: {e}")
    return "Generation failed."

def generate_images(description, count=3):
    image_list = []
    base_prompt = quote(f"concept art, {description}")
    for i in range(count):
        seed = random.randint(100, 9999)
        url = f"https://image.pollinations.ai/prompt/{base_prompt}?width=512&height=512&nologo=true&seed={seed}"
        image_list.append(url)
    return json.dumps(image_list)

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    total_likes = db.session.query(func.sum(Generation.likes)).scalar() or 0
    return render_template('home.html', total_likes=total_likes)

@app.route('/about')
def about(): return render_template('about.html')

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

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    badges = json.loads(current_user.badges)
    recent_gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    for gen in recent_gens:
        try: gen.image_list = json.loads(gen.images)
        except: gen.image_list = []
    return render_template('dashboard.html', user=current_user, badges=badges, recent_gens=recent_gens)

@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    if request.method == 'GET':
        return render_template('generate.html')
    
    if not current_user.is_pro and current_user.credits < 1:
        return "<div class='text-red-400 p-4 border border-red-600 rounded'>❌ Out of credits.</div>"

    data = request.form
    
    # Parallel-like Execution
    text_content = generate_text_groq(data.get('type'), data.get('genre'), data.get('details'))
    images_json = generate_images(f"{data.get('type')} {data.get('genre')}", count=3)
    video_url = get_video_preview(f"{data.get('genre')} {data.get('type')}") # Get Video

    # Stats
    if not current_user.is_pro: current_user.credits -= 1
    current_user.xp += 10
    badges = json.loads(current_user.badges)
    if current_user.xp >= 50 and "Scribe" not in badges: badges.append("Scribe")
    current_user.badges = json.dumps(badges)

    gen = Generation(
        user_id=current_user.id, 
        type=data.get('type'), 
        content=text_content, 
        images=images_json,
        video_url=video_url
    )
    db.session.add(gen)
    db.session.commit()
    
    gen.image_list = json.loads(images_json)
    return render_template('partials/result_card.html', gen=gen)

@app.route('/like/<int:gen_id>', methods=['POST'])
@login_required
def like_content(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    gen.likes += 1
    db.session.commit()
    return f"""<button class="flex items-center gap-1 text-pink-500 font-bold" disabled>❤️ {gen.likes}</button>"""

@app.route('/download_text/<int:gen_id>')
@login_required
def download_text(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    return Response(gen.content, mimetype="text/plain", headers={"Content-disposition": f"attachment; filename=gamelore_{gen.id}.txt"})

@app.route('/share_discord/<int:gen_id>', methods=['POST'])
@login_required
def share_discord(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🚀 **New Creation**\n{gen.type}\n{gen.video_url if gen.video_url else 'No Video'}"})
        return "<span class='text-green-400 text-sm'>Shared!</span>"
    return "<span class='text-red-400 text-sm'>Error</span>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
