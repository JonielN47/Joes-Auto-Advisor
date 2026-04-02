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
    
    # 1. Update Google Calendar
    event = {
        'summary': f"🚗 {summary}",
        'location': '510 North Reading Road, Ephrata, PA 17522',
        'description': f'Customer: {phone}\nBooked via Current Auto Bot',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
    print(f"📅 Calendar Updated: {summary}")

    # 2. Update Supabase 'bookings' table
    supabase.table('bookings').insert({
        "customer_phone": phone,
        "appointment_time": start_time.isoformat(),
        "service_type": summary
    }).execute()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    print("🔔 DOORBELL: Request received from tablet.")
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    if not msg: return "OK", 200

    # 1. MEMORY: Save User message to Supabase
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "user", "content": msg}).execute()
    except: pass

    # 2. MEMORY: Fetch last 5 messages for context
    history_data = supabase.table('messages').select("role, content").eq("phone_number", num).order("created_at", desc=True).limit(5).execute()
    chat_history = [{"role": h['role'], "content": h['content']} for h in reversed(history_data.data)]

    now = datetime.now(TIMEZONE)
    
    # 3. SYSTEM PROMPT (Tells AI how to use the 'Pipe' for service names)
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA. 
    SHOP INFO: 30+ years experience. 510 N Reading Rd.
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    SERVICES: PA Inspections, Trailer Inspections, Fleet Services, Brakes, AC, Engine Repair.
    
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL:
    1. Collect Name, Car, and Issue.
    2. Add [{LEAD_TAG}] once you have car/issue info.
    3. To book, use exactly: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS | Service Name]
    Example: [{BOOKING_TAG}: 2026-04-05T09:00:00 | Brake Inspection]
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
    print(f"💬 AI REPLY: {bot_reply}")

    # 5. MEMORY: Save AI reply to Supabase
    try:
        supabase.table('messages').insert({"phone_number": num, "role": "assistant", "content": bot_reply}).execute()
    except: pass

    # --- THE IMPROVED LOGIC GATEKEEPER ---
    final_reply = bot_reply
    
    if BOOKING_TAG in bot_reply:
        try:
            # Parse: [CONFIRMED_BOOKING: TIME | SERVICE]
            tag_content = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            
            if "|" in tag_content:
                time_str, service_name = tag_content.split("|")
                time_str = time_str.strip()
                service_name = service_name.strip()
            else:
                time_str = tag_content
                service_name = "General Service"

            req_time = parser.parse(time_str, fuzzy=True).replace(tzinfo=TIMEZONE)
            
            if is_slot_available(req_time):
                create_booking(f"{service_name} - {num}", req_time, num)
                final_reply = bot_reply.split('[')[0] + f" \n\n✅ Scheduled for {req_time.strftime('%b %d at %I:%M %p')}!"
                # Optional: Clear memory after successful booking to keep it fresh
                supabase.table('messages').delete().eq("phone_number", num).execute()
            else:
                final_reply = "I'm sorry, that specific slot was just taken. Is there another time that works for you?"
        except Exception as e:
            print(f"❌ BOOKING ERROR: {e}")
            final_reply = "I'm having a quick look at the calendar—one second while I confirm that time for you."
    
    elif LEAD_TAG in bot_reply:
        try:
            # Save to Supabase 'leads' table (matches column 'issue')
            supabase.table('leads').insert({"phone_number": num, "issue": msg}).execute()
            print(f"🔥 LEAD CAPTURED: {num}")
        except Exception as e:
            print(f"❌ SUPABASE LEAD ERROR: {e}")
        final_reply = bot_reply.replace(f"[{LEAD_TAG}]", "")

    # PING TABLET
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
