import os
import json
import time
import requests
import smtplib
import stripe
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

# Load .env only if running locally (Render sets these in the dashboard)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

# --- Configuration ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
OWNER_EMAIL = os.getenv("GMAIL_USER")
DOMAIN_URL = os.getenv("DOMAIN_URL", "http://127.0.0.1:8080")

# --- Database Config (PostgreSQL for Render) ---
# Render provides 'DATABASE_URL', but SQLAlchemy requires 'postgresql://' not 'postgres://'
db_url = os.getenv("DATABASE_URL", "sqlite:///gamelore.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
stripe.api_key = STRIPE_SECRET_KEY

# --- Database Models ---
class User(db.Model):
    email = db.Column(db.String(120), primary_key=True)
    credits = db.Column(db.Integer, default=1) 
    sub_status = db.Column(db.String(20), default='none')
    history = db.Column(db.Text, default='[]') 
    ref_code = db.Column(db.String(50), nullable=True)
    referrer = db.Column(db.String(120), nullable=True)

# --- Helper Functions ---
def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Email Error: {e}")
        return False

def call_groq_api(content_type, genre, user_details):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        f"You are a professional game writer. Generate an engaging, original {content_type} "
        f"in {genre} style based on: {user_details}. Keep it under 800 words."
    )

    data = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }

    retries = 3
    for attempt in range(retries):
        try:
            response = requests.post(url, headers=headers, json=data)
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content']
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1)
            
    return None

# --- Routes ---
@app.route('/')
def home():
    if request.args.get('ref'):
        session['ref_code'] = request.args.get('ref')
    if 'user_email' in session:
        return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/signup', methods=['POST'])
def signup():
    email = request.form.get('email').lower().strip()
    if not email: return redirect(url_for('home'))

    user = User.query.get(email)
    if not user:
        new_ref = email.split('@')[0]
        user = User(email=email, ref_code=new_ref, referrer=session.get('ref_code'))
        db.session.add(user)
        db.session.commit()
    
    session['user_email'] = user.email
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.pop('user_email', None)
    return redirect(url_for('home'))

@app.route('/dashboard')
def dashboard():
    if 'user_email' not in session: return redirect(url_for('home'))
    user = User.query.get(session['user_email'])
    return render_template('dashboard.html', user=user, history=json.loads(user.history))

@app.route('/generate', methods=['GET', 'POST'])
def generate():
    if 'user_email' not in session: return redirect(url_for('home'))
    user = User.query.get(session['user_email'])

    if request.method == 'POST':
        if user.sub_status != 'active' and user.credits <= 0:
            flash("No credits left", "warning")
            return redirect(url_for('payment'))

        content_type = request.form.get('content_type')
        genre = request.form.get('genre')
        details = request.form.get('user_details')

        result = call_groq_api(content_type, genre, details)

        if result:
            if user.sub_status != 'active':
                user.credits -= 1
            
            hist = json.loads(user.history)
            hist.insert(0, {
                "type": content_type,
                "preview": result[:50] + "...",
                "full_text": result,
                "date": datetime.now().strftime("%Y-%m-%d")
            })
            user.history = json.dumps(hist)
            db.session.commit()
            
            send_email(user.email, f"GameLore AI: {content_type}", result.replace('\n', '<br>'))
            flash("Generated!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("AI busy, try again.", "danger")

    return render_template('generate.html', user=user)

@app.route('/payment')
def payment():
    if 'user_email' not in session: return redirect(url_for('home'))
    return render_template('payment.html', key=STRIPE_PUBLISHABLE_KEY)

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data = json.loads(request.data)
    price_id = os.getenv("STRIPE_PRICE_ID_PACK") if data.get('type') == 'pack' else os.getenv("STRIPE_PRICE_ID_SUB")
    mode = 'payment' if data.get('type') == 'pack' else 'subscription'
    
    try:
        session_stripe = stripe.checkout.Session.create(
            customer_email=session['user_email'],
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode=mode,
            success_url=DOMAIN_URL + '/dashboard?payment=success',
            cancel_url=DOMAIN_URL + '/payment?payment=cancelled',
            metadata={'type': data.get('type'), 'email': session['user_email']}
        )
        return jsonify(id=session_stripe.id)
    except Exception as e:
        return jsonify(error=str(e)), 403

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.get_data(as_text=True)
    sig = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return 'Error', 400

    if event['type'] == 'checkout.session.completed':
        session_data = event['data']['object']
        email = session_data['metadata'].get('email')
        p_type = session_data['metadata'].get('type')
        
        user = User.query.get(email)
        if user:
            if p_type == 'pack': user.credits += 5
            elif p_type == 'sub': user.sub_status = 'active'
            db.session.commit()

    return 'Success', 200

# FAQ and TOS routes
@app.route('/faq')
def faq(): return render_template('faq.html')
@app.route('/tos')
def tos(): return render_template('tos.html')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # For local testing only. Render uses Gunicorn command.
    app.run(host='0.0.0.0', port=5000)
