import logging
import os
import requests
import azure.functions as func
from azure.communication.email import EmailClient
import google.generativeai as genai
from bs4 import BeautifulSoup

app = func.FunctionApp()

# --- CONFIGURATION ---
LAT, LON = 52.493, 4.577 # Wijk aan Zee
FROM_EMAIL = "DoNotReply@ec88a152-9e95-4360-a3fd-bda7e3e04908.azurecomm.net" 

@app.schedule(schedule="0 0 7 * * *", arg_name="myTimer", run_on_startup=False) 
def wijk_multi_source_scout(myTimer: func.TimerRequest) -> None:
    logging.info('🏄 Scout woke up. Getting multiple opinions...')
    
    acs_conn = os.environ.get("COMMUNICATION_SERVICES_CONNECTION_STRING")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    to_email = os.environ.get("TO_EMAIL")

    if not acs_conn or not gemini_key or not to_email:
        logging.error("Missing Environment Variables! Check ACS, Gemini, or TO_EMAIL.")
        return

    # ---------------------------------------------------------
    # SOURCE 1: Open-Meteo 
    # ---------------------------------------------------------
    url_meteo = (
        f"https://marine-api.open-meteo.com/v1/marine?"
        f"latitude={LAT}&longitude={LON}&"
        f"hourly=wave_height,swell_wave_height,swell_wave_period,wind_speed_10m,wind_direction_10m&"
        f"timezone=Europe%2FBerlin&forecast_days=1"
    )
    
    try:
        data_meteo = requests.get(url_meteo, timeout=10).json()['hourly']
        day_slice = slice(7, 19)
        max_swell_meteo = max(data_meteo['swell_wave_height'][day_slice])
    except Exception as e:
        logging.error(f"Open-Meteo Failed: {e}")
        max_swell_meteo = 0
        data_meteo = None

# ---------------------------------------------------------
    # SOURCE 2: SurfVoorspelling.nl (Zandvoort API Proxy)
    # ---------------------------------------------------------
    url_local = "https://www.surfvoorspelling.nl/api/forecast.php?spot=zandvoort"
    local_site_text = "Source unavailable."
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        }
        resp = requests.get(url_local, headers=headers, timeout=8)
        
        if resp.status_code == 200:
            # Pass clean, formatted JSON data directly to Gemini
            data = resp.json()
            import json
            local_site_text = json.dumps(data, indent=2)[:4000]
            logging.info("Successfully fetched Zandvoort forecast API data.")
        else:
            logging.warning(f"Zandvoort API returned status: {resp.status_code}")
            local_site_text = f"API online but returned status code {resp.status_code}."
            
    except Exception as e:
        logging.error(f"API request failed: {e}")
        local_site_text = "Zandvoort API endpoint is unreachable."

    # ---------------------------------------------------------
    # DECISION TIME
    # ---------------------------------------------------------
    logging.info(f"Meteo max swell says: {max_swell_meteo}m")
    
    if max_swell_meteo < 0.3: 
        logging.info("Open-Meteo says it's basically a lake. Sleeping.")
        return
        
    if data_meteo is None:
        logging.error("No scientific data available. Aborting.")
        return

    # ---------------------------------------------------------
    # GEMINI: THE JUDGE
    # ---------------------------------------------------------
    analysis = ask_gemini_judge(data_meteo, local_site_text, gemini_key)

    if "GO SURF" in analysis.upper() or "WORTH CHECKING" in analysis.upper():
        send_email(analysis, acs_conn, to_email)
    else:
        logging.info("Gemini reviewed both sources but didn't think it was worth a session.")

def ask_gemini_judge(meteo_data, site_text, api_key):
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-3.6-flash')
        
        meteo_summary = "Open-Meteo Forecast (Hour | Swell | Wind):\n"
        for i in range(7, 19):
             meteo_summary += f"{meteo_data['time'][i][-5:]} | {meteo_data['swell_wave_height'][i]}m | {meteo_data['wind_direction_10m'][i]}deg\n"

        prompt = f"""
        You are an expert surf scout for Wijk aan Zee (Noordpier).
        
        SOURCE 1 (Scientific Model):
        {meteo_summary}

        SOURCE 2 (Local Website Scrape):
        {site_text}

        TASK:
        1. Analyze Source 1 (Meteo).
        2. Read Source 2. NOTE: If Source 2 says "unavailable", IGNORE IT and rely on Source 1.
        3. If Source 2 contains valid text, check if it mentions higher waves than Source 1.
        
        WIJK RULES:
        - Pier blocks SW wind (Good). NW is Bad.
        - Swell > 0.6m is usually needed.
        
        DECISION:
        - If it looks surfable based on EITHER source, start with "GO SURF" or "WORTH CHECKING".
        - If it's flat/messy, say "STAY HOME".
        """
        
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Gemini Error: {e}")
        return "WORTH CHECKING (AI failed, but wave thresholds met!)"

def send_email(body, conn_string, recipient_email):
    try:
        client = EmailClient.from_connection_string(conn_string)
        message = {
            "senderAddress": FROM_EMAIL,
            "recipients":  { "to": [{"address": recipient_email }] },
            "content": {
                "subject": "🏄 Wijk Multi-Source Surf Alert",
                "plainText": body,
                "html": f"<html><h3>🏄 Wijk Forecast</h3><p style='white-space: pre-line'>{body}</p></html>"
            }
        }
        client.begin_send(message)
        logging.info(f"Email sent successfully.")
    except Exception as e:
        logging.error(f"Email Failed: {e}")