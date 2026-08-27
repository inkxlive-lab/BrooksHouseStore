import json
from app.services.marketplace_order_ingestion import run_sync_cycle

result = run_sync_cycle(("walmart", "amazon"))
print(json.dumps(result, default=str))
