import azure.functions as func
import logging
import os
import requests
import google.generativeai as genai
from azure.communication.email import EmailClient

app = func.FunctionApp()

# --- CONFIGURATION ---
# The timer is set to run daily at 07:00 UTC
@app.timer_trigger(schedule="0 0 7 * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False)
def wijk_multi_source_scout(myTimer: func.TimerRequest) -> None:
    logging.info("🏄 Scout woke up. Evaluating conditions...")

    # 1. FETCH OPEN-METEO DATA
    # (Coordinates for Wijk aan Zee / IJmuiden)
    meteo_url = "https://marine-api.open-meteo.com/v1/marine?latitude=52.49&longitude=4.58&hourly=wave_height,wave_direction,wave_period&timezone=Europe%2FAmsterdam"
    
    try:
        resp = requests.get(meteo_url, timeout=10)
        resp.raise_for_status()
        meteo_data = resp.json()
        
        # Extract today's wave heights (first 24 hours)
        wave_heights = meteo_data["hourly"]["wave_height"][:24]
        
        # Filter out None values just in case the API returns gaps
        valid_heights = [h for h in wave_heights if h is not None]
        max_swell = max(valid_heights) if valid_heights else 0
        
        logging.info(f"Meteo max swell says: {max_swell}m")

        # LAKE LOGIC: If it's completely flat, don't waste AI tokens or send an email
        if max_swell < 0.3:
            logging.info("Open-Meteo says it's basically a lake. Sleeping.")
            return
            
        import json
        meteo_text = json.dumps(meteo_data["hourly"], indent=2)[:4000]

    except Exception as e:
        logging.error(f"Failed to fetch Open-Meteo: {e}")
        return

    # 2. ASK GEMINI
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-3.6-flash")

    # The highly refined prompt with Wijk rules
    prompt = f"""
    You are an expert surf scout for Wijk aan Zee (Noordpier).
    
    SOURCE (Scientific Marine Model):
    {meteo_text}

    WIJK AAN ZEE RULES:
    - The Noordpier blocks South-West (SW) wind. SW is Good. 
    - North-West (NW) wind is Bad (blows directly onshore, creating chop).
    - Swell > 0.6m is strictly needed for rideable waves.
    
    TASK:
    1. Analyze the wave height and wind/period data from the source.
    2. Evaluate the morning and afternoon windows based on the strict Wijk rules.
    
    DECISION:
    - If it looks surfable based on the rules, start your response EXACTLY with "GO SURF" or "WORTH CHECKING".
    - If it is flat or a blown-out mess, start EXACTLY with "STAY HOME".
    - Provide a punchy summary of the conditions to justify your decision.
    """

    try:
        response = model.generate_content(prompt)
        ai_verdict = response.text
        logging.info("Gemini Analysis Complete.")
    except Exception as e:
        logging.error(f"Gemini Error: {e}")
        return

    # 3. SEND EMAIL (Only if the AI thinks it's worth it)
    if "GO SURF" in ai_verdict.upper() or "WORTH CHECKING" in ai_verdict.upper():
        logging.info("Conditions look good! Sending alert email...")
        try:
            connection_string = os.environ.get("COMMUNICATION_SERVICES_CONNECTION_STRING")
            email_client = EmailClient.from_connection_string(connection_string)
            
            message = {
                "senderAddress": "DoNotReply@ab3b6e80-d6e4-411f-8ba5-3db64f43407c.azurecomm.net",
                "recipients": {
                    "to": [{"address": os.environ.get("TO_EMAIL")}]
                },
                "content": {
                    "subject": "🌊 Wijk aan Zee Surf Alert",
                    "plainText": ai_verdict
                }
            }
            
            poller = email_client.begin_send(message)
            poller.result()
            logging.info("Email sent successfully.")
            
        except Exception as e:
            logging.error(f"Email failed: {e}")
    else:
        logging.info(f"AI decided it is not worth surfing today. Verdict: {ai_verdict[:50]}...")