import os
from flask import Flask, request
from supabase import create_client
import requests
import json

app = Flask(__name__)

# Connect to Supabase
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

@app.route('/')
def home():
    return "Joe's Auto Bot is Online!"

@app.route('/sms', methods=['POST'])
def handle_sms():
    msg = request.form.get('Body', '').lower()
    num = request.form.get('From', '')

    # 1. Simple Saturday Block
    if "saturday" in msg:
        return "<Response><Message>Joe is closed Saturdays! Pick a weekday?</Message></Response>"

    # 2. Talk to the Brain (OpenRouter)
    prompt = "You are Joe's AI Advisor. Get Name, Vehicle, Issue. If all 3 are here, say CONFIRMED."
    
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY')}"},
        data=json.dumps({
            "model": "stepfun/step-3-5-flash:free",
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": msg}]
        })
    )
    
    bot_reply = response.json()['choices'][0]['message']['content']
    return f"<Response><Message>{bot_reply}</Message></Response>"
