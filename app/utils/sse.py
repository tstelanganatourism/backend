import asyncio
import json
import time
from typing import Dict, Set

class SSEManager:
    def __init__(self):
        # Maps package_id to a set of queues
        self.package_clients: Dict[str, Set[asyncio.Queue]] = {}
        # Maps room_id to a set of queues
        self.room_clients: Dict[str, Set[asyncio.Queue]] = {}

    def _get_clients(self, entity_type: str, entity_id: str) -> Set[asyncio.Queue]:
        if entity_type == "package":
            if entity_id not in self.package_clients:
                self.package_clients[entity_id] = set()
            return self.package_clients[entity_id]
        elif entity_type == "room":
            if entity_id not in self.room_clients:
                self.room_clients[entity_id] = set()
            return self.room_clients[entity_id]
        return set()

    def add_client(self, entity_type: str, entity_id: str, queue: asyncio.Queue):
        clients = self._get_clients(entity_type, entity_id)
        clients.add(queue)

    def remove_client(self, entity_type: str, entity_id: str, queue: asyncio.Queue):
        clients = self._get_clients(entity_type, entity_id)
        if queue in clients:
            clients.remove(queue)
            if not clients:
                if entity_type == "package":
                    del self.package_clients[entity_id]
                elif entity_type == "room":
                    del self.room_clients[entity_id]

    async def broadcast_event(self, entity_type: str, entity_id: str, event_name: str, payload: dict):
        """
        Emits a Server-Sent Event to all scoped connected clients.
        Example Payload MUST include version, timestamp, etc.
        """
        clients = self._get_clients(entity_type, str(entity_id))
        if not clients:
            return

        data_str = json.dumps(payload)
        sse_message = f"event: {event_name}\ndata: {data_str}\n\n"
        
        # Snapshot clients to avoid runtime modification errors
        for queue in list(clients):
            try:
                # Non-blocking put
                queue.put_nowait(sse_message)
            except asyncio.QueueFull:
                pass

def build_package_sse_payload(variant, inventory_row, travel_date):
    import time
    from app.core.timezone import get_ist_now
    from decimal import Decimal
    
    is_weekend = travel_date.weekday() in (5, 6)
    is_student = False
    if hasattr(variant, 'package') and variant.package:
        is_student = variant.package.is_student_package

    modifier = Decimal("0.00")
    total_capacity = 0
    booked_count = 0
    reserved_count = 0
    is_closed = True

    if inventory_row:
        modifier = inventory_row.price_override if inventory_row.price_override is not None else Decimal("0.00")
        total_capacity = inventory_row.total_capacity
        booked_count = inventory_row.booked_count
        reserved_count = getattr(inventory_row, 'reserved_count', 0)
        is_closed = inventory_row.is_closed

    if is_student:
        b_student = variant.weekend_student_price if is_weekend and getattr(variant, 'weekend_student_price', None) is not None else getattr(variant, 'student_price', Decimal("0.00"))
        b_adult = Decimal("0.00")
        b_child = Decimal("0.00")
    else:
        b_student = None
        b_adult = variant.weekend_adult_price if is_weekend and getattr(variant, 'weekend_adult_price', None) is not None else getattr(variant, 'adult_price', Decimal("0.00"))
        b_child = variant.weekend_child_price if is_weekend and getattr(variant, 'weekend_child_price', None) is not None else getattr(variant, 'child_price', Decimal("0.00"))

    eff_student = float(max(Decimal("0.00"), (b_student or Decimal("0.00")) + modifier)) if b_student is not None else None
    eff_adult = float(max(Decimal("0.00"), (b_adult or Decimal("0.00")) + modifier))
    eff_child = float(max(Decimal("0.00"), (b_child or Decimal("0.00")) + modifier))

    return {
        "version": int(time.time() * 1000),
        "timestamp": get_ist_now().isoformat(),
        "package_id": variant.package_id,
        "travel_date": str(travel_date),
        "available": max(0, total_capacity - (booked_count + reserved_count)),
        "reserved": reserved_count,
        "booked": booked_count,
        "is_closed": is_closed,
        "effective_adult_price": eff_adult,
        "effective_child_price": eff_child,
        "effective_student_price": eff_student,
        "variant_id": variant.id
    }

sse_manager = SSEManager()
