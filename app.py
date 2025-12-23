import os
import json
import random
import re
import time
import requests
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from sqlalchemy.sql import func
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key_change_me")

# --- Database ---
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_lore.db")
if db_url.startswith("postgres://"): 
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- Keys ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN") # Optional: For AI Video
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- OAuth ---
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
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
    credits = db.Column(db.Integer, default=15) # Generous start
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    prompt_used = db.Column(db.Text)
    content = db.Column(db.Text)
    selected_image = db.Column(db.String(500))
    video_url = db.Column(db.String(500))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Logic: Prompt Architect ---
def construct_precision_prompt(content_type, genre, details):
    """Enforces strict visual rules based on asset type."""
    base = f"{genre} style, {details}"
    if content_type == "Weapon":
        return f"isolated single weapon asset, {base}, 3d render, blender style, plain white background, high detail, no characters, no hands"
    elif content_type == "Item":
        return f"isolated game item icon, {base}, digital painting, magical glow, plain background, centric composition, no text"
    elif content_type == "NPC":
        return f"character concept art, portrait of {base}, looking at camera, detailed face, upper body, rpg style, dynamic lighting"
    elif content_type == "Location":
        return f"wide shot, environmental concept art, {base}, atmospheric, cinematic lighting, unreal engine 5, 8k"
    return base

def clean_text(text):
    text = re.sub(r'\*\*|__', '', text) 
    text = re.sub(r'#+', '', text)
    text = re.sub(r'^\s*-\s+', '• ', text, flags=re.MULTILINE)
    return text.strip()

# --- Logic: Generators ---
def generate_images_pollinations(prompt, count=4):
    images = []
    encoded = quote(prompt)
    for i in range(count):
        seed = random.randint(1, 99999)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&seed={seed}&nologo=true&model=flux"
        images.append(url)
    return images

def generate_lore_groq(content_type, genre, details):
    if not GROQ_API_KEY: return "Error: GROQ_API_KEY missing."
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    sys_prompt = "You are a Game Lore Expert. Output plain text. No markdown. Structure: 1. Visuals 2. History 3. Abilities."
    user_prompt = f"Write lore for a {content_type} in {genre}. Context: {details}"
    data = {"model": "llama-3.1-8b-instant", "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}], "temperature": 0.7}
    try:
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 200: return clean_text(res.json()['choices'][0]['message']['content'])
    except: return "Lore generation failed."
    return "Error"

def generate_video_replicate(image_url):
    if not REPLICATE_API_TOKEN: return None
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"}
    data = {
        "version": "1446844794ad193ee054152331572c638202521c402120b08064402633000570",
        "input": {"input_image": image_url, "video_length": "14_frames_with_svd_xt", "frames_per_second": 6}
    }
    try:
        req = requests.post("https://api.replicate.com/v1/predictions", json=data, headers=headers)
        if req.status_code != 201: return None
        get_url = req.json()['urls']['get']
        for _ in range(15): 
            time.sleep(2)
            check = requests.get(get_url, headers=headers).json()
            if check['status'] == 'succeeded': return check['output']
            if check['status'] == 'failed': return None
    except: return None
    return None

# --- Routes ---
@app.route('/')
def home():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    return render_template('home.html')

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
    user = User.query.filter_by(email=user_info['email']).first()
    if not user:
        user = User(email=user_info['email'], name=user_info.get('name', 'Creator'))
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
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    return render_template('dashboard.html', user=current_user, gens=gens)

# PHASE 1: Visuals
@app.route('/generate_visuals', methods=['GET', 'POST'])
@login_required
def generate_visuals():
    if request.method == 'GET': return render_template('generate.html')
    data = request.form
    precise_prompt = construct_precision_prompt(data.get('type'), data.get('genre'), data.get('details'))
    image_urls = generate_images_pollinations(precise_prompt, count=4)
    return render_template('partials/image_selection.html', 
                           images=image_urls, prompt_base=precise_prompt,
                           c_type=data.get('type'), c_genre=data.get('genre'), c_details=data.get('details'))

# PHASE 2: Finalize
@app.route('/finalize_creation', methods=['POST'])
@login_required
def finalize_creation():
    data = request.form
    selected_image = data.get('selected_image')
    lore = generate_lore_groq(data.get('type'), data.get('genre'), data.get('details'))
    
    gen = Generation(user_id=current_user.id, type=data.get('type'), prompt_used=data.get('prompt_base'), content=lore, selected_image=selected_image)
    if not current_user.is_pro: current_user.credits -= 1
    current_user.xp += 20
    db.session.add(gen)
    db.session.commit()
    return render_template('partials/final_result.html', gen=gen)

# PHASE 3: Video
@app.route('/create_video/<int:gen_id>', methods=['POST'])
@login_required
def create_video(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    video_url = generate_video_replicate(gen.selected_image)
    if video_url:
        gen.video_url = video_url
        db.session.commit()
        return f"""<video src="{video_url}" controls autoplay loop class="w-full rounded-lg border border-purple-500 shadow-[0_0_20px_rgba(168,85,247,0.5)]"></video>"""
    else:
        return f"""<div class="relative w-full aspect-square overflow-hidden rounded-lg border border-purple-500"><img src="{gen.selected_image}" class="w-full h-full object-cover animate-ken-burns"><div class="absolute bottom-2 right-2 bg-black/70 text-white text-xs px-2 py-1 rounded">Simulated Motion</div></div><style>.animate-ken-burns {{ animation: ken-burns 15s ease-in-out infinite alternate; }} @keyframes ken-burns {{ 0% {{ transform: scale(1); }} 100% {{ transform: scale(1.1) translate(-2%, -2%); }} }}</style>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

