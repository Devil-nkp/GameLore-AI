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
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///supreme_v2.db")
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
    credits = db.Column(db.Integer, default=100)

class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    prompt = db.Column(db.Text)
    # Storing results from different engines
    puter_image_url = db.Column(db.Text)
    evolink_image_url = db.Column(db.Text)
    horde_image_url = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# ===========================
# === AI ENGINES ===
# ===========================

def generate_evolink(prompt):
    """
    Generates image using Evolink.ai. 
    Fallback: Returns error if key is missing, allowing other engines to run.
    """
    key = os.getenv("EVOLINK_KEY")
    if not key:
        return {"error": "Skipped (No Key)"}
    
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        # Standard OpenAI-compatible format often used by these new APIs
        payload = {
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024", 
            "model": "flux-realism" # Assuming model name, can be adjusted
        }
        response = requests.post("https://api.evolink.ai/v1/images/generations", json=payload, headers=headers, timeout=40)
        
        if response.status_code == 200:
            data = response.json()
            # Standard OpenAI format usually puts URL in data[0]['url']
            if 'data' in data and len(data['data']) > 0:
                 return {"url": data['data'][0]['url']}
            return {"error": "Evolink No Data"}
        return {"error": f"Evolink Error: {response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# --- AI Horde (Async Backend Fallback) ---
HORDE_API_KEY = os.getenv("HORDE_KEY", "0000000000") # Anonymous Fallback
HORDE_BASE = "https://stablehorde.net/api/v2"

def horde_start_gen(payload):
    headers = {"apikey": HORDE_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.post(f"{HORDE_BASE}/generate/async", json=payload, headers=headers)
        if resp.status_code == 202: return {"id": resp.json()['id']}
        return {"error": resp.text}
    except Exception as e: return {"error": str(e)}

def horde_check_status(gen_id):
    try:
        resp = requests.get(f"{HORDE_BASE}/generate/status/{gen_id}")
        data = resp.json()
        if data['done']:
            return {"status": "done", "url": data['generations'][0]['img']}
        return {"status": "processing", "wait": data.get('wait_time', 10)}
    except Exception as e: return {"error": str(e)}

# ===========================
# === ROUTES ===
# ===========================

@app.route('/')
def login():
    if current_user.is_authenticated: return redirect(url_for('hub'))
    return render_template('login.html')

@app.route('/auth/google')
def google_auth():
    # Fallback Demo Login if No Google Keys
    if not os.getenv("GOOGLE_CLIENT_ID"):
        user = User.query.first() or User(email="demo@user.com", name="Commander")
        db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('hub'))
        
    token = google.authorize_access_token()
    user_info = google.get('userinfo').json()
    user = User.query.filter_by(email=user_info['email']).first()
    if not user:
        user = User(email=user_info['email'], name=user_info.get('name', 'User'))
        db.session.add(user); db.session.commit()
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
    # Show history
    gens = Generation.query.filter_by(user_id=current_user.id).order_by(Generation.timestamp.desc()).limit(20).all()
    return render_template('hub.html', user=current_user, history=gens)

@app.route('/history/<int:gen_id>')
@login_required
def history_detail(gen_id):
    gen = Generation.query.get_or_404(gen_id)
    if gen.user_id != current_user.id: return "Unauthorized", 403
    return render_template('history_detail.html', gen=gen)

# --- API Endpoints ---

@app.route('/api/gen/evolink', methods=['POST'])
@login_required
def api_gen_evolink():
    prompt = request.json.get('prompt')
    return jsonify(generate_evolink(prompt))

@app.route('/api/gen/horde/start', methods=['POST'])
@login_required
def api_horde_start():
    prompt = request.json.get('prompt')
    payload = {"prompt": prompt, "params": {"steps": 25, "width": 512, "height": 512}}
    return jsonify(horde_start_gen(payload))

@app.route('/api/gen/horde/check/<id>', methods=['GET'])
def api_horde_check(id):
    return jsonify(horde_check_status(id))

@app.route('/api/save_final', methods=['POST'])
@login_required
def save_final():
    data = request.json
    gen = Generation(
        user_id=current_user.id,
        prompt=data.get('prompt'),
        puter_image_url=data.get('puter_url'),
        evolink_image_url=data.get('evolink_url'),
        horde_image_url=data.get('horde_url')
    )
    db.session.add(gen)
    db.session.commit()
    return jsonify({"status": "saved", "id": gen.id})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
