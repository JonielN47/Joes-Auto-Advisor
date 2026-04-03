import os
import json
import requests
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
    """Checks if the 1-hour slot is free on Joe's calendar."""
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
    """Creates the event in Google Calendar and saves to Supabase."""
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
    
    # Save to original bookings table
    supabase.table('bookings').insert({
        "customer_phone": phone,
        "appointment_time": start_time.isoformat(),
        "service_type": summary
    }).execute()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    print("🔔 DOORBELL: Request received.")
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    if not msg: return "OK", 200

    # 1. MEMORY: Save User message
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    except: pass

    # 2. MEMORY: Fetch context (Last 5 messages)
    history_data = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(5).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history_data.data)]

    now = datetime.now(TIMEZONE)
    
    # 3. SYSTEM PROMPT: Enhanced for better lead/booking parsing
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA. 
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL:
    1. Collect Name, Car, and Issue.
    2. Once you have Name/Car/Issue, add this tag: [{LEAD_TAG}: Name | Vehicle | Issue].
    3. To book, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name]
    """

    # 4. CALL AI (Step 3.5 Flash)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    ai_resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "stepfun/step-3.5-flash:free",
            "messages": [{"role": "system", "content": system_prompt}] + chat_history
        }
    ).json()
    
    bot_reply = ai_resp['choices'][0]['message']['content']

    # 5. MEMORY: Save AI reply
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": bot_reply}).execute()
    except: pass

    # --- LOGIC GATEKEEPER ---
    final_reply = bot_reply
    
    if BOOKING_TAG in bot_reply:
        try:
            tag_content = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            time_str, service_name = tag_content.split("|") if "|" in tag_content else (tag_content, "General Service")
            
            req_time = parser.parse(time_str.strip(), fuzzy=True).replace(tzinfo=TIMEZONE)
            
            # 🔧 THE MINUTE ERASER: Force minutes to :00
            req_time = req_time.replace(minute=0, second=0, microsecond=0)
            
            if is_slot_available(req_time):
                create_booking(f"{service_name.strip()} - {num}", req_time, num)
                final_reply = bot_reply.split('[')[0] + f" \n\n✅ Scheduled for {req_time.strftime('%b %d at %I:%M %p')}!"
                # Clear memory after booking
                supabase.table('messages').delete().eq("phone_number", num).execute()
            else:
                final_reply = "I'm sorry, that slot was just taken. Is there another time that works?"
        except: final_reply = bot_reply.split('[')[0]

    elif LEAD_TAG in bot_reply:
        try:
            lead_content = bot_reply.split(f"{LEAD_TAG}: ")[1].split(']')[0].strip()
            parts = [p.strip() for p in lead_content.split("|")]
            
            supabase.table('leads').insert({
                "customer_name": parts[0] if len(parts) > 0 else "Unknown",
                "phone_number": num,
                "vehicle": parts[1] if len(parts) > 1 else "Unknown",
                "issue": parts[2] if len(parts) > 2 else msg
            }).execute()
            print(f"🔥 CLEAN LEAD SAVED: {parts[0]}")
        except: pass
        final_reply = bot_reply.split('[')[0].strip()

    # PING TABLET
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
