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


async def broadcast_transport_update(db, transport_option_id: int, travel_date):
    from sqlalchemy import select
    from app.models.package import PackageTransportOption, PackageTransportInventory
    import time
    from app.core.timezone import get_ist_now
    
    # Fetch option
    opt = await db.scalar(
        select(PackageTransportOption).where(PackageTransportOption.id == transport_option_id)
    )
    if not opt:
        return
        
    inv_row = await db.scalar(
        select(PackageTransportInventory).where(
            PackageTransportInventory.transport_option_id == transport_option_id,
            PackageTransportInventory.date == travel_date,
            PackageTransportInventory.deleted_at.is_(None)
        )
    )
    
    t_type_str = opt.type.value if hasattr(opt.type, 'value') else str(opt.type)
    is_shared = t_type_str != 'SEPARATE_VEHICLE'
    
    if inv_row:
        total_capacity = (inv_row.available_count * (opt.capacity or 1)) if is_shared else inv_row.available_count
        remaining = max(0, total_capacity - inv_row.booked_count)
        is_closed = inv_row.is_closed
        price_override = inv_row.price_override
    else:
        remaining = 0
        is_closed = True
        price_override = None
        
    sse_payload = {
        "version": int(time.time() * 1000),
        "timestamp": get_ist_now().isoformat(),
        "package_id": opt.package_id,
        "travel_date": str(travel_date),
        "option_id": transport_option_id,
        "remaining": remaining,
        "is_closed": is_closed,
        "price_override": float(price_override) if price_override is not None else None
    }
    await sse_manager.broadcast_event("package", str(opt.package_id), "TRANSPORT_UPDATE", sse_payload)

