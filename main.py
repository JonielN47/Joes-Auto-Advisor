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
try:
    supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))
    print("✅ Supabase Client Connected")
except Exception as e:
    print(f"❌ Supabase Connection Error: {e}")

def get_calendar_service():
    info = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = service_account.Credentials.from_service_account_info(info)
    return build('calendar', 'v3', credentials=creds)

def get_free_slots():
    """Scans the next 10 days for a spread of open slots across the week."""
    service = get_calendar_service()
    now = datetime.now(TIMEZONE)
    slots = []
    
    # Scan 10 days ahead to give the customer a full week of options
    for i in range(11):
        check_date = now + timedelta(days=i)
        if check_date.weekday() >= 5: continue # Skip weekends
        
        # Check slots at 8 AM, 10 AM, 1 PM, and 3 PM to give a variety
        for hour in [8, 10, 13, 15]:
            start = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
            if start < now + timedelta(hours=2): continue # Give Joe 2 hours notice
            
            end = start + timedelta(hours=1)
            events = service.events().list(
                calendarId=os.environ.get('GOOGLE_CALENDAR_ID'),
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True
            ).execute().get('items', [])
            
            if not events:
                slots.append(start.strftime('%a, %b %d at %I:%M %p'))
            if len(slots) >= 6: return slots
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
    print("🔔 DOORBELL: Request received.")
    data = request.json if request.method == 'POST' else request.args
    msg, num = data.get('message', ''), data.get('number', '')
    if not msg: return "OK", 200

    # 1. Memory & Context
    supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    history = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(10).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history.data)]

    # 2. Get Real-Time Availability (Spread across the week)
    available_slots = get_free_slots()
    slots_str = "\n- ".join(available_slots)

    now = datetime.now(TIMEZONE)
    system_prompt = f"""
    You are the Senior Service Advisor for Current Auto Care (30+ yrs experience).
    SHOP: 510 N Reading Rd, Ephrata. HOURS: Mon-Fri 7am-5:30pm.
    CURRENT TIME: {now.strftime('%A, %b %d, %I:%M %p')}

    GOAL:
    1. Help the customer book an appointment. You can book up to 2 weeks in advance.
    2. Collect Name, Year, Make, Model, and Issue.
    3. Once you have info, use: [{LEAD_TAG}: Name | Year | Make | Model | Issue]
    4. To book, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name]
    
    AVAILABILITY EXAMPLES (Offer these, but you can book any Mon-Fri slot):
    - {slots_str}
    
    If they ask for a day not in the examples, check if it's a weekday and suggest a time (8am, 10am, 1pm, 3pm).
    """

    # 3. Call OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    ai_resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": system_prompt}] + chat_history,
            "temperature": 0.5
        },
        timeout=15
    ).json()
    
    bot_reply = ai_resp['choices'][0]['message']['content']
    print(f"💬 AI REPLY: {bot_reply}")

    # 4. The Logic Gatekeeper
    final_reply = clean_tags(bot_reply)

    if BOOKING_TAG in bot_reply:
        try:
            tag = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            time_str, svc = tag.split("|") if "|" in tag else (tag, "Repair")
            req_time = parser.parse(time_str.strip(), fuzzy=True).replace(tzinfo=TIMEZONE).replace(minute=0, second=0)
            create_booking(f"{svc.strip()} - {num}", req_time, num)
            final_reply += f" \n\n✅ You're all set for {req_time.strftime('%b %d at %I:%M %p')}!"
            supabase.table('messages').delete().eq("phone_number", num).execute()
        except Exception as e:
            print(f"❌ BOOKING ERROR: {e}")
            
    elif LEAD_TAG in bot_reply:
        try:
            lead_content = bot_reply.split(f"{LEAD_TAG}: ")[1].split(']')[0].strip()
            p = [i.strip() for i in lead_content.split("|")]
            
            # Robust Parsing: Fill missing parts with "Unknown" so it doesn't crash
            supabase.table('leads').insert({
                "customer_name": p[0] if len(p) > 0 else "Unknown",
                "phone_number": num,
                "year": p[1] if len(p) > 1 else "Unknown",
                "make": p[2] if len(p) > 2 else "Unknown",
                "model": p[3] if len(p) > 3 else "Unknown",
                "issue": p[4] if len(p) > 4 else msg
            }).execute()
            print(f"🔥 LEAD SAVED: {p[0]}")
        except Exception as e:
            print(f"❌ LEAD SAVE ERROR: {e}")

    # 5. Final Send
    supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": final_reply}).execute()
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
