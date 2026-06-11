from enum import Enum


class CameraProfile(str, Enum):
    HIGH_SECURITY = "high_security"
    BALANCED = "balanced"
    HIGH_THROUGHPUT = "high_throughput"
    CROWD_ONLY = "crowd_only"


class IdentityTag(str, Enum):
    ENROLLED = "enrolled"
    VISITOR = "visitor"
    UNKNOWN = "unknown"


class AlertType(str, Enum):
    RESTRICTED_ENTRY = "restricted_entry"
    INTRUDER = "intruder"
    HEADCOUNT_BREACH = "headcount_breach"
    CROWD_DENSITY = "crowd_density"
    PEDESTRIAN_ANOMALY = "pedestrian_anomaly"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertStatus(str, Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class ZoneType(str, Enum):
    RESTRICTED = "restricted"
    MONITORED = "monitored"
    SAFE = "safe"


class ZoneEventType(str, Enum):
    ENTERED = "entered"
    EXITED = "exited"
    DWELL = "dwell"


class DensityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CameraStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"


class FeedbackType(str, Enum):
    CONFIRM = "confirm"
    REJECT = "reject"


class DataCategory(str, Enum):
    VISITOR_EMBEDDINGS = "visitor_embeddings"
    RECOGNITION_EVENTS = "recognition_events"
    DETECTION_EVENTS = "detection_events"
    SNAPSHOTS = "snapshots"
    AUDIT_LOG = "audit_log"