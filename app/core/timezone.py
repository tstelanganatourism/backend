from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

def get_ist_now() -> datetime:
    """Returns the current aware datetime in IST."""
    return datetime.now(IST)

def to_ist(dt: datetime) -> datetime:
    """Converts an existing datetime to IST."""
    if dt.tzinfo is None:
        # Assume UTC if naive, then convert
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def ist_date_today():
    """Returns the current date in IST."""
    return get_ist_now().date()
