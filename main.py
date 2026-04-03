import os
import json
import requests
import re
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
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def get_calendar_service():
    info = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = service_account.Credentials.from_service_account_info(info)
    return build('calendar', 'v3', credentials=creds)

def get_free_slots():
    """Scans the next 3 business days for 5 open 1-hour slots."""
    service = get_calendar_service()
    now = datetime.now(TIMEZONE)
    slots = []
    
    # Check next 3 days
    for i in range(4):
        check_date = now + timedelta(days=i)
        if check_date.weekday() >= 5: continue # Skip weekends
        
        # Check hourly from 7 AM to 4 PM (to end by 5:30)
        for hour in range(7, 17):
            start = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
            if start < now: continue
            
            end = start + timedelta(hours=1)
            events = service.events().list(
                calendarId=os.environ.get('GOOGLE_CALENDAR_ID'),
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True
            ).execute().get('items', [])
            
            if not events:
                slots.append(start.strftime('%a, %b %d at %I:%M %p'))
            if len(slots) >= 5: return slots
    return slots

def create_booking(summary, start_time, phone):
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    event = {
        'summary': f"🚗 {summary}",
        'location': '510 North Reading Road, Ephrata, PA 17522',
        'description': f'Customer: {phone}\nSenior AI Booking',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
    supabase.table('bookings').insert({
        "customer_phone": phone, "appointment_time": start_time.isoformat(), "service_type": summary
    }).execute()

def clean_tags(text):
    return re.sub(r'\[.*?\]', '', text).strip()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    data = request.json if request.method == 'POST' else request.args
    msg, num = data.get('message', ''), data.get('number', '')
    if not msg: return "OK", 200

    # 1. Memory & Context
    supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    history = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(8).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history.data)]

    # 2. PROACTIVE SCHEDULING: Get real-time availability
    available_slots = get_free_slots()
    slots_str = "\n- ".join(available_slots)

    now = datetime.now(TIMEZONE)
    system_prompt = f"""
    You are the Senior Service Advisor for Current Auto Care (30+ yrs experience).
    SHOP: 510 N Reading Rd, Ephrata. HOURS: Mon-Fri 7am-5:30pm.
    CURRENT TIME: {now.strftime('%A, %b %d, %I:%M %p')}

    CURRENT AVAILABILITY (Suggest these to the user):
    - {slots_str}

    TONE: Professional, local PA friendly, and authoritative. 
    GOAL: 
    1. Collect Name, Year/Make/Model, and Issue.
    2. Tag leads: [{LEAD_TAG}: Name | Year | Make | Model | Issue]
    3. Book with: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name]
    
    If they ask for a time not listed, politely offer the closest available slot from the list above.
    """

    # 3. Call OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    ai_resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": system_prompt}] + chat_history,
            "temperature": 0.4
        },
        timeout=15
    ).json()
    
    bot_reply = ai_resp['choices'][0]['message']['content']
    final_reply = clean_tags(bot_reply)

    # 4. Logic Gatekeeper
    if BOOKING_TAG in bot_reply:
        try:
            tag = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            time_str, svc = tag.split("|") if "|" in tag else (tag, "Repair")
            req_time = parser.parse(time_str.strip(), fuzzy=True).replace(tzinfo=TIMEZONE).replace(minute=0, second=0)
            create_booking(f"{svc.strip()} - {num}", req_time, num)
            final_reply += f" \n\n✅ You're all set for {req_time.strftime('%b %d at %I:%M %p')}!"
            supabase.table('messages').delete().eq("phone_number", num).execute()
        except: pass
    elif LEAD_TAG in bot_reply:
        try:
            p = [i.strip() for i in bot_reply.split(f"{LEAD_TAG}: ")[1].split(']')[0].split("|")]
            supabase.table('leads').insert({
                "customer_name": p[0], "phone_number": num, "year": p[1], "make": p[2], "model": p[3], "issue": p[4]
            }).execute()
        except: pass
        final_reply = bot_reply.split('[')[0].strip()

    supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": final_reply}).execute()
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
