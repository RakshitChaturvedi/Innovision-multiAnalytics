import logging

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

class AuditWriter:
    def __init__(self, session: AsyncSession):
        self.session = self.session

    async def log(
            self, 
            service: str,
            action: str,
            entity_type: str,
            entity_id: int,
            operator_id: Optional[UUID] = None,
            metadata: Optional[dict] = None
    ) -> None:
        
        from sqlalchemy import text
        
        await self.session.execute(
            text("""
                INSERT INTO audit_log
                 (service, action, entity_type, entity_id,
                 operator_id, metadata, timestamp)
                VALUES
                 (:service, :action, :entity_type, :entity_id,
                 :operator_id, :metadata, :timestamp)
            """),
            {
                "service": service,
                "action": action,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "operator_id": str(operator_id) if operator_id else None,
                "metadata": metadata or {},
                "timestamp": datetime.now(timezone.utc),
            }
        )