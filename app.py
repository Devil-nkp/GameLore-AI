import os
import json
import time
import random
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
    credits = db.Column(db.Integer, default=5)
    xp = db.Column(db.Integer, default=0) 
    is_pro = db.Column(db.Boolean, default=False)
    badges = db.Column(db.Text, default="[]") 

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    type = db.Column(db.String(50))
    content = db.Column(db.Text)
    # Changed to store a JSON list of multiple image URLs
    images = db.Column(db.Text, default="[]") 
    likes = db.Column(db.Integer, default=0)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- AI Logic ---

def generate_text_groq(content_type, genre, details):
    """Generates deep lore using Llama 3.1"""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    system_prompt = (
        "You are a Senior Game Designer. Output strictly in Markdown. "
        "Structure: 1. Visuals 2. Lore 3. Mechanics. "
        "Be creative, unique, and professional."
    )
    user_prompt = f"Create a {content_type} for a {genre} game. Details: {details}."
    
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.8
    }
    
    try:
        res = requests.post(url, headers=headers, json=data)
        if res.status_code == 200:
            return res.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Groq Error: {e}")
    return "AI Generation failed."

def generate_multiple_images(description, count=4):
    """Generates multiple unique variants using random seeds"""
    image_list = []
    base_prompt = quote(f"concept art, video game asset, {description}")
    
    for i in range(count):
        seed = random.randint(1000, 9999)
        # We append the seed to the URL to force a different image
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
def about():
    return render_template('about.html')

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
    # Pre-process images for template
    for gen in recent_gens:
        try:
            gen.image_list = json.loads(gen.images)
        except:
            gen.image_list = []
    return render_template('dashboard.html', user=current_user, badges=badges, recent_gens=recent_gens)

@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    if request.method == 'GET':
        return render_template('generate.html')
    
    if not current_user.is_pro and current_user.credits < 1:
        return "<div class='text-red-400 p-4 border border-red-600 rounded'>❌ Out of credits.</div>"

    data = request.form
    
    # 1. Generate Text
    text_content = generate_text_groq(data.get('type'), data.get('genre'), data.get('details'))
    
    # 2. Generate 4 Images
    images_json = generate_multiple_images(f"{data.get('type')} {data.get('genre')} {data.get('details')[:80]}")

    # 3. Credits & XP
    if not current_user.is_pro:
        current_user.credits -= 1
    
    current_user.xp += 10
    badges = json.loads(current_user.badges)
    if current_user.xp >= 50 and "Scribe" not in badges:
        badges.append("Scribe")
    current_user.badges = json.dumps(badges)

    # 4. Save
    gen = Generation(
        user_id=current_user.id, 
        type=data.get('type'), 
        content=text_content, 
        images=images_json 
    )
    db.session.add(gen)
    db.session.commit()
    
    # Attach list for the immediate view
    gen.image_list = json.loads(images_json)
    
    return render_template('partials/result_card.html', gen=gen)

@app.route('/like/<int:gen_id>', methods=['POST'])
@login_required
def like_content(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    gen.likes += 1
    db.session.commit()
    return f"""<button class="flex items-center gap-1 text-pink-500 font-bold" disabled>❤️ {gen.likes}</button>"""

# --- Download Routes ---

@app.route('/download_json/<int:gen_id>')
@login_required
def download_json(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    
    data = {
        "id": gen.id, 
        "type": gen.type, 
        "content": gen.content,
        "images": json.loads(gen.images)
    }
    return send_file(
        io.BytesIO(json.dumps(data, indent=4).encode()),
        mimetype='application/json',
        as_attachment=True,
        download_name=f"gamelore_{gen.id}.json"
    )

@app.route('/download_text/<int:gen_id>')
@login_required
def download_text(gen_id):
    """Download plain text file"""
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    
    return Response(
        gen.content,
        mimetype="text/plain",
        headers={"Content-disposition": f"attachment; filename=gamelore_{gen.id}.txt"}
    )

@app.route('/share_discord/<int:gen_id>', methods=['POST'])
@login_required
def share_discord(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if DISCORD_WEBHOOK_URL:
        # Get first image for preview
        imgs = json.loads(gen.images)
        first_img = imgs[0] if imgs else ""
        
        payload = {
            "content": f"🚀 **New Creation by {current_user.name}**\nType: {gen.type}\nLikes: {gen.likes}\n{first_img}"
        }
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return "<span class='text-green-400 text-sm font-bold'>Shared!</span>"
    return "<span class='text-red-400 text-sm'>Not configured.</span>"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
