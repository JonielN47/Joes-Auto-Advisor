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
supabase = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

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
        'description': f'Customer: {phone}\nScheduled by AI Bot',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
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

    # 1. SAVE USER MESSAGE TO MEMORY
    supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()

    # 2. FETCH CONVERSATION HISTORY (Last 5 messages)
    history_data = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(5).execute()
    
    # Format history for the AI (reverse it so it's in chronological order)
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history_data.data)]

    now = datetime.now(TIMEZONE)
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA. 
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL: Collect Name, Car, and Issue. Use [{LEAD_TAG}] once you have them.
    To book, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS].
    """

    # 3. CALL AI WITH MEMORY
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

    # 4. SAVE AI REPLY TO MEMORY
    supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": bot_reply}).execute()

    # --- LOGIC GATEKEEPER ---
    final_reply = bot_reply
    if BOOKING_TAG in bot_reply:
        try:
            time_part = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            req_time = parser.parse(time_part, fuzzy=True).replace(tzinfo=TIMEZONE)
            if is_slot_available(req_time):
                create_booking(f"Appt for {num}", req_time, num)
                final_reply = bot_reply.split('[')[0] + f" \n\n✅ Scheduled for {req_time.strftime('%b %d at %I:%M %p')}!"
                # CLEAR MEMORY AFTER BOOKING (Optional: Per your request to keep it light)
                supabase.table('messages').delete().eq("phone_number", num).execute()
            else:
                final_reply = "That slot is taken. Is there another time?"
        except: pass
    elif LEAD_TAG in bot_reply:
        # Note: Ensure column 'issue' exists in your Supabase 'leads' table!
        try:
            supabase.table('leads').insert({"phone_number": num, "issue": msg}).execute()
        except Exception as e:
            print(f"❌ SUPABASE LEAD ERROR: {e}")
        final_reply = bot_reply.replace(f"[{LEAD_TAG}]", "")

    # PING TABLET
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200
