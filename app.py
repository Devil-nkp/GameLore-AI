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
app.secret_key = os.getenv("SECRET_KEY", "supreme_v3_key")

# --- Configuration ---
# Ensure uploads directory exists
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Database
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_v3.db")
if db_url.startswith("postgres://"): 
    db_url = db_url.replace("postgres://", "postgresql://", 1)
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
    credits = db.Column(db.Integer, default=50)

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50)) # 'Image' or 'Video'
    prompt_used = db.Column(db.Text)
    result_url = db.Column(db.Text) # Stores Image or Video URL
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Logic: V3 Engines ---

def generate_visuals_v3(prompt):
    """Generates exactly 2 High-Quality Images."""
    images = []
    
    # Image 1: Evolink (High Quality)
    if EVOLINK_KEY:
        try:
            headers = {"Authorization": f"Bearer {EVOLINK_KEY}", "Content-Type": "application/json"}
            resp = requests.post("https://api.evolink.ai/v1/images/generations", 
                               json={"prompt": prompt, "n": 1, "model": "flux-realism"}, 
                               headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data: images.append(data['data'][0]['url'])
        except: pass

    # Image 2 (or Backup): Pollinations (Reliable)
    # We loop to ensure we always have 2 images total
    while len(images) < 2:
        seed = random.randint(1, 999999)
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&seed={seed}&nologo=true&model=flux"
        images.append(url)
        
    return images[:2] # Strict limit of 2

def generate_video_v3(image_input, is_file=False, prompt=""):
    """
    Handles Video Generation.
    If is_file=True, image_input is a filepath (needs Base64 conversion).
    If is_file=False, image_input is a URL.
    """
    if not A2E_KEY: return None

    try:
        headers = {"x-api-key": A2E_KEY, "Content-Type": "application/json"}
        payload = {}

        if is_file:
            # Convert local file to Base64 for API
            with open(image_input, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                # A2E often accepts base64 or requires a public URL. 
                # If A2E strictly requires URL, this part fails on localhost without S3.
                # NOTE: For this demo, we assume the user provides a URL or we use the URL flow.
                # Fallback: Many APIs accept "data:image/png;base64,..."
                payload["image_url"] = f"data:image/png;base64,{encoded_string}" 
        else:
            payload["image_url"] = image_input

        # Add prompt if API supports it (A2E is mostly img2vid, but we send it as context if possible)
        # payload["text_prompt"] = prompt 

        resp = requests.post("https://api.a2e.ai/v1/image-to-video", json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json().get('video_url')
    except Exception as e:
        print(f"Video Error: {e}")
        
    return None

# --- Routes ---

@app.route('/')
def home():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/auth/google')
def google_auth():
    # Demo Fallback
    if not os.getenv("GOOGLE_CLIENT_ID"):
        user = User.query.first() or User(email="demo@user.com", name="Commander"); db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()
    user = User.query.filter_by(email=user_info['email']).first()
    if not user:
        user = User(email=user_info['email'], name=user_info.get('name', 'User')); db.session.add(user); db.session.commit()
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
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    return render_template('dashboard.html', user=current_user, gens=gens)

# --- IMAGE STUDIO ---
@app.route('/generate_visuals', methods=['GET', 'POST'])
@login_required
def generate_visuals():
    if request.method == 'GET': return render_template('generate.html')
    
    d = request.form
    # Refined Prompting
    prompt = f"{d.get('genre')} style, {d.get('details')}"
    if d.get('type') == "Weapon": prompt += ", isolated, white background, 3d render, blender, 8k"
    
    image_urls = generate_visuals_v3(prompt)
    
    return render_template('partials/image_selection.html', 
                           images=image_urls, prompt_base=prompt,
                           c_type=d.get('type'))

@app.route('/save_generation', methods=['POST'])
@login_required
def save_generation():
    d = request.form
    gen = Generation(user_id=current_user.id, type=d.get('type'), prompt_used=d.get('prompt_base'), result_url=d.get('selected_image'))
    db.session.add(gen)
    db.session.commit()
    return render_template('partials/final_result.html', gen=gen)

# --- VIDEO STUDIO (NEW) ---
@app.route('/video_studio', methods=['GET', 'POST'])
@login_required
def video_studio():
    if request.method == 'GET': return render_template('video_studio.html')
    
    video_url = None
    prompt = request.form.get('prompt', '')
    
    # Handle File Upload
    if 'image_file' in request.files and request.files['image_file'].filename != '':
        file = request.files['image_file']
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        video_url = generate_video_v3(filepath, is_file=True, prompt=prompt)
        # Clean up
        try: os.remove(filepath) 
        except: pass
        
    # Handle URL Paste
    elif request.form.get('image_url'):
        video_url = generate_video_v3(request.form.get('image_url'), is_file=False, prompt=prompt)

    if video_url:
        gen = Generation(user_id=current_user.id, type='Video', prompt_used=prompt, result_url=video_url)
        db.session.add(gen)
        db.session.commit()
        return render_template('partials/final_result.html', gen=gen)
    
    return "<div class='text-red-500 text-center p-4'>Video Generation Failed. Try a different source.</div>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
