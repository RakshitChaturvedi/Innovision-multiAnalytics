"""The only way this repo publishes alerts to the platform (`alerts:live`)."""

import logging
import os
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from shared.frames import minio_frame_key
from shared.platform_contracts.alert_event import AlertEvent
from shared.platform_contracts.enums import (
    AlertSeverity,
    FrameProvider,
    SourceUC,
)

logger = logging.getLogger(__name__)

ALERTS_STREAM = "alerts:live"
ALERTS_MAXLEN = 10000
TITLE_MAX = 200
DESCRIPTION_MAX = 2000


def _enum_value(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def build_platform_alert(
    *,
    domain_event_id: UUID,
    camera_id: UUID,
    timestamp: datetime,
    severity: Any,
    alert_type: str,
    title: str,
    description: str | None,
    frame_seq: int | None,
    metadata: dict | None = None,
) -> AlertEvent:
    """Build a platform AlertEvent with a deterministic alert_id.

    The id derives from the domain event id, so re-publishing after a retry
    yields the same alert_id and the platform dedupes it.
    """
    alert_type = _enum_value(alert_type)
    title = title[:TITLE_MAX]
    description = (description or title)[:DESCRIPTION_MAX]

    if frame_seq is not None:
        frame_reference = minio_frame_key(camera_id, frame_seq)
        frame_provider = FrameProvider.MINIO
    else:
        frame_reference = None
        frame_provider = None

    metadata = dict(metadata or {})
    if "source_event_ids" in metadata:
        metadata["source_event_ids"] = [str(i) for i in metadata["source_event_ids"]]

    return AlertEvent(
        alert_id=uuid5(NAMESPACE_URL, f"innovision-mva:{alert_type}:{domain_event_id}"),
        camera_id=camera_id,
        timestamp=timestamp,
        severity=AlertSeverity(_enum_value(severity)),
        alert_type=alert_type,
        title=title,
        description=description,
        source_event_id=domain_event_id,
        source_uc=SourceUC(os.environ.get("SOURCE_UC", "uc1")),
        frame_reference=frame_reference,
        frame_provider=frame_provider,
        metadata=metadata,
    )


class AlertPublisher:
    def __init__(
        self,
        redis,
        stream: str = ALERTS_STREAM,
        maxlen: int = ALERTS_MAXLEN,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    async def publish(self, alert: AlertEvent) -> None:
        """Validate and XADD. Raises on any failure so the caller's message
        stays pending and is retried (same alert_id -> platform dedupes)."""
        payload = alert.model_dump_json()
        checked = AlertEvent.model_validate_json(payload)  # JSON round trip
        if not checked.title.strip():
            raise ValueError("alert title is whitespace only")
        if not checked.description.strip():
            raise ValueError("alert description is whitespace only")

        await self._redis.xadd(
            self._stream,
            {"data": payload},
            maxlen=self._maxlen,
            approximate=True,
        )
