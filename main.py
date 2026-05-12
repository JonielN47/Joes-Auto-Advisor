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
DATA_TAG = "INTERNAL_DATA"

# Initialize Supabase
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def get_calendar_service():
    info = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = service_account.Credentials.from_service_account_info(info)
    return build('calendar', 'v3', credentials=creds)

def get_free_slots():
    """Scans next 10 days and picks a morning, mid-day, and afternoon slot for variety."""
    service = get_calendar_service()
    now = datetime.now(TIMEZONE)
    slots = []
    
    for i in range(1, 11): # Start from tomorrow to give Mel time to prep
        check_date = now + timedelta(days=i)
        if check_date.weekday() == 6: continue # Skip Sundays
        
        # Mel's Hours: Mon-Fri 9-5, Sat 9-2
        end_hour = 17 if check_date.weekday() < 5 else 14
        day_free_hours = []

        for hour in range(9, end_hour):
            start = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=1)
            
            events = service.events().list(
                calendarId=os.environ.get('GOOGLE_CALENDAR_ID'),
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True
            ).execute().get('items', [])
            
            if not events:
                day_free_hours.append(start.strftime('%a, %b %d at %I:%M %p'))
        
        # Pick Variety: First available, middle of day, and last available
        if day_free_hours:
            if len(day_free_hours) <= 3:
                slots.extend(day_free_hours)
            else:
                slots.append(day_free_hours[0]) # Morning
                slots.append(day_free_hours[len(day_free_hours)//2]) # Mid-day
                slots.append(day_free_hours[-1]) # Afternoon
            
        if len(slots) >= 15: break # Cap the list for AI readability
            
    return slots

def create_booking(summary, start_time, phone):
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    event = {
        'summary': f"🚗 {summary}",
        'location': '101 Franklin St, West Reading, PA 19611',
        'description': f'Customer: {phone}\nBerks Digital Partners AI Booking',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
    supabase.table('bookings').insert({
        "customer_phone": phone, "appointment_time": start_time.isoformat(), "service_type": summary
    }).execute()

def clean_tags(text):
    text = re.sub(r'\[CONFIRMED_BOOKING:.*?\]', '', text)
    text = re.sub(r'\[INTERNAL_DATA:.*?\]', '', text)
    return text.strip()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    data = request.json if request.method == 'POST' else request.args
    msg, num = data.get('message', ''), data.get('number', '')
    if not msg: return "OK", 200

    # 1. Store Message for Context
    supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    
    # 2. Get Context & Variety Openings
    history = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(10).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history.data)]
    available_slots = get_free_slots()
    slots_str = "\n- ".join(available_slots)

    now = datetime.now(TIMEZONE)
    system_prompt = f"""
    You are the Professional Service Advisor for Mel's Auto Service LLC.
    SHOP: 101 Franklin St, West Reading, PA 19611.
    HOURS: Mon-Fri 9am-5pm, Sat 9am-2pm. (Closed Sundays).
    CURRENT TIME: {now.strftime('%A, %b %d, %I:%M %p')}

    PERSONA:
    - You represent Mel. You are professional, honest, and helpful.
    - We specialize in PA State Inspections, Emissions, Brakes, and Repairs.
    - Treat customers like family. Be concise.

    INSTRUCTIONS:
    1. Every time you learn info (Name, Year, Make, Model, Issue), you MUST include:
       [{DATA_TAG}: Name | Year | Make | Model | Issue]
       Use 'None' for missing values.
    2. Suggest from these varied openings (Morning, Mid-Day, and Afternoon included):
       - {slots_str}
    3. You can book ANY week/Sat hour within business hours up to 10 days out.
    4. To finalize a booking, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name]
    """

    # 3. Call OpenAI GPT-4o-mini
    api_key = os.environ.get("OPENAI_API_KEY")
    ai_resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": system_prompt}] + chat_history,
            "temperature": 0.4
        }
    ).json()
    
    bot_reply = ai_resp['choices'][0]['message']['content']

    # 4. Handle Incremental Data (Lead Tracking)
    if DATA_TAG in bot_reply:
        try:
            raw_data = bot_reply.split(f"{DATA_TAG}: ")[1].split(']')[0].strip()
            p = [i.strip() for i in raw_data.split("|")]
            lead_payload = {
                "phone_number": num,
                "customer_name": p[0] if p[0] != "None" else None,
                "year": p[1] if p[1] != "None" else None,
                "make": p[2] if p[2] != "None" else None,
                "model": p[3] if p[3] != "None" else None,
                "issue": p[4] if p[4] != "None" else None
            }
            lead_payload = {k: v for k, v in lead_payload.items() if v is not None}
            supabase.table('leads').upsert(lead_payload, on_conflict='phone_number').execute()
        except: pass

    # 5. Handle Final Booking
    final_reply = clean_tags(bot_reply)
    if BOOKING_TAG in bot_reply:
        try:
            tag = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            time_str, svc = tag.split("|") if "|" in tag else (tag, "Repair")
            req_time = parser.parse(time_str.strip(), fuzzy=True).replace(tzinfo=TIMEZONE).replace(minute=0, second=0)
            create_booking(f"{svc.strip()} - {num}", req_time, num)
            final_reply += f" \n\n✅ I've got you down for {req_time.strftime('%b %d at %I:%M %p')} at 101 Franklin St!"
            # Clear chat history so next time is a fresh start
            supabase.table('messages').delete().eq("phone_number", num).execute()
        except: pass

    # 6. Save & Push SMS via MacroDroid
    supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": final_reply}).execute()
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
