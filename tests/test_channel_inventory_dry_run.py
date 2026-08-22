import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.migrations.channel_inventory_engine_schema import apply_to_copy
from app.services.channel_inventory_controls import set_copy_control
from app.services.channel_inventory_dry_run import build_dry_run
from tests.test_channel_inventory_preflight import SCHEMA


class ChannelInventoryDryRunTests(unittest.TestCase):
    def test_dry_run_is_read_only_and_control_policy_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/"copy.db"
            with closing(sqlite3.connect(db)) as connection:
                connection.executescript(SCHEMA)
                connection.execute("INSERT INTO products VALUES(10)")
                connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)",[(1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage")])
                connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)",[(1,10,1,"F",1,0,0,"x"),(2,10,2,"B",2,0,0,"x")])
                connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-21','2026-08-21','2026-08-21',NULL,'PAID','UNFULFILLED',0)")
                connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',10,'SKU','Widget',3,3,0,'matched','v1')")
                connection.commit()
            apply_to_copy(db)
            for scope in ("global","shopify"):
                set_copy_control(db,scope,mode="dry_run",paused=False,cutover_at="2026-08-20",source_checkpoint="2026-08-20",reason="test",
                                 allocation_policy="ordered_multi_location")
            before = db.read_bytes()
            report = build_dry_run(db,"2026-08-20")
            after = db.read_bytes()
            self.assertEqual(before,after)
            row = report["rows"][0]
            self.assertEqual((row["would_deduct"],row["allocation_policy"]),(3,"ordered_multi_location"))
            self.assertEqual([item["inventory_id"] for item in row["physical_inventory_rows"]],[1,2])


if __name__ == "__main__":
    unittest.main()
