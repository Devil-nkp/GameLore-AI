import os
import random
import re
import time
import base64
import requests
from datetime import datetime
from urllib.parse import quote
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supreme_v4_key")

# --- Config ---
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Database
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_v4.db")
if db_url.startswith("postgres://"): db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'home'

# --- Keys ---
EVOLINK_KEY = os.getenv("EVOLINK_KEY")
A2E_KEY = os.getenv("A2E_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

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
    credits = db.Column(db.Integer, default=100)

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50)) # Image, Video
    prompt_used = db.Column(db.Text)
    result_url = db.Column(db.Text) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Logic: Supreme Engines ---

def construct_futuristic_prompt(base_details, asset_type):
    """
    V4 Engine: Injects 'Evo Gun' style keywords automatically.
    """
    # The 'Supreme' Style Injector
    style_modifiers = "glowing neon energy lines, holographic aura, floating crystal particles, cybernetic details, futuristic sci-fi aesthetic, unreal engine 5 render, cinematic lighting, volumetric fog, masterpiece, 8k resolution, high contrast, bloom effect"
    
    if asset_type == "Weapon":
        return f"legendary sci-fi weapon, {base_details}, {style_modifiers}, isolated, black background, 3d render, intricate mechanical parts, glowing barrel"
    elif asset_type == "Character":
        return f"futuristic cyberpunk warrior, {base_details}, {style_modifiers}, detailed armor, glowing eyes, dynamic pose, epic composition"
    elif asset_type == "Vehicle":
        return f"futuristic sci-fi vehicle, {base_details}, {style_modifiers}, hovering, energy thrusters, sleek metal design"
    
    return f"{base_details}, {style_modifiers}"

def generate_visuals_v4(prompt):
    images = []
    # 1. Evolink (Primary)
    if EVOLINK_KEY:
        try:
            headers = {"Authorization": f"Bearer {EVOLINK_KEY}", "Content-Type": "application/json"}
            resp = requests.post("https://api.evolink.ai/v1/images/generations", 
                               json={"prompt": prompt, "n": 1, "model": "flux-realism"}, 
                               headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data: images.append(data['data'][0]['url'])
        except: pass

    # 2. Pollinations (Backup)
    while len(images) < 2:
        seed = random.randint(1, 999999)
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&seed={seed}&nologo=true&model=flux"
        images.append(url)
    return images[:2]

def generate_video_v4(image_input, is_file=False):
    if not A2E_KEY: return None
    try:
        headers = {"x-api-key": A2E_KEY, "Content-Type": "application/json"}
        payload = {}
        if is_file:
            with open(image_input, "rb") as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
                payload["image_url"] = f"data:image/png;base64,{b64}"
        else:
            payload["image_url"] = image_input
            
        resp = requests.post("https://api.a2e.ai/v1/image-to-video", json=payload, headers=headers, timeout=40)
        if resp.status_code == 200:
            return resp.json().get('video_url')
    except: pass
    return None

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/auth/google')
def google_auth():
    if not os.getenv("GOOGLE_CLIENT_ID"): # Demo
        user = User.query.first() or User(email="demo@test.com", name="Commander"); db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    token = google.authorize_access_token()
    u = google.get('userinfo').json()
    user = User.query.filter_by(email=u['email']).first()
    if not user: user = User(email=u['email'], name=u.get('name', 'User')); db.session.add(user); db.session.commit()
    login_user(user)
    return redirect(url_for('dashboard'))

@app.route('/login/google')
def google_login():
    if not os.getenv("GOOGLE_CLIENT_ID"): return redirect(url_for('google_auth'))
    return google.authorize_redirect(url_for('google_auth', _external=True))

@app.route('/logout')
def logout(): logout_user(); return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Show ALL history (Images & Videos)
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    return render_template('dashboard.html', user=current_user, gens=gens)

@app.route('/generate_visuals', methods=['GET', 'POST'])
@login_required
def generate_visuals():
    if request.method == 'GET': return render_template('generate.html')
    
    d = request.form
    # V4 Prompt Engine
    final_prompt = construct_futuristic_prompt(d.get('details'), d.get('type'))
    image_urls = generate_visuals_v4(final_prompt)
    
    # AUTO-SAVE HISTORY (Fixing user issue)
    for url in image_urls:
        gen = Generation(user_id=current_user.id, type=d.get('type'), prompt_used=final_prompt, result_url=url)
        db.session.add(gen)
    db.session.commit()
    
    return render_template('partials/image_selection.html', images=image_urls, prompt_base=final_prompt)

@app.route('/video_studio', methods=['GET', 'POST'])
@login_required
def video_studio():
    if request.method == 'GET': return render_template('video_studio.html')
    
    vid_url = None
    prompt = request.form.get('prompt', 'Cinematic motion')
    
    # File Upload (or Paste)
    if 'image_file' in request.files and request.files['image_file'].filename != '':
        f = request.files['image_file']
        path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(f.filename))
        f.save(path)
        vid_url = generate_video_v4(path, is_file=True)
        try: os.remove(path) 
        except: pass
    # URL Input
    elif request.form.get('image_url'):
        vid_url = generate_video_v4(request.form.get('image_url'), is_file=False)

    if vid_url:
        # Auto-Save Video
        gen = Generation(user_id=current_user.id, type='Video', prompt_used=prompt, result_url=vid_url)
        db.session.add(gen)
        db.session.commit()
        return render_template('partials/final_result.html', gen=gen)
    
    return "<div class='text-red-400 text-center p-4 bg-red-900/20 rounded border border-red-500'>Video Generation Failed. Try another image.</div>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
