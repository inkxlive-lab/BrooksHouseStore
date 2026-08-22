import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app.services.channel_inventory_engine import (
    PRODUCTION_DB, ProductionWriteRefused, StalePreview, apply_quantity_change_to_copy,
    apply_sale_to_copy, cancel_before_fulfillment_to_copy, confirm_restock_to_copy,
    confirm_staged_inventory_to_copy, initialize_copy_schema, preview_line,
    record_refund_notice_to_copy, connect_copy,
)


SCHEMA = """
CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT,average_cost REAL,active INTEGER);
CREATE TABLE product_barcodes(barcode_id INTEGER PRIMARY KEY,product_id INTEGER,barcode TEXT,is_primary INTEGER);
CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT,location_type TEXT,active INTEGER);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,location_id INTEGER,container_id TEXT,quantity_on_hand INTEGER,quantity_reserved INTEGER,reorder_level INTEGER,updated_at TEXT);
CREATE TABLE inventory_transactions(transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER,location_id INTEGER,container_id TEXT,transaction_type TEXT,quantity_change INTEGER,unit_cost REAL,reference_number TEXT,notes TEXT,created_at TEXT);
CREATE TABLE shopify_sales_orders(shopify_order_id TEXT PRIMARY KEY,processed_at TEXT,test_order INTEGER,cancelled_at TEXT,fulfillment_status TEXT);
CREATE TABLE shopify_sales_lines(shopify_line_id TEXT PRIMARY KEY,shopify_order_id TEXT,product_id INTEGER,quantity INTEGER,current_quantity INTEGER,sku TEXT,title TEXT,updated_at TEXT,match_status TEXT);
CREATE TABLE amazon_order_history(amazon_order_id TEXT PRIMARY KEY,created_time TEXT,fulfillment_status TEXT);
CREATE TABLE amazon_order_item_history(amazon_order_id TEXT,order_item_id TEXT,product_id INTEGER,quantity_ordered INTEGER,seller_sku TEXT,asin TEXT,title TEXT,synced_at TEXT);
CREATE TABLE amazon_listings(amazon_listing_id INTEGER PRIMARY KEY,seller_sku TEXT,asin TEXT,approval_status TEXT,inventory_status TEXT);
CREATE TABLE amazon_product_links(amazon_product_link_id INTEGER PRIMARY KEY,amazon_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE walmart_orders(purchase_order_id TEXT PRIMARY KEY,order_date TEXT,walmart_status TEXT,synced_at TEXT);
CREATE TABLE walmart_order_lines(order_line_id INTEGER PRIMARY KEY,purchase_order_id TEXT,product_id INTEGER,quantity INTEGER,sku TEXT,item_name TEXT,line_status TEXT);
CREATE TABLE walmart_listings(walmart_listing_id INTEGER PRIMARY KEY,seller_sku TEXT);
CREATE TABLE walmart_product_links(walmart_product_link_id INTEGER PRIMARY KEY,walmart_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE operations_work_queue(task_id INTEGER PRIMARY KEY AUTOINCREMENT,task_key TEXT UNIQUE,task_type TEXT,title TEXT,details TEXT,priority TEXT,status TEXT,source_channel TEXT,source_reference TEXT,product_id INTEGER,location_id INTEGER,requested_quantity INTEGER,created_at TEXT,updated_at TEXT,source_location_id INTEGER,source_container_id TEXT,destination_location_id INTEGER,destination_container_id TEXT);
"""


def make_copy(directory: Path, *, quantity=2, storefront=5, back_room=10, reserve=0, staged=0, mapped=True):
    source = directory / "source.db"
    copied = directory / "copy.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO products VALUES(10,'Widget',1.25,1)")
        connection.execute("INSERT INTO product_barcodes VALUES(1,10,'012345678905',1)")
        connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)", [
            (1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage"),
            (3,"Warehouse","warehouse"),(5,"Online Orders / Reserved","reserved"),
        ])
        connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)", [
            (1,10,1,"FRONT",storefront,0,0,"before"),(2,10,2,"BACK",back_room,0,0,"before"),
            (3,10,3,"WH",reserve,0,0,"before"),(4,10,5,"STAGED",staged,0,0,"before"),
        ])
        connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-20',0,NULL,'unfulfilled')")
        connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',?,?,?,?,?,?,?)",
                           (10 if mapped else None,quantity,quantity,'SKU','Widget','v1','matched' if mapped else 'unmatched'))
        connection.commit()
    shutil.copy2(source, copied)
    initialize_copy_schema(copied)
    return copied


def inventory_state(database):
    with closing(sqlite3.connect(database)) as connection:
        quantities = dict(connection.execute("SELECT inventory_id,quantity_on_hand FROM inventory"))
        tx = connection.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[0]
        ledger = connection.execute("SELECT COUNT(*) FROM channel_inventory_ledger").fetchone()[0]
        allocation = connection.execute("SELECT status,ordered_quantity,deducted_quantity,unlocated_quantity,restored_quantity FROM channel_inventory_allocations").fetchone()
    return quantities, tx, ledger, allocation


class ChannelInventoryEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _add_channel_line(self, db, channel):
        with closing(sqlite3.connect(db)) as connection:
            if channel == "amazon":
                connection.execute("INSERT INTO amazon_order_history VALUES('A1','2026-08-20','Unfulfilled')")
                connection.execute("INSERT INTO amazon_order_item_history VALUES('A1','AL1',10,2,'SKU','ASIN1','Widget','v1')")
                connection.execute("INSERT INTO amazon_listings VALUES(1,'SKU','ASIN1','approved','active')")
                connection.execute("INSERT INTO amazon_product_links VALUES(1,1,10,'linked')")
                key = ("amazon","A1","AL1")
            else:
                connection.execute("INSERT INTO walmart_orders VALUES('W1','2026-08-20','Created','v1')")
                connection.execute("INSERT INTO walmart_order_lines VALUES(101,'W1',10,2,'SKU','Widget','Created')")
                connection.execute("INSERT INTO walmart_listings VALUES(1,'SKU')")
                connection.execute("INSERT INTO walmart_product_links VALUES(1,1,10,'linked')")
                key = ("walmart","W1","101")
            connection.commit()
        return key

    def test_amazon_and_walmart_normal_sale_and_duplicate_sync(self):
        for channel in ("amazon","walmart"):
            with self.subTest(channel=channel):
                channel_root = self.root / channel
                channel_root.mkdir()
                db = make_copy(channel_root,storefront=5)
                key = self._add_channel_line(db,channel)
                first = apply_sale_to_copy(db,*key)
                second = apply_sale_to_copy(db,*key)
                quantities, tx, ledger, allocation = inventory_state(db)
                self.assertEqual((first["status"],second["status"]),("deducted","already_applied"))
                self.assertEqual((quantities[1],tx,ledger,tuple(allocation)),
                                 (3,1,1,("deducted",2,2,0,0)))

    def test_amazon_and_walmart_unsafe_authoritative_mappings_are_review_only(self):
        cases = (("missing", None), ("unlinked", "unlinked"), ("disabled", "disabled"), ("ambiguous", "ambiguous"),
                 ("conflicting", "linked"), ("disabled_product", "linked"))
        for channel in ("amazon","walmart"):
            for state, status in cases:
                with self.subTest(channel=channel,state=state):
                    root = self.root / f"{channel}-{state}"; root.mkdir()
                    db = make_copy(root,storefront=5)
                    key = self._add_channel_line(db,channel)
                    with closing(sqlite3.connect(db)) as connection:
                        table = f"{channel}_product_links"
                        if state == "missing":
                            connection.execute(f"DELETE FROM {table}")
                        elif state == "conflicting":
                            connection.execute("INSERT INTO products VALUES(11,'Other',1,1)")
                            connection.execute(f"UPDATE {table} SET product_id=11,match_status=?",(status,))
                        elif state == "disabled_product":
                            connection.execute("UPDATE products SET active=0 WHERE product_id=10")
                        else:
                            connection.execute(f"UPDATE {table} SET match_status=?",(status,))
                        connection.commit()
                    before = inventory_state(db)
                    result = apply_sale_to_copy(db,*key)
                    self.assertEqual(result["status"],"review")
                    self.assertEqual(inventory_state(db),before)

    def test_disabled_amazon_listing_is_review_only(self):
        db = make_copy(self.root,storefront=5)
        key = self._add_channel_line(db,"amazon")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE amazon_listings SET approval_status='rejected'")
            connection.commit()
        before = inventory_state(db)
        self.assertEqual(apply_sale_to_copy(db,*key)["status"],"review")
        self.assertEqual(inventory_state(db),before)

    def test_storefront_complete_fulfillment(self):
        db = make_copy(self.root, storefront=5, back_room=10)
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, allocation = inventory_state(db)
        self.assertEqual(result["status"],"deducted")
        self.assertEqual((quantities[1],quantities[2]),(3,10))
        self.assertEqual((tx,ledger),(1,1))
        self.assertEqual(tuple(allocation),("deducted",2,2,0,0))

    def test_back_room_complete_without_split(self):
        db = make_copy(self.root, storefront=1, back_room=10)
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, _ = inventory_state(db)
        self.assertEqual(result["plan"]["location_name"],"Store Back Room")
        self.assertEqual((quantities[1],quantities[2]),(1,8))
        self.assertEqual((tx,ledger),(1,1))

    def test_reserve_candidate_creates_owed_allocation_and_work(self):
        db = make_copy(self.root, storefront=1, back_room=1, reserve=10)
        before = inventory_state(db)[0]
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, allocation = inventory_state(db)
        with closing(sqlite3.connect(db)) as connection:
            task = connection.execute("SELECT task_type,requested_quantity,source_location_id FROM operations_work_queue").fetchone()
        self.assertEqual(result["status"],"replenishment_needed")
        self.assertEqual(quantities,before)
        self.assertEqual((tx,ledger),(0,1))
        self.assertEqual(tuple(allocation),("replenishment_needed",2,0,2,0))
        self.assertEqual(task,("directed_replenishment",2,3))

    def test_unavailable_stays_visible_without_fake_stock(self):
        db = make_copy(self.root, storefront=0, back_room=0, reserve=1)
        before = inventory_state(db)[0]
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, allocation = inventory_state(db)
        self.assertEqual(result["status"],"replenishment_needed")
        self.assertEqual(quantities,before)
        self.assertEqual((tx,ledger),(0,1))
        self.assertEqual(allocation[3],2)

    def test_duplicate_sync_is_idempotent(self):
        db = make_copy(self.root)
        first = apply_sale_to_copy(db,"shopify","O1","L1")
        second = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, _ = inventory_state(db)
        self.assertEqual(first["status"],"deducted")
        self.assertEqual(second["status"],"already_applied")
        self.assertEqual((quantities[1],tx,ledger),(3,1,1))

    def test_quantity_decrease_and_increase_lifecycle(self):
        db = make_copy(self.root,quantity=3,storefront=5,back_room=0)
        apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_lines SET current_quantity=2,updated_at='v2'")
            connection.commit()
        decrease = apply_quantity_change_to_copy(db,"shopify","O1","L1")
        self.assertEqual((decrease["restored_quantity"],inventory_state(db)[0][1]),(1,3))
        duplicate = apply_quantity_change_to_copy(db,"shopify","O1","L1")
        self.assertEqual(duplicate["status"],"already_applied")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_lines SET current_quantity=4,updated_at='v3'")
            connection.commit()
        increase = apply_quantity_change_to_copy(db,"shopify","O1","L1")
        self.assertEqual((increase["unlocated_quantity"],inventory_state(db)[0][1]),(0,1))

    def test_multi_quantity_uses_multiple_rows_in_one_eligible_location(self):
        db = make_copy(self.root,quantity=5,storefront=3,back_room=0)
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("INSERT INTO inventory VALUES(5,10,1,'FRONT-2',2,0,0,'before')")
            connection.commit()
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            physical = dict(connection.execute("SELECT inventory_id,quantity_on_hand FROM inventory WHERE inventory_id IN (1,5)"))
            ownership = list(connection.execute("SELECT inventory_id,deducted_quantity FROM channel_inventory_allocation_inventory ORDER BY inventory_id"))
        self.assertEqual(result["status"],"deducted")
        self.assertEqual(physical,{1:0,5:0})
        self.assertEqual(ownership,[(1,3),(5,2)])

    def test_location_policy_is_explicit_ordered_and_allowlisted(self):
        db = make_copy(self.root,quantity=5,storefront=3,back_room=3,reserve=50,staged=50)
        single = apply_sale_to_copy(db,"shopify","O1","L1")
        self.assertEqual(single["status"],"replenishment_needed")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("DELETE FROM channel_inventory_ledger")
            connection.execute("DELETE FROM channel_inventory_allocations")
            connection.execute("DELETE FROM operations_work_queue")
            connection.commit()
        multi = apply_sale_to_copy(db,"shopify","O1","L1",allocation_policy="ordered_multi_location",
                                   eligible_locations=("BrooksHouse Storefront","Store Back Room"))
        with closing(sqlite3.connect(db)) as connection:
            quantities = dict(connection.execute("SELECT inventory_id,quantity_on_hand FROM inventory"))
            metadata = connection.execute("SELECT metadata_json FROM channel_inventory_ledger").fetchone()[0]
        self.assertEqual(multi["status"],"deducted")
        self.assertEqual((quantities[1],quantities[2],quantities[3],quantities[4]),(0,1,50,50))
        self.assertIn('"allocation_policy":"ordered_multi_location"',metadata)
        self.assertEqual(multi["plan"]["location_name"],"BrooksHouse Storefront -> Store Back Room")

    def test_location_allowlist_never_uses_unapproved_locations(self):
        db = make_copy(self.root,quantity=2,storefront=0,back_room=0,reserve=50,staged=50)
        result = apply_sale_to_copy(db,"shopify","O1","L1",allocation_policy="ordered_multi_location",
                                    eligible_locations=("BrooksHouse Storefront","Store Back Room"))
        quantities = inventory_state(db)[0]
        self.assertEqual(result["status"],"replenishment_needed")
        self.assertEqual((quantities[3],quantities[4]),(50,50))
        rejected_root = self.root / "unapproved-location"; rejected_root.mkdir()
        rejected_db = make_copy(rejected_root,quantity=2,storefront=0,back_room=0,reserve=50)
        with self.assertRaises(ValueError):
            apply_sale_to_copy(rejected_db,"shopify","O1","L1",allocation_policy="ordered_multi_location",
                               eligible_locations=("Warehouse",))

    def test_cancellation_before_fulfillment_releases_owed(self):
        db = make_copy(self.root,storefront=0,back_room=0,reserve=0)
        apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at='now'")
            connection.commit()
        result = cancel_before_fulfillment_to_copy(db,"shopify","O1","L1")
        self.assertEqual((result["restored_quantity"],tuple(inventory_state(db)[3])),(0,("cancelled",0,0,0,0)))

    def test_cancellation_after_deduction_restores_once(self):
        db = make_copy(self.root,storefront=5)
        apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at='now'")
            connection.commit()
        first = cancel_before_fulfillment_to_copy(db,"shopify","O1","L1")
        second = cancel_before_fulfillment_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, _ = inventory_state(db)
        self.assertEqual((first["restored_quantity"],second["status"]),(2,"already_applied"))
        self.assertEqual((quantities[1],tx,ledger),(5,2,2))

    def test_refund_does_not_restock(self):
        db = make_copy(self.root,storefront=5)
        apply_sale_to_copy(db,"shopify","O1","L1")
        before = inventory_state(db)
        result = record_refund_notice_to_copy(db,"shopify","O1","L1","refund-1")
        after = inventory_state(db)
        self.assertEqual(result["inventory_change"],0)
        self.assertEqual((before[0],before[1]),(after[0],after[1]))

    def test_confirmed_restock_is_distinct_and_idempotent(self):
        db = make_copy(self.root,storefront=5)
        apply_sale_to_copy(db,"shopify","O1","L1")
        first = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-1")
        second = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-1")
        quantities, tx, ledger, _ = inventory_state(db)
        self.assertEqual((first["status"],second["status"]),("physically_restocked","already_applied"))
        self.assertEqual((quantities[1],tx,ledger),(5,2,2))

    def test_cumulative_restock_is_capped_at_legitimately_returnable_quantity(self):
        db = make_copy(self.root,quantity=5,storefront=5)
        apply_sale_to_copy(db,"shopify","O1","L1")
        first = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-1")
        second = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-2")
        third = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-3")
        duplicate = confirm_restock_to_copy(db,"shopify","O1","L1",1,2,"return-3")
        blocked = confirm_restock_to_copy(db,"shopify","O1","L1",1,1,"return-4")
        self.assertEqual((first["quantity"],second["quantity"],third["quantity"]),(2,2,1))
        self.assertEqual(third["status"],"partially_restocked_capped")
        self.assertEqual((duplicate["status"],blocked["status"]),("already_applied","over_restock_blocked"))
        self.assertEqual(inventory_state(db)[0][1],5)

    def test_unmatched_product_is_review_only(self):
        db = make_copy(self.root,mapped=False)
        before = inventory_state(db)
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        self.assertEqual(result["status"],"review")
        self.assertEqual(inventory_state(db),before)

    def test_ambiguous_product_id_is_review_only(self):
        db = make_copy(self.root,mapped=True)
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_lines SET match_status='ambiguous'")
            connection.commit()
        before = inventory_state(db)
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        self.assertEqual(result["status"],"review")
        self.assertEqual(inventory_state(db),before)

    def test_cancel_reactivate_and_cancel_again_is_idempotent(self):
        db = make_copy(self.root,quantity=2,storefront=5)
        apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at='c1'")
            connection.commit()
        cancel_before_fulfillment_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at=NULL")
            connection.execute("UPDATE shopify_sales_lines SET updated_at='v2'")
            connection.commit()
        reopened = apply_sale_to_copy(db,"shopify","O1","L1")
        duplicate = apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at='c2'")
            connection.commit()
        cancelled_again = cancel_before_fulfillment_to_copy(db,"shopify","O1","L1")
        quantities, tx, ledger, allocation = inventory_state(db)
        self.assertEqual((reopened["status"],duplicate["status"],cancelled_again["status"]),
                         ("deducted","already_applied","cancelled"))
        self.assertEqual((quantities[1],tx,ledger,allocation[0]),(5,4,4,"cancelled"))

    def test_failure_after_inventory_update_rolls_back_everything_and_retry_is_safe(self):
        db = make_copy(self.root,quantity=2,storefront=5)
        with patch("app.services.channel_inventory_engine._inventory_transaction", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                apply_sale_to_copy(db,"shopify","O1","L1")
        self.assertEqual(inventory_state(db)[0:3],({1:5,2:10,3:0,4:0},0,0))
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        self.assertEqual((result["status"],inventory_state(db)[0][1]),("deducted",3))

    def test_second_row_failure_rolls_back_first_row_and_retry_is_safe(self):
        db = make_copy(self.root,quantity=5,storefront=3,back_room=0)
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("INSERT INTO inventory VALUES(5,10,1,'FRONT-2',2,0,0,'before')")
            connection.commit()
        from app.services import channel_inventory_engine as engine
        original = engine._inventory_transaction
        calls = {"count": 0}
        def fail_second(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected second-row failure")
            return original(*args, **kwargs)
        with patch("app.services.channel_inventory_engine._inventory_transaction", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                apply_sale_to_copy(db,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(dict(connection.execute("SELECT inventory_id,quantity_on_hand FROM inventory WHERE inventory_id IN (1,5)")),{1:3,5:2})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM channel_inventory_allocations").fetchone()[0],0)
        self.assertEqual(apply_sale_to_copy(db,"shopify","O1","L1")["status"],"deducted")

    def test_mapping_change_after_preview_is_refused(self):
        db = make_copy(self.root)
        with closing(connect_copy(db)) as connection:
            preview = preview_line(connection,"shopify","O1","L1")
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("UPDATE shopify_sales_lines SET product_id=NULL,updated_at='v2'")
            connection.commit()
        with self.assertRaises(StalePreview):
            apply_sale_to_copy(db,"shopify","O1","L1",preview)
        self.assertEqual(inventory_state(db)[0][1],5)

    def test_concurrent_attempts_deduct_once(self):
        db = make_copy(self.root)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: apply_sale_to_copy(db,"shopify","O1","L1"),range(2)))
        self.assertEqual(sorted(result["status"] for result in results),["already_applied","deducted"])
        quantities, tx, ledger, _ = inventory_state(db)
        self.assertEqual((quantities[1],tx,ledger),(3,1,1))

    def test_online_reserved_is_not_general_stock_and_production_is_refused(self):
        db = make_copy(self.root,storefront=0,back_room=0,reserve=0,staged=100)
        result = apply_sale_to_copy(db,"shopify","O1","L1")
        quantities, tx, _, allocation = inventory_state(db)
        self.assertEqual((result["status"],quantities[4],tx,allocation[3]),("unlocated",100,0,2))
        staged = confirm_staged_inventory_to_copy(db,"shopify","O1","L1",4,2,"staged-by-martel-1")
        duplicate = confirm_staged_inventory_to_copy(db,"shopify","O1","L1",4,2,"staged-by-martel-1")
        with closing(sqlite3.connect(db)) as connection:
            physical = connection.execute("SELECT quantity_on_hand,quantity_reserved FROM inventory WHERE inventory_id=4").fetchone()
            owned = connection.execute("SELECT staged_quantity,unlocated_quantity,status FROM channel_inventory_allocations").fetchone()
        self.assertEqual((staged["status"],duplicate["status"]),("staged_and_reserved","already_applied"))
        self.assertEqual(physical,(100,2))
        self.assertEqual(owned,(2,0,"staged"))
        with self.assertRaises(ProductionWriteRefused):
            initialize_copy_schema(PRODUCTION_DB)


if __name__ == "__main__":
    unittest.main()
