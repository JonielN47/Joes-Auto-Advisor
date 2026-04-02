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
    print("✅ Supabase Client Connected and Ready")
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
    # --- LOG 1: THE DOORBELL ---
    print("🔔 DOORBELL: The tablet successfully reached the Render server!")
    
    data = request.json if request.method == 'POST' else request.args
    msg = data.get('message', '')
    num = data.get('number', '')
    
    if not msg:
        print("⚠️ WARNING: Received a request but the message was empty.")
        return "Empty message", 200

    now = datetime.now(TIMEZONE)
    
    system_prompt = f"""
    You are the AI Assistant for Current Auto Care in Ephrata, PA. 
    SHOP INFO: 30+ years experience. 510 N Reading Rd.
    HOURS: Mon-Fri 7:00 AM - 5:30 PM. CLOSED Weekends.
    SERVICES: PA Inspections, Trailer Inspections, Fleet Services, Brakes, AC, Engine Repair.
    
    CURRENT TIME: {now.strftime('%A, %Y-%m-%d %I:%M %p')}
    
    GOAL:
    1. Collect Name, Car, and Issue.
    2. Add [{LEAD_TAG}] if you have car/issue info.
    3. To book, use: [{BOOKING_TAG}: YYYY-MM-DDTHH:MM:SS].
    """

    # --- LOG 2: THE AI CALL ---
    print(f"🧠 AI: Sending request to Step 3.5 Flash for user: {num}")
    
    try:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://render.com", # Required for some free models
                "X-Title": "Current Auto Bot"
            },
            json={
                "model": "stepfun/step-3.5-flash:free", # <--- UPDATED MODEL ID
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": msg}
                ]
            },
            timeout=15 # Don't wait forever if the AI is slow
        )
        
        if response.status_code != 200:
            print(f"❌ AI API ERROR: Status {response.status_code} - {response.text}")
            return "AI Error", 500
            
        bot_reply = response.json()['choices'][0]['message']['content']
        print(f"💬 AI REPLY: {bot_reply}")

    except Exception as e:
        print(f"❌ AI REQUEST CRASHED: {e}")
        return "Internal Error", 500

    # --- THE GATEKEEPER LOGIC ---
    final_reply = bot_reply # Default
    
    if BOOKING_TAG in bot_reply:
        try:
            time_part = bot_reply.split(f"{BOOKING_TAG}: ")[1].split(']')[0].strip()
            req_time = parser.parse(time_part, fuzzy=True).replace(tzinfo=TIMEZONE)
            
            if is_slot_available(req_time):
                create_booking(f"Appt for {num}", req_time, num)
                final_reply = bot_reply.split('[')[0] + f" \n\n✅ Scheduled for {req_time.strftime('%b %d at %I:%M %p')}!"
            else:
                final_reply = "That slot is taken. Is there another time?"
        except Exception as e:
            print(f"❌ CALENDAR ERROR: {e}")
    else:
        if LEAD_TAG in bot_reply:
            try:
                supabase.table('leads').insert({"phone_number": num, "issue": msg}).execute()
                print("💾 LEAD SAVED TO SUPABASE")
            except Exception as e:
                print(f"❌ SUPABASE ERROR: {e}")
        final_reply = bot_reply.replace(f"[{LEAD_TAG}]", "")

    # PING TABLET
    print(f"📱 TABLET: Sending reply back to customer via MacroDroid...")
    requests.get(os.environ.get("MACRODROID_URL"), params={"number": num, "text": final_reply})
    
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
