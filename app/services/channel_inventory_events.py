"""Normalize channel-specific lifecycle signals into BrooksHouse inventory events."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NormalizedInventoryEvent:
    channel: str
    event_type: str
    quantity: int
    inventory_mutation: bool
    requires_review: bool
    reason: str
    reference: str

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_channel_event(channel: str, signal: str, *, quantity: int = 0,
                            previous_quantity: int | None = None, status: str = "",
                            physical_restock_confirmed: bool = False, reference: str = "") -> NormalizedInventoryEvent:
    channel, signal, state = channel.casefold(), signal.casefold(), status.casefold()
    if channel not in {"shopify", "amazon", "walmart"}:
        raise ValueError(f"Unsupported channel: {channel}")
    if signal == "new_order":
        eligible = ((channel == "shopify" and state in {"paid/unfulfilled", "authorized/unfulfilled", "partially_paid/unfulfilled"})
                    or (channel == "amazon" and "unshipped" in state and "merchant" in state)
                    or (channel == "walmart" and state in {"created", "acknowledged"}))
        return NormalizedInventoryEvent(channel, "sale_commitment" if eligible else "lifecycle_review", quantity,
                                        eligible, not eligible, "" if eligible else "channel order state is not deduction-eligible", reference)
    if signal == "quantity_change":
        if previous_quantity is None or quantity == previous_quantity:
            return NormalizedInventoryEvent(channel, "unchanged", 0, False, previous_quantity is None,
                                            "previous quantity is required" if previous_quantity is None else "", reference)
        kind = "quantity_increase" if quantity > previous_quantity else "quantity_decrease"
        return NormalizedInventoryEvent(channel, kind, abs(quantity-previous_quantity), True, False, "", reference)
    if signal in {"cancellation", "partial_cancellation"}:
        kind = "partial_cancellation" if signal == "partial_cancellation" or quantity > 0 else "cancellation"
        return NormalizedInventoryEvent(channel, kind, quantity, True, False, "", reference)
    if signal == "refund":
        return NormalizedInventoryEvent(channel, "refund_notice", quantity, False, True,
                                        "financial refund never proves physical restock", reference)
    if signal in {"return", "restock"}:
        if not physical_restock_confirmed:
            return NormalizedInventoryEvent(channel, "return_notice", quantity, False, True,
                                            "physical receipt and sellable destination are required", reference)
        return NormalizedInventoryEvent(channel, "confirmed_physical_restock", quantity, True, False, "", reference)
    if signal in {"reopened", "reactivated"}:
        supported = channel in {"shopify", "walmart"}
        return NormalizedInventoryEvent(channel, "reactivation" if supported else "lifecycle_review", quantity,
                                        supported, not supported,
                                        "Amazon reactivation requires manual review" if not supported else "", reference)
    return NormalizedInventoryEvent(channel, "lifecycle_review", quantity, False, True,
                                    f"unknown {channel} lifecycle signal", reference)
