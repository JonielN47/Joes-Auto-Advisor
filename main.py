import os
import json
import requests
from flask import Flask, request, jsonify
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. INITIALIZE THE FILING CABINET (SUPABASE) ---
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# --- 2. THE EMPLOYEE HANDBOOK (SYSTEM PROMPT) ---
SYSTEM_PROMPT = """
You are the AI Service Advisor for Joe’s Current Auto Care in Ephrata, PA. 
Goal: Get the customer's Name, Vehicle, and Issue.

STRICT RULES:
1. If you have the Name, Vehicle, and Issue, you MUST output exactly: 'CONFIRMED' followed by 'M_DATE: YYYY-MM-DD HH:mm'.
2. SATURDAY BLOCK: Joe is CLOSED on Saturdays. If a user asks for Saturday, tell them Joe is with family and ask for a weekday.
3. Keep the tone friendly but concise (under 160 characters).
4. If you see 'CONFIRMED' in the conversation, do not ask more questions.
"""

@app.route('/')
def home():
    return "Joe's AI Advisor is Online and Connected to Tablet."

@app.route('/sms', methods=['POST'])
def handle_sms():
    # --- STEP A: RECEIVE DATA FROM TABLET ---
    # MacroDroid sends: {"message": "...", "number": "..."}
    data = request.json
    if not data:
        return "No data received", 400
        
    incoming_msg = data.get('message', '').strip()
    from_number = data.get('number', '').strip()

    print(f"New Message from {from_number}: {incoming_msg}")

    # --- STEP B: CHECK THE FILING CABINET ---
    # See if we already know this phone number
    try:
        user_query = supabase.table("leads").select("*").eq("phone_number", from_number).execute()
        user_data = user_query.data
        
        if user_data:
            customer_name = user_data[0].get('customer_name', 'Unknown')
            vehicle = user_data[0].get('vehicle_info', 'Unknown')
            context = f"Returning customer: {customer_name} (Vehicle: {vehicle})"
        else:
            context = "First-time customer. Need to ask for their name and vehicle."
    except Exception as e:
        print(f"Database Error: {e}")
        context = "New Customer."

    # --- STEP C: ASK THE BRAIN (OPENROUTER) ---
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json"
            },
            data=json.dumps({
                "model": "stepfun/step-3.5-flash:free",
                "messages": [
                    {"role": "system", "content": f"{SYSTEM_PROMPT}\nContext: {context}"},
                    {"role": "user", "content": incoming_msg}
                ]
            })
        )
        
        ai_full_response = response.json()['choices'][0]['message']['content']
        # We split the text to send only the 'Friendly Part' to the customer's phone
        bot_reply = ai_full_response.split('---')[0].strip()
        
    except Exception as e:
        print(f"AI Error: {e}")
        bot_reply = "Hey, it's Joe's Auto. We're having a quick tech glitch, but I'll get back to you shortly!"

    # --- STEP D: SEND REPLY TO TABLET (MACRODROID) ---
    # We send the 'number' and 'text' as query parameters to your Webhook URL
    macrodroid_base_url = os.environ.get("MACRODROID_URL")
    
    if macrodroid_base_url:
        payload = {
            "number": from_number,
            "text": bot_reply
        }
        # This triggers the 'Send AI Reply' macro on your tablet
        requests.get(macrodroid_base_url, params=payload)
        return "Success", 200
    else:
        return "MacroDroid URL Missing", 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
