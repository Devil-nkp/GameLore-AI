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
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY") 

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
    credits = db.Column(db.Integer, default=10) # Increased credits
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)
    badges = db.Column(db.Text, default="[]") 

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    images = db.Column(db.Text, default="[]") 
    video_url = db.Column(db.String(500))
    likes = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- PRECISION AI ENGINES ---

def clean_text(text):
    """Aggressively cleans text to be plain and readable"""
    text = re.sub(r'\*\*|__', '', text) # Remove bold
    text = re.sub(r'#+', '', text)      # Remove headers
    text = re.sub(r'^\s*-\s+', '• ', text, flags=re.MULTILINE) # Nice bullets
    return text.strip()

def get_video_preview(content_type, genre):
    """
    SMART VIDEO LOGIC:
    Searching for specific items (e.g. "Void Sword") returns bad stock footage.
    Instead, we search for the GENRE Atmosphere (e.g. "Cyberpunk Background")
    to give the user a 'Vibe Check' video that is always relevant/high quality.
    """
    if not PEXELS_API_KEY: return None
    try:
        # Search for the MOOD, not the item.
        query = f"{genre} cinematic background loop"
        
        headers = {"Authorization": PEXELS_API_KEY}
        url = f"https://api.pexels.com/videos/search?query={query}&per_page=1&orientation=landscape&size=medium"
        res = requests.get(url, headers=headers, timeout=3)
        data = res.json()
        
        if data.get('videos'):
            video_files = data['videos'][0]['video_files']
            # Get a lightweight HD file for fast loading
            best = next((v for v in video_files if v['width'] >= 1280 and v['width'] < 2000), video_files[0])
            return best['link']
    except:
        pass
    return None

def generate_images_precise(content_type, genre, details, count=3):
    """
    PRECISION IMAGE PROMPTING:
    Forces the AI to draw exactly what is asked by prepending structural keywords.
    """
    image_list = []
    
    # 1. Define strict prefixes based on type
    prefix = ""
    if content_type == "Item" or content_type == "Weapon":
        prefix = "isolated object, 3d render, white background, single weapon asset, detailed product shot, "
    elif content_type == "NPC":
        prefix = "character portrait, face closeup, rpg character art, detailed, looking at camera, "
    elif content_type == "Location":
        prefix = "wide shot, environment concept art, landscape, detailed scenery, "
    
    # 2. Build the full prompt
    full_prompt = f"{prefix} {genre} style, {details}"
    encoded_prompt = quote(full_prompt)
    
    # 3. Generate variants
    for i in range(count):
        seed = random.randint(100, 99999)
        # Using Pollinations with 'nologo' and specific seed
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"
        image_list.append(url)
        
    return json.dumps(image_list)

def generate_text_fast(content_type, genre, details):
    """Uses Llama 3 8B Instant for sub-1-second responses"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    system_prompt = (
        "You are an expert Game Writer. "
        "Output CLEAN plain text only. No markdown formatting (*, #). "
        "Structure: 1. Visual Description. 2. Lore/Backstory. 3. Stats/Abilities. "
        "Keep it concise and punchy."
    )
    user_prompt = f"Write about a {content_type} in a {genre} setting. Details: {details}"
    
    data = {
        "model": "llama-3.1-8b-instant", # The Fastest Model
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 500
    }
    
    try:
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 200:
            return clean_text(res.json()['choices'][0]['message']['content'])
    except Exception as e:
        print(f"Groq Error: {e}")
    return "Generation failed. Please try again."

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
    
    # Execute Generators
    text_content = generate_text_fast(data.get('type'), data.get('genre'), data.get('details'))
    # Use the new Precise Image Logic
    images_json = generate_images_precise(data.get('type'), data.get('genre'), data.get('details'), count=3)
    # Use the new Ambient Video Logic
    video_url = get_video_preview(data.get('type'), data.get('genre'))

    # Credits & XP
    if not current_user.is_pro: current_user.credits -= 1
    current_user.xp += 15 # More XP
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
        # Send text preview
        requests.post(DISCORD_WEBHOOK_URL, json={"content": f"🚀 **New Creation**\nType: {gen.type}\n\n{gen.content[:200]}..."})
        return "<span class='text-green-400 text-sm'>Shared!</span>"
    return "<span class='text-red-400 text-sm'>Config Error</span>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
