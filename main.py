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
    """Checks if Joe is already busy during the requested hour."""
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    
    # Google Calendar needs strings for timeMin/timeMax
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
    
    # 1. Create Google Event
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()
    print(f"📅 Google Calendar Event Created for {start_time}")

    # 2. Save to Supabase 'bookings' table
    try:
        supabase.table('bookings').insert({
            "customer_phone": phone,
            "appointment_time": start_time.isoformat(),
            "service_type": summary
        }).execute()
        print("💾 Booking saved to Supabase")
    except Exception as e:
        print(f"❌ Supabase Booking Error: {e}")

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    # Get current time for AI awareness
    now = datetime.now(TIMEZONE)
    
    # --- STEPFUN SYSTEM PROMPT ---
    # I added the Year to 'now' so the AI knows exactly what year it is
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA. 
    SHOP INFO: 30+ years experience. 510 N Reading Rd.
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    SERVICES: PA Inspections, Trailer Inspections, Fleet Services, Brakes, AC, Engine Repair.
    
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL:
    1. Collect Name, Car, and Issue.
    2. Add [{LEAD_TAG}] if you have their car/issue info.
    3. To book, use exactly: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS].
    4. ONLY book during business hours. Suggest next business day if they ask for after-hours.
    """

    # Call OpenRouter with Stepfun
    api_key = os.environ.get("OPENROUTER_API_KEY")
    ai_resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "stepfun/step-3.5-flash: free",
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": msg}]
        }
    ).json()
    
    bot_reply = ai_resp['choices'][0]['message']['content']

    # --- THE IMPROVED LOGIC GATEKEEPER ---
    if BOOKING_TAG in bot_reply:
        try:
            # Extract raw time from AI reply
            time_part = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            print(f"🔍 DEBUG: AI requested this time: {time_part}")

            # Parse with 'fuzzy=True' to prevent defaulting to 'now' on minor format errors
            req_time = parser.parse(time_part, fuzzy=True).replace(tzinfo=TIMEZONE)
            print(f"✅ DEBUG: Final Parsed Time for Google: {req_time.isoformat()}")

            if is_slot_available(req_time):
                create_booking(f"Appt for {num}", req_time, num)
                final_reply = bot_reply.split('[')[0] + " \n\n✅ I've got you scheduled for " + req_time.strftime('%b %d at %I:%M %p') + "!"
            else:
                final_reply = "It looks like that specific slot was just taken. Is there another time that works for you?"
        except Exception as e:
            print(f"❌ BOOKING ERROR: {e}")
            final_reply = "I'm having a quick look at the calendar—one second while I confirm that time for you."
    else:
        # Check for Lead Tag and save to Supabase
        if LEAD_TAG in bot_reply:
            try:
                supabase.table('leads').insert({"phone_number": num, "issue": msg}).execute()
                print(f"🔥 LEAD SAVED: {num}")
            except Exception as e:
                print(f"❌ Supabase Lead Error: {e}")
        
        final_reply = bot_reply.replace(f"[{LEAD_TAG}]", "")

    # PING TABLET via MacroDroid
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200

if __name__ == "__main__":
    # Ensure Render can find the port
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
