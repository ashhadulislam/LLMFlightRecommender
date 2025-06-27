from flask import Flask, request, render_template, jsonify
import os
import json
import requests
from datetime import date
from openai import OpenAI
from supabase import create_client, Client
from flask import Flask, request, render_template, jsonify, session
from datetime import timedelta



app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret")  # must be secret in production!
app.permanent_session_lifetime = timedelta(hours=1)

API_KEY = os.getenv("DEEPSEEK_API_KEY")
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")

# Read from environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Cache token in memory
amadeus_token = None

def get_amadeus_token():
    global amadeus_token
    if amadeus_token:
        return amadeus_token
    url = "https://test.api.amadeus.com/v1/security/oauth2/token"
    payload = {
        'grant_type': 'client_credentials',
        'client_id': AMADEUS_CLIENT_ID,
        'client_secret': AMADEUS_CLIENT_SECRET
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    response = requests.post(url, data=payload, headers=headers)
    if response.status_code == 200:
        amadeus_token = response.json()['access_token']
        return amadeus_token
    return None

def search_flights(token, origin, destination, date, adults=1, max_results=10):
    url = "https://test.api.amadeus.com/v2/shopping/flight-offers"
    headers = {'Authorization': f'Bearer {token}'}
    params = {
        'originLocationCode': origin,
        'destinationLocationCode': destination,
        'departureDate': date,
        'adults': adults,
        'max': max_results
    }
    response = requests.get(url, headers=headers, params=params)
    return response.json() if response.status_code == 200 else None

def enrich_flight_data(raw_response):
    data = raw_response.get('data', [])
    dictionaries = raw_response.get('dictionaries', {})
    enriched_flights = []

    for offer in data:
        offer_id = offer['id']
        price = offer['price']['total']
        currency = offer['price']['currency']
        itinerary = offer['itineraries'][0]
        segments = itinerary['segments']
        duration = itinerary['duration']
        num_stops = len(segments) - 1

        enriched_segments = []
        for segment in segments:
            departure = segment['departure']
            arrival = segment['arrival']
            carrier = segment['carrierCode']
            aircraft_code = segment['aircraft']['code']
            flight_number = segment['number']
            segment_id = segment['id']
            duration_seg = segment['duration']

            aircraft_name = dictionaries.get('aircraft', {}).get(aircraft_code, aircraft_code)
            carrier_name = dictionaries.get('carriers', {}).get(carrier, carrier)

            traveler = offer.get('travelerPricings', [])[0]
            fare_details = traveler.get('fareDetailsBySegment', [])
            segment_fare = next((f for f in fare_details if f['segmentId'] == segment_id), {})
            baggage = segment_fare.get('includedCheckedBags', {})
            cabin_bags = segment_fare.get('includedCabinBags', {})
            amenities = segment_fare.get('amenities', [])

            enriched_segments.append({
                'from': departure['iataCode'],
                'to': arrival['iataCode'],
                'departure_time': departure['at'],
                'arrival_time': arrival['at'],
                'carrier': carrier_name,
                'flight_number': flight_number,
                'aircraft': aircraft_name,
                'duration': duration_seg,
                'baggage_allowance': baggage,
                'cabin_bag': cabin_bags,
                'amenities': [{
                    'description': a['description'],
                    'type': a['amenityType'],
                    'chargeable': a['isChargeable']
                } for a in amenities]
            })

        enriched_flights.append({
            'offer_id': offer_id,
            'total_price': f"{price} {currency}",
            'total_duration': duration,
            'stops': num_stops,
            'segments': enriched_segments
        })
    return enriched_flights

def format_enriched_flights_full(enriched_flights):
    lines = []
    for enrich in enriched_flights:
        header = (
            f"offer_id: {enrich['offer_id']}\n"
            f"From: {enrich['segments'][0]['from']} → To: {enrich['segments'][-1]['to']}\n"
            f"Price: {enrich['total_price']}\n"
            f"Total Duration: {enrich['total_duration']}\n"
            f"Number of Stops: {enrich['stops']}\n"
            f"Segments:"
        )
        segment_lines = []
        for i, seg in enumerate(enrich['segments'], 1):
            baggage = seg['baggage_allowance']
            cabin_bag = seg['cabin_bag']
            amenities = seg['amenities']
            amenity_text = "\n      ".join(
                [f"- {a['description']} (Type: {a['type']}, Chargeable: {a['chargeable']})" for a in amenities])
            segment_lines.append(
                f"  Segment {i}:\n    From: {seg['from']} at {seg['departure_time']}\n    To: {seg['to']} at {seg['arrival_time']}\n    Carrier: {seg['carrier']}\n    Flight Number: {seg['flight_number']}\n    Aircraft: {seg['aircraft']}\n    Duration: {seg['duration']}\n    Checked Baggage: {baggage.get('weight', 'N/A')} {baggage.get('weightUnit', '')}\n    Cabin Bag: {cabin_bag.get('weight', 'N/A')} {cabin_bag.get('weightUnit', '')}\n    Amenities:\n      {amenity_text}"
            )
        lines.append(header + "\n" + "\n".join(segment_lines))
    return "\n\n".join(lines)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST"])
def search():
    data = request.json
    origin = data.get("origin")
    destination = data.get("destination")
    dep_date = data.get("date")

     # Persist in session
    session.permanent = True
    session["origin"] = origin
    session["destination"] = destination
    session["dep_date"] = dep_date

    token = get_amadeus_token()
    if not token:
        return jsonify({"error": "Token error"}), 400
    raw = search_flights(token, origin, destination, dep_date)
    enriched = enrich_flight_data(raw)
    result={}
    result['enriched']=enriched

    return jsonify(result)

@app.route("/recommend", methods=["POST"])
def recommend():
    data = request.json
    enriched = data.get("enriched")
    user_prompt = data.get("prompt")

    # Read persisted info
    origin = session.get("origin")
    destination = session.get("destination")
    dep_date = session.get("dep_date")
    optimized_user_prompt=run_finetunerecoprompt(user_prompt)


    print(user_prompt)
    print('_'*10)
    print(optimized_user_prompt)
    print('-'*10)
    enriched_str = format_enriched_flights_full(enriched)


    system_prompt = f"""
You are an expert travel agent.

You will get a list of travel itineraries and you need to select the best flight as per the user's preference.

Make sure you select the best flight only from the list provided.
Flight options list: {enriched_str}
You only need to return the offer_id of the itinerary and the justification.

Your output must be in JSON format with the following keys:

{{
  "offer_id": "...",  
  "justification": "..."
}}
Do not give anything extra
"""
    #print(system_prompt)    
    print('*'*10)

    api_client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")
    try:        
        response = api_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": optimized_user_prompt}
            ],
            response_format={'type': 'json_object'}
        )

        result = json.loads(response.choices[0].message.content)
        print(result)
        offer_id = int(result['offer_id'])
        print(offer_id)
        selected = next((x for x in enriched if int(x['offer_id']) == offer_id), None)        

        # add data to supabase
        # Initialize Supabase client        
        print('supa client created')
        supa_user_query_data = {
            "departure": origin,
            "arrival": destination,
            "travel_date": dep_date,
            "user_prompt": user_prompt,
            "optimized_user_prompt": optimized_user_prompt
        }
        response = supabase.table("userQueries").insert(supa_user_query_data).execute()

        return jsonify({"recommended": selected, "justification": result.get("justification", "")})
    except Exception as e:
        if 'justification' in result:
            return {"recommended": 'None', "justification": result.get("justification", "")}
        return jsonify({"error": str(e)}), 500

@app.route("/finetunerecoprompt", methods=["POST"])
def serve_finetunerecoprompt():
    data = request.json    
    user_prompt = data.get("prompt")
    optimized_prompt=run_finetunerecoprompt(user_prompt)
    return jsonify({"optimized_prompt": optimized_prompt})


def run_finetunerecoprompt(user_prompt):
    system_prompt = f"""
You are an expert flight search query analyst.
Your task is to go through the users query and enrich their prompt
Infer IMPLICIT parameters from the user query adding more details and conditions that would help a travel agent understnad the need of the traveller
Keep it brief, one paragraph.
"""
    #print(system_prompt)    
    print('*'*10)

    api_client = OpenAI(api_key=API_KEY, base_url="https://api.deepseek.com")
    try:        
        response = api_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            #response_format={'type': 'json_object'}
        )

        optimized_prompt = response.choices[0].message.content
        
        return optimized_prompt
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/user-queries')
def get_user_queries():
    response = supabase.table("userQueries").select("*").execute()
    return jsonify(response.data)


@app.route('/show-user-queries')
def show_user_queries():
    return render_template("queries.html")


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)