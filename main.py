import os
import json
import requests
from flask import Flask, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz

app = Flask(__name__)

# --- CONFIGURATION ---
TIMEZONE = pytz.timezone("America/New_York")
BOOKING_TAG = "CONFIRMED_BOOKING"
LEAD_TAG = "LEAD_CAPTURED"

def get_calendar_service():
    # Uses the Secret JSON key you put in Render
    service_account_info = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))
    creds = service_account.Credentials.from_service_account_info(service_account_info)
    return build('calendar', 'v3', credentials=creds)

def is_slot_available(start_time):
    """Checks if Joe is already busy during the requested hour."""
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    
    events_result = service.events().list(
        calendarId=os.environ.get('GOOGLE_CALENDAR_ID'),
        timeMin=start_time.isoformat(),
        timeMax=end_time.isoformat(),
        singleEvents=True
    ).execute()
    
    return len(events_result.get('items', [])) == 0

def create_booking(summary, start_time):
    service = get_calendar_service()
    end_time = start_time + timedelta(hours=1)
    event = {
        'summary': f"🚗 {summary}",
        'location': '510 North Reading Road, Ephrata, PA 17522',
        'description': 'Scheduled by Current Auto Bot',
        'start': {'dateTime': start_time.isoformat(), 'timeZone': 'America/New_York'},
        'end': {'dateTime': end_time.isoformat(), 'timeZone': 'America/New_York'},
    }
    service.events().insert(calendarId=os.environ.get('GOOGLE_CALENDAR_ID'), body=event).execute()

@app.route('/sms', methods=['POST', 'GET'])
def handle_sms():
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    # Get current time for the AI's awareness
    now = datetime.now(TIMEZONE)
    
    # --- STEPFUN BRAIN PROMPT ---
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA.
    SHOP INFO: 30+ years experience. Located at 510 N Reading Rd.
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    SERVICES: PA Inspections, Trailer Inspections, Fleet Services, Brakes, AC, Engine Repair.
    
    CURRENT TIME: {now.strftime('%A, %I:%M %p')}
    
    GOAL: 
    1. Collect Name, Car, and Issue.
    2. If you have Name/Car/Issue, add [{LEAD_TAG}] to your internal log.
    3. To book, use exactly: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS].
    4. ONLY book during Mon-Fri 7:00 AM - 5:30 PM. If they ask for after-hours, suggest the next morning.
    """

    # --- OPENROUTER CALL (Using Stepfun) ---
    api_key = os.environ.get("OPENROUTER_API_KEY")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "stepfun/step-1.5p", # Your requested Stepfun brain
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": msg}
            ]
        }
    )
    
    bot_reply = response.json()['choices'][0]['message']['content']

    # --- THE GATEKEEPER LOGIC ---
    if BOOKING_TAG in bot_reply:
        try:
            # Extract the ISO time from the AI's response
            time_str = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0]
            req_time = datetime.fromisoformat(time_str).replace(tzinfo=TIMEZONE)
            
            # CHECK FOR DOUBLE BOOKING
            if is_slot_available(req_time):
                create_booking(f"Appt: {num}", req_time)
                final_reply = bot_reply.split('[')[0] + " \n\n✅ I've got you scheduled! See you at 510 N Reading Rd."
            else:
                final_reply = "I'm sorry, that specific time was just taken! Is there another slot between 7:00 and 5:30 that works for you?"
        except Exception as e:
            print(f"Booking Error: {e}")
            final_reply = "I'm having a quick look at the calendar—one second while I confirm that time for you."
    else:
        final_reply = bot_reply.replace(f"[{LEAD_TAG}]", "")
        if LEAD_TAG in bot_reply:
            print(f"🔥 LEAD CAPTURED: {num} for {msg}")

    # PING THE TABLET
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    return "OK", 200
