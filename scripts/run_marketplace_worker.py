from dotenv import load_dotenv
load_dotenv(".env")

import time

from app.services.marketplace_order_ingestion import worker_loop

print("BrooksHouse marketplace worker starting...", flush=True)

# A reboot can leave the previous worker's durable singleton lock alive
# for up to 120 seconds. Give it time to expire before claiming it.
print("Waiting 150 seconds for any previous worker lock to expire...", flush=True)
time.sleep(150)

print("Starting marketplace worker loop...", flush=True)
worker_loop()
