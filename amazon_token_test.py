import os
from pathlib import Path
import requests

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()

    if not line or line.startswith("#") or "=" not in line:
        continue

    key, value = line.split("=", 1)
    os.environ[key.strip()] = value.strip().strip('"').strip("'")

response = requests.post(
    "https://api.amazon.com/auth/o2/token",
    data={
        "grant_type": "refresh_token",
        "refresh_token": os.getenv("AMAZON_REFRESH_TOKEN"),
        "client_id": os.getenv("AMAZON_LWA_CLIENT_ID"),
        "client_secret": os.getenv("AMAZON_LWA_CLIENT_SECRET"),
    },
    timeout=30,
)

print("STATUS:", response.status_code)
print(response.text)
