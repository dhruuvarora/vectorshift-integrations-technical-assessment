import json
import secrets
import base64
import requests
import httpx

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse

from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = "YOUR_HUBSPOT_CLIENT_ID"
REDIRECT_URI = "http://localhost:8000/integrations/hubspot/oauth2callback"
SCOPE = "crm.objects.contacts.read"
CLIENT_SECRET = "YOUR_HUBSPOT_CLIENT_SECRET"

async def authorize_hubspot(user_id, org_id):
    state_data = {
        "state": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id
    }
    # encode the state data
    encoded_state = base64.urlsafe_b64encode(
        json.dumps(state_data).encode("utf-8")
    ).decode("utf-8")
    
    # auth_url
    auth_url = (
        "https://app.hubspot.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPE}"
        f"&state={encoded_state}"
    )
    
    await add_key_value_redis(
        f"hubspot_state:{org_id}:{user_id}",
        json.dumps(state_data),
        expire=600,
    )
    
    return auth_url

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400,
            detail=request.query_params.get("error_description", "OAuth error"),
        )

    code = request.query_params.get("code")
    encoded_state = request.query_params.get("state")

    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    state_data = json.loads(
        base64.urlsafe_b64decode(encoded_state).decode("utf-8")
    )

    user_id = state_data.get("user_id")
    org_id = state_data.get("org_id")
    original_state = state_data.get("state")

    saved_state = await get_value_redis(
        f"hubspot_state:{org_id}:{user_id}"
    )

    if not saved_state or original_state != json.loads(saved_state).get("state"):
        raise HTTPException(status_code=400, detail="State does not match")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.hubapi.com/oauth/v1/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Token exchange failed: {response.text}",
        )

    await delete_key_redis(f"hubspot_state:{org_id}:{user_id}")

    await add_key_value_redis(
        f"hubspot_credentials:{org_id}:{user_id}",
        json.dumps(response.json()),
        expire=600,
    )

    close_window_html = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_html)
    

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(
        f"hubspot_credentials:{org_id}:{user_id}"
    )

    if not credentials:
        raise HTTPException(
            status_code=400,
            detail="No HubSpot credentials found",
        )

    credentials = json.loads(credentials)

    await delete_key_redis(
        f"hubspot_credentials:{org_id}:{user_id}"
    )

    return credentials

async def create_integration_item_metadata_object(response_json):
    properties = response_json.get("properties", {})
    name = properties.get("email")
    if not name:
        first = properties.get("firstname", "")
        last = properties.get("lastname", "")
        name = f"{first} {last}".strip() or "Unknown Contact"
        
    return IntegrationItem(
        id=response_json.get("id"),
        type="contact",
        name=name,
        creation_time=response_json.get("createdAt"),
        last_modified_time=response_json.get("updatedAt"),
    )

async def get_items_hubspot(credentials):
    credentials = json.loads(credentials)
    access_token = credentials.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="No access token found in credentials",
        )

    response = requests.get(
        "https://api.hubapi.com/crm/v3/objects/contacts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch HubSpot items: {response.text}",
        )

    results = response.json().get("results", [])

    items = []
    for contact in results:
        item = await create_integration_item_metadata_object(contact)
        items.append(item)

    print("HubSpot Integration Items:")
    print(items)

    return items