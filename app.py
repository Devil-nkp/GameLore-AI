import os
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
app.secret_key = os.getenv("SECRET_KEY", "supreme_key_123")

# --- Database ---
db_url = os.getenv("DATABASE_URL", "sqlite:///supreme_restored.db")
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
HORDE_KEY = os.getenv("HORDE_KEY", "0000000000")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") # Optional for Lore
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

# --- Models (Fixed Text Columns) ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=50)
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    prompt_used = db.Column(db.Text)
    content = db.Column(db.Text)
    # Changed to Text to prevent crashes
    selected_image = db.Column(db.Text) 
    video_url = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Logic: Hybrid Engines ---

def generate_visuals_hybrid(prompt, count=4):
    """
    Tries Evolink first (High Quality).
    Falls back to Pollinations (Free/Unlimited).
    """
    images = []
    
    # 1. Try Evolink (If Key Exists)
    if EVOLINK_KEY:
        try:
            headers = {"Authorization": f"Bearer {EVOLINK_KEY}", "Content-Type": "application/json"}
            # Request 1 image to save credits/time, then fill rest with Pollinations
            resp = requests.post("https://api.evolink.ai/v1/images/generations", 
                               json={"prompt": prompt, "n": 1, "model": "flux-realism"}, 
                               headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data:
                    images.append(data['data'][0]['url'])
        except:
            pass # Fail silently to fallback

    # 2. Fill remaining slots with Pollinations (Flux)
    remaining = count - len(images)
    for i in range(remaining):
        seed = random.randint(1, 99999)
        safe_prompt = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&seed={seed}&nologo=true&model=flux"
        images.append(url)
        
    return images

def generate_video_hybrid(image_url):
    """Tries A2E first, then falls back to Horde or Simulated."""
    if A2E_KEY:
        try:
            headers = {"x-api-key": A2E_KEY, "Content-Type": "application/json"}
            resp = requests.post("https://api.a2e.ai/v1/image-to-video", json={"image_url": image_url}, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp.json().get('video_url')
        except:
            pass
            
    # Fallback to Simulated Video (Ken Burns) if no API available
    return None 

def clean_text(text):
    text = re.sub(r'\*\*|__', '', text) 
    return text.strip()

# --- Routes (Original "Supreme" Layout) ---

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
    # Demo Fallback
    if not GOOGLE_CLIENT_ID:
        user = User.query.first() or User(email="demo@user.com", name="Commander"); db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
        
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()
    user = User.query.filter_by(email=user_info['email']).first()
    if not user:
        user = User(email=user_info['email'], name=user_info.get('name', 'User'))
        db.session.add(user)
        db.session.commit()
    login_user(user)
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout(): logout_user(); return redirect(url_for('home'))

@app.route('/dashboard')
@login_required
def dashboard():
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).all()
    return render_template('dashboard.html', user=current_user, gens=gens)

@app.route('/generate_visuals', methods=['GET', 'POST'])
@login_required
def generate_visuals():
    if request.method == 'GET': return render_template('generate.html')
    
    # 1. Construct Prompt
    d = request.form
    base_prompt = f"{d.get('genre')} style, {d.get('details')}"
    if d.get('type') == "Weapon": base_prompt = f"isolated weapon, {base_prompt}, white background, no humans"
    
    # 2. Hybrid Generation
    image_urls = generate_visuals_hybrid(base_prompt)
    
    return render_template('partials/image_selection.html', 
                           images=image_urls, prompt_base=base_prompt,
                           c_type=d.get('type'), c_genre=d.get('genre'), c_details=d.get('details'))

@app.route('/finalize_creation', methods=['POST'])
@login_required
def finalize_creation():
    d = request.form
    # Simple Lore Gen (Mock if Groq missing)
    lore = f"Analysis of {d.get('type')}: A legendary artifact found in the {d.get('genre')} realms."
    
    gen = Generation(
        user_id=current_user.id, type=d.get('type'), 
        prompt_used=d.get('prompt_base'), content=lore, 
        selected_image=d.get('selected_image')
    )
    db.session.add(gen)
    db.session.commit()
    return render_template('partials/final_result.html', gen=gen)

@app.route('/create_video/<int:gen_id>', methods=['POST'])
@login_required
def create_video(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    video_url = generate_video_hybrid(gen.selected_image)
    
    if video_url:
        gen.video_url = video_url
        db.session.commit()
        return f"""<video src="{video_url}" controls autoplay loop class="w-full rounded-lg border border-purple-500 shadow-[0_0_20px_rgba(168,85,247,0.5)]"></video>"""
    else:
        # Fallback Ken Burns
        return f"""
        <div class="relative w-full aspect-square overflow-hidden rounded-lg border border-purple-500 shadow-[0_0_20px_rgba(168,85,247,0.5)]">
            <img src="{gen.selected_image}" class="w-full h-full object-cover animate-ken-burns">
            <div class="absolute bottom-2 right-2 bg-black/70 text-white text-xs px-2 py-1 rounded">Simulated Motion</div>
        </div>
        <style>.animate-ken-burns {{ animation: ken-burns 15s ease-in-out infinite alternate; }} @keyframes ken-burns {{ 0% {{ transform: scale(1); }} 100% {{ transform: scale(1.1) translate(-2%, -2%); }} }}</style>
        """

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
