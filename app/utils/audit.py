from typing import Optional, Any
from decimal import Decimal
import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.settings import AuditLog
from loguru import logger

def make_json_serializable(data: Any) -> Any:
    """
    Recursively converts non-serializable objects (like time, date, datetime, Decimal)
    into standard JSON-safe data types.
    """
    if isinstance(data, dict):
        return {k: make_json_serializable(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [make_json_serializable(v) for v in data]
    elif isinstance(data, (datetime.datetime, datetime.date, datetime.time)):
        return data.isoformat()
    elif isinstance(data, Decimal):
        return float(data)
    return data

async def log_action(
    db: AsyncSession,
    user_id: Optional[int],
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None
):
    """
    Log an administrative or critical action to the audit_logs table.
    """
    try:
        serialized_details = make_json_serializable(details) if details else None
        new_log = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            details=serialized_details,
            ip_address=ip_address
        )
        db.add(new_log)
        await db.flush() # Flush to ensure it's part of the current transaction but not committed yet
        logger.info(f"Audit Logged: {action} on {entity_type} {entity_id} by User {user_id}")
    except Exception as e:
        logger.error(f"Failed to create audit log: {str(e)}")
        # We don't raise here to prevent the main action from failing just because logging failed
