"""Helpers for public vs post-booking room display details."""

from typing import Any, Dict, Optional, Sequence

_VENDOR_KEYWORDS = (
    "hotel",
    "residency",
    "resort",
    "punnami",
    "lodge",
    "vashista",
    "vashistha",
    "anand",
    "haritha",
)


def format_legacy_room_type_title(
    slug: str,
    variant_names: Sequence[str] = (),
    fallback: Optional[str] = None,
) -> str:
    """Legacy fallback for formatting a generic room type when `public_title` is empty."""
    slug_lower = (slug or "").lower()
    if any(k in slug_lower for k in ("suite", "deluxe", "luxury", "bed", "cottage", "hut", "room")):
        clean_parts = []
        for part in slug_lower.split("-"):
            if part in ("ac", "a/c"):
                clean_parts.append("A/C")
            elif part in ("nonac", "non-ac"):
                clean_parts.append("Non-A/C")
            elif part.isdigit():
                continue
            elif part:
                clean_parts.append(part.capitalize())
        res = " ".join(clean_parts).strip()
        if res:
            return res

    active_variants = [str(v).strip() for v in (variant_names or []) if v and str(v).strip()]
    if active_variants:
        if len(active_variants) == 1:
            return active_variants[0]
        lower_variants = [v.lower() for v in active_variants]
        has_suite = any("suite" in v for v in lower_variants)
        has_deluxe = any("deluxe" in v for v in lower_variants)
        has_4bed = any("4" in v or "four" in v for v in lower_variants)
        has_2bed = any("2" in v or "two" in v or "double" in v for v in lower_variants)
        has_3bed = any("3" in v or "three" in v or "triple" in v for v in lower_variants)
        if has_suite and has_deluxe:
            return "Deluxe & Family Suite A/C"
        if has_2bed and has_4bed:
            return "2-Bed & 4-Bed A/C Deluxe Room"
        if has_3bed or has_suite:
            return "Family Suite & Deluxe A/C"
        return " / ".join(active_variants[:2])

    return (fallback or "").strip() or "Deluxe Riverside Stay"


def looks_like_vendor_name(name: Optional[str]) -> bool:
    """Return True if a name appears to contain an internal hotel/vendor identifier."""
    lowered = (name or "").strip().lower()
    return any(kw in lowered for kw in _VENDOR_KEYWORDS)


def get_public_room_title(room: Any, variant_names: Sequence[str] = ()) -> str:
    """
    Resolve the customer-facing room title shown publicly BEFORE booking.

    Admin-configured `public_title` is always respected verbatim. Only legacy records
    missing `public_title` fall back to safe non-vendor formatting so internal hotel
    names (`lodge_name`) are never leaked during public discovery.
    """
    explicit = str(getattr(room, "public_title", "") or "").strip()
    if explicit:
        return explicit

    lodge = str(getattr(room, "lodge_name", "") or "").strip()
    if lodge and not looks_like_vendor_name(lodge):
        return lodge

    return format_legacy_room_type_title(
        getattr(room, "slug", "") or "",
        variant_names,
        fallback="Deluxe Riverside Stay",
    )


def get_public_room_address(room: Any) -> str:
    """
    Return a safe general locality string for pre-booking discovery pages.
    Exact lodge street addresses and vendor map pins are withheld until after booking.
    """
    raw_text = " ".join(
        str(part or "")
        for part in (
            getattr(room, "address", None),
            getattr(room, "slug", None),
            getattr(room, "public_title", None),
        )
    ).lower()
    if "kolluru" in raw_text or "papikondalu" in raw_text or "sandbank" in raw_text:
        return "Kolluru, Papikondalu Region"
    if "rajahmundry" in raw_text or "rajamahendravaram" in raw_text:
        return "Rajahmundry, Godavari Region"
    return "Bhadrachalam, Telangana"


def resolve_post_booking_hotel_details(
    room: Any,
    inventory: Any = None,
    pricing_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    """
    Resolve assigned hotel details revealed AFTER booking:
    1. Per-date inventory override (`RoomSlotInventory.hotel_name/address/map_url`)
    2. Snapshot frozen on booking (`pricing_snapshot`)
    3. Room default hotel settings (`Room.lodge_name`, `Room.address`, `Room.map_url`)
    """
    snap = pricing_snapshot or {}
    hotel_name = (
        (getattr(inventory, "hotel_name", None) or "").strip()
        or (str(snap.get("hotel_name") or "").strip())
        or (getattr(room, "lodge_name", None) or "").strip()
        or None
    )
    hotel_address = (
        (getattr(inventory, "hotel_address", None) or "").strip()
        or (str(snap.get("hotel_address") or "").strip())
        or (getattr(room, "address", None) or "").strip()
        or None
    )
    hotel_map_url = (
        (getattr(inventory, "hotel_map_url", None) or "").strip()
        or (str(snap.get("hotel_map_url") or "").strip())
        or (getattr(room, "map_url", None) or "").strip()
        or None
    )
    return {
        "hotel_name": hotel_name,
        "hotel_address": hotel_address,
        "hotel_map_url": hotel_map_url,
    }
