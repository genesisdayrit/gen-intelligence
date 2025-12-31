import os
import requests
import redis
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Dropbox credentials from the .env file
client_id = os.getenv('DROPBOX_ACCESS_KEY')
client_secret = os.getenv('DROPBOX_ACCESS_SECRET')
refresh_token = os.getenv('DROPBOX_REFRESH_TOKEN')

# Get Redis configuration from environment variables
redis_host = os.getenv('REDIS_HOST', 'localhost')  # Default to 'localhost' if not set
redis_port = int(os.getenv('REDIS_PORT', 6379))    # Default to 6379 if not set
redis_password = os.getenv('REDIS_PASSWORD', None)  # Default to None if not set

# Connect to Redis using the environment variables
r = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)

def refresh_access_token():
    url = 'https://api.dropbox.com/oauth2/token'
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': client_id,
        'client_secret': client_secret
    }

    response = requests.post(url, data=data)

    if response.status_code == 200:
        response_data = response.json()
        access_token = response_data.get('access_token')
        expires_in = response_data.get('expires_in')

        print(f"New Access Token: {access_token}")
        print(f"Expires In: {expires_in} seconds")

        # Store the access token in Redis with an expiration time
        r.set('DROPBOX_ACCESS_TOKEN', access_token, ex=expires_in)
        return access_token
    else:
        print(f"Error: {response.status_code} - {response.content}")
        return None

if __name__ == "__main__":
    new_access_token = refresh_access_token()
    if new_access_token:
        print("Access token refresh was successful and stored in Redis.")
    else:
        print("Access token refresh failed.")
