import os
import json
import requests
import re
import time
from flask import Flask, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from supabase import create_client
import pytz
from dateutil import parser

app = Flask(__name__)

# --- CONFIGURATION ---
TIMEZONE = pytz.timezone("America/New_York")
BOOKING_TAG = "CONFIRMED_BOOKING"
LEAD_TAG = "LEAD_CAPTURED"

# Initialize Supabase
try:
    supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
    print("✅ Supabase Client Connected")
except Exception as e:
    print(f"❌ Supabase Connection Error: {e}")

def get_calendar_service():
    info = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = service_account.Credentials.from_service_account_info(info)
    return build('calendar', 'v3', credentials=creds)

def is_slot_available(start_time):
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    events_result = service.events().list(
        calendarId=os.environ.get('GOOGLE_CALENDAR_ID'),
        timeMin=start_time.isoformat(),
        timeMax=end_time.isoformat(),
        singleEvents=True
    ).execute()
    return len(events_result.get('items', [])) == 0

def create_booking(summary, start_time, phone):
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    event = {
        'summary': f"🚗 {summary}",
        'location': '510 North Reading Road, Ephrata, PA 17522',
        'description': f'Customer: {phone}\nBooked via Current Auto Bot',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
    supabase.table('bookings').insert({
        "customer_phone": phone,
        "appointment_time": start_time.isoformat(),
        "service_type": summary
    }).execute()

def clean_tags(text):
    return re.sub(r'\[.*?\]', '', text).strip()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    print("🔔 DOORBELL: OpenAI is processing a request...")
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    if not msg: return "OK", 200

    # 1. MEMORY: Save user text
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    except: pass

    # 2. MEMORY: Get context (Last 8 messages)
    history_data = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(8).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history_data.data)]

    now = datetime.now(TIMEZONE)
    system_prompt = f"""
    You are the Senior AI Coordinator for Current Auto Care in Ephrata, PA. 
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL:
    1. Collect Name, Car, and Issue.
    2. Once you have info, include this tag: [{LEAD_TAG}: Name | Vehicle | Issue].
    3. To book, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name].
    """

    # 3. CALL PAID AI (OpenAI GPT-4o-mini via OpenRouter for high speed)
    # This model is 10x faster than the Stepfun free version.
    api_key = os.environ.get("OPENROUTER_API_KEY") 
    bot_reply = ""
    
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "openai/gpt-4o-mini", # <--- THE TURBO UPGRADE
                "messages": [{"role": "system", "content": system_prompt}] + chat_history,
                "temperature": 0.5
            },
            timeout=15
        )
        if response.status_code == 200:
            bot_reply = response.json()['choices'][0]['message']['content']
        else:
            print(f"❌ OpenAI Error: {response.text}")
    except Exception as e:
        print(f"❌ Request Failed: {e}")

    if not bot_reply:
        bot_reply = "I'm having a quick connection issue—could you repeat that?"

    # --- LOGIC GATEKEEPER ---
    final_reply = clean_tags(bot_reply)

    if BOOKING_TAG in bot_reply:
        try:
            tag_content = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            time_str, svc = tag_content.split("|") if "|" in tag_content else (tag_content, "General Service")
            req_time = parser.parse(time_str.strip(), fuzzy=True).replace(tzinfo=TIMEZONE)
            req_time = req_time.replace(minute=0, second=0, microsecond=0) # MINUTE ERASER
            
            if is_slot_available(req_time):
                create_booking(f"{svc.strip()} - {num}", req_time, num)
                final_reply += f" \n\n✅ Confirmed for {req_time.strftime('%b %d at %I:%M %p')}!"
                supabase.table('messages').delete().eq("phone_number", num).execute()
            else:
                final_reply = "I'm sorry, that slot was just taken! Is there another time?"
        except: pass

    elif LEAD_TAG in bot_reply:
        try:
            lead_content = bot_reply.split(f"{LEAD_TAG}: ")[1].split(']')[0].strip()
            p = [i.strip() for i in lead_content.split("|")]
            supabase.table('leads').insert({
                "customer_name": p[0] if len(p)>0 else "Unknown",
                "phone_number": num,
                "vehicle": p[1] if len(p)>1 else "Unknown",
                "issue": p[2] if len(p)>2 else msg
            }).execute()
        except: pass
        final_reply = bot_reply.split('[')[0].strip()

    # 4. MEMORY: Save final reply
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": final_reply}).execute()
    except: pass

    # 5. PING TABLET
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
