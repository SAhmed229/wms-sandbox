"""Demand predictor: Phase 1 stub returning binary load probability."""

from __future__ import annotations

from src.models.orders import CarrierAppointment, OutboundOrder


class DemandPredictor:
    """Predicts the probability that a SKU will load on a given appointment.

    Phase 1 implementation: returns 1.0 if the SKU appears on any order
    linked to the appointment, 0.0 otherwise. Phase 2 replaces this with
    a trained LightGBM model using historical velocity features.
    """

    def predict(
        self,
        sku_id: str,
        appointment: CarrierAppointment,
        orders: list[OutboundOrder],
    ) -> float:
        """Return binary load probability for a SKU on a carrier appointment.

        Args:
            sku_id: The SKU identifier to check.
            appointment: The carrier appointment to evaluate.
            orders: All outbound orders within the planning horizon.

        Returns:
            1.0 if the SKU appears on an order linked to this appointment, 0.0 otherwise.
        """
        for order in orders:
            if order.appointment.appointment_id != appointment.appointment_id:
                continue
            for line in order.lines:
                if line.sku_id == sku_id and not line.picked:
                    return 1.0
        return 0.0
