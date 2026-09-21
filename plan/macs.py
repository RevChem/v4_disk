import requests
import json
from datetime import datetime

API_URL = "https://mech.chistai.ru/v1/downtime"
API_KEY = "sk_74220500d6ee8ea412496a62fecdfc5f09f8f922"

DATE_FROM = "2026-04-29"
DATE_TO   = "2026-04-30"

response = requests.get(
    API_URL,
    headers={"X-API-Key": API_KEY},
    params={
        "date_from": DATE_FROM,
        "date_to":   DATE_TO,
        "limit":     200,
    }
)

print(json.dumps(response.json(), ensure_ascii=False, indent=2))

lines = []
filename = f"downtime_{DATE_FROM}_{DATE_TO}.md"
with open(filename, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))