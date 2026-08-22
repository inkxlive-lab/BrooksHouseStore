import unittest

from app.services.channel_inventory_events import normalize_channel_event


class ChannelInventoryEventTests(unittest.TestCase):
    def test_channel_specific_new_order_states(self):
        eligible = {"shopify":"paid/unfulfilled", "amazon":"Unshipped / Merchant", "walmart":"acknowledged"}
        for channel, status in eligible.items():
            with self.subTest(channel=channel):
                event = normalize_channel_event(channel,"new_order",quantity=2,status=status)
                self.assertEqual((event.event_type,event.inventory_mutation,event.requires_review),
                                 ("sale_commitment",True,False))
                unsafe = normalize_channel_event(channel,"new_order",quantity=2,status="refunded")
                self.assertTrue(unsafe.requires_review)

    def test_quantity_cancel_refund_return_and_reactivation_semantics(self):
        for channel in ("shopify","amazon","walmart"):
            with self.subTest(channel=channel):
                self.assertEqual(normalize_channel_event(channel,"quantity_change",quantity=5,previous_quantity=3).event_type,"quantity_increase")
                self.assertEqual(normalize_channel_event(channel,"quantity_change",quantity=2,previous_quantity=5).event_type,"quantity_decrease")
                self.assertEqual(normalize_channel_event(channel,"partial_cancellation",quantity=2).event_type,"partial_cancellation")
                self.assertFalse(normalize_channel_event(channel,"refund",quantity=2).inventory_mutation)
                self.assertFalse(normalize_channel_event(channel,"return",quantity=2).inventory_mutation)
                self.assertTrue(normalize_channel_event(channel,"return",quantity=2,physical_restock_confirmed=True).inventory_mutation)
                reopened = normalize_channel_event(channel,"reopened",quantity=2)
                self.assertEqual(reopened.requires_review,channel == "amazon")


if __name__ == "__main__":
    unittest.main()
