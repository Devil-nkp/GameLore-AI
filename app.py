import os
import time
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supreme_key_123")

# --- Database ---
# Using sqlite for ease, change to postgres for production
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///supreme_ultimate.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- OAuth ---
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
    credits = db.Column(db.Integer, default=100) # More credits for massive gen

class Generation(db.Model):
    """Stores finalized generations that the user decided to save."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    prompt = db.Column(db.Text)
    # We store the "best" image picked by user
    main_image_url = db.Column(db.Text)
    # We store related videos
    a2e_video_url = db.Column(db.Text)
    horde_video_url = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# ===========================
# === AI ENGINE FUNCTIONS ===
# ===========================

def generate_raphael(prompt):
    key = os.getenv("RAPHAEL_KEY")
    if not key: return {"error": "Raphael Key Missing"}
    try:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        # Adjust endpoint based on actual Raphael API docs
        response = requests.post("https://api.raphael.app/v1/generations", json={"prompt": prompt, "model": "flux-realism"}, headers=headers, timeout=30)
        if response.status_code == 200:
            # Assuming response structure, adjust as needed based on actual API
            return {"url": response.json().get('output', {}).get('url')} 
        return {"error": f"Raphael Error: {response.text}"}
    except Exception as e: return {"error": str(e)}

def generate_a2e_video(image_url):
    key = os.getenv("A2E_KEY")
    if not key: return {"error": "A2E Key Missing"}
    try:
        headers = {"x-api-key": key, "Content-Type": "application/json"}
        # Adjust endpoint based on A2E docs
        response = requests.post("https://api.a2e.ai/v1/image-to-video", json={"image_url": image_url}, headers=headers, timeout=30)
        if response.status_code == 200:
            return {"url": response.json().get('video_url')}
        return {"error": f"A2E Error: {response.text}"}
    except Exception as e: return {"error": str(e)}

# --- AI Horde (Async Engines) ---
HORDE_API_KEY = os.getenv("HORDE_KEY", "0000000000") # Default anonymous
HORDE_BASE = "https://stablehorde.net/api/v2"

def horde_start_gen(payload, type='image'):
    headers = {"apikey": HORDE_API_KEY, "Content-Type": "application/json"}
    endpoint = "/generate/async" if type == 'image' else "/generate/video/async"
    try:
        resp = requests.post(f"{HORDE_BASE}{endpoint}", json=payload, headers=headers)
        if resp.status_code == 202: return {"id": resp.json()['id']}
        return {"error": resp.text}
    except Exception as e: return {"error": str(e)}

def horde_check_status(gen_id, type='image'):
    endpoint = "/generate/status" if type == 'image' else "/generate/video/status"
    try:
        resp = requests.get(f"{HORDE_BASE}{endpoint}/{gen_id}")
        data = resp.json()
        if data['done']:
            # Cleanup: Horde image results are often temporary urls or base64
            result_url = data['generations'][0]['img'] 
            return {"status": "done", "url": result_url}
        return {"status": "processing", "wait": data.get('wait_time', 10)}
    except Exception as e: return {"error": str(e)}


# ===========================
# === ROUTES & API ENDPOINTS ===
# ===========================

# --- Standard Page Routes (Login, Hub, History) ---
# (Keep these mostly the same as before)
@app.route('/')
def login():
    if current_user.is_authenticated: return redirect(url_for('hub'))
    return render_template('login.html')

@app.route('/auth/google')
def google_auth():
    # MOCK LOGIN for easy testing if no keys set
    if not os.getenv("GOOGLE_CLIENT_ID"):
        user = User.query.first() or User(email="demo@test.com", name="Supreme Commander"); db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('hub'))
    # Real Login
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()
    user = User.query.filter_by(email=user_info['email']).first()
    if not user: user = User(email=user_info['email'], name=user_info.get('name', 'User')); db.session.add(user); db.session.commit()
    login_user(user)
    return redirect(url_for('hub'))

@app.route('/login/google')
def google_login_start():
    if not os.getenv("GOOGLE_CLIENT_ID"): return redirect(url_for('google_auth'))
    return google.authorize_redirect(url_for('google_auth', _external=True))

@app.route('/logout')
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/hub')
@login_required
def hub():
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).limit(20).all()
    return render_template('hub.html', user=current_user, history=gens)

@app.route('/history/<int:gen_id>')
@login_required
def history_detail(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    return render_template('history_detail.html', gen=gen)

# --- API Endpoints (Called by Frontend JS) ---

@app.route('/api/gen/raphael', methods=['POST'])
@login_required
def api_gen_raphael():
    prompt = request.json.get('prompt')
    return jsonify(generate_raphael(prompt))

@app.route('/api/gen/horde/image/start', methods=['POST'])
@login_required
def api_horde_img_start():
    prompt = request.json.get('prompt')
    # Basic Horde payload
    payload = {"prompt": prompt, "params": {"steps": 30, "width": 512, "height": 512}}
    return jsonify(horde_start_gen(payload, 'image'))

@app.route('/api/gen/horde/image/check/<id>', methods=['GET'])
def api_horde_img_check(id):
    return jsonify(horde_check_status(id, 'image'))

# Simplified Video flow: Frontend sends image URL directly
@app.route('/api/gen/a2e', methods=['POST'])
@login_required
def api_gen_a2e():
    img_url = request.json.get('image_url')
    return jsonify(generate_a2e_video(img_url))

# Final Save (When user picks the best result)
@app.route('/api/save_final', methods=['POST'])
@login_required
def save_final():
    data = request.json
    gen = Generation(
        user_id=current_user.id,
        prompt=data.get('prompt'),
        main_image_url=data.get('image_url'),
        a2e_video_url=data.get('a2e_url'),
        horde_video_url=data.get('horde_url')
    )
    db.session.add(gen)
    db.session.commit()
    return jsonify({"status": "saved", "id": gen.id})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
