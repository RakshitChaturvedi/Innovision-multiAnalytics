from enum import Enum

"""
class CameraProfile(str, Enum):
    HIGH_SECURTY = "high_security"
    BALANCED = "balanced"
"""

class FrameProvider(str, Enum):
    MINIO = "minio"
    REDIS = "redis"

class AlertSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class AlertStatus(str, Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"

class IncidentStatus(str, Enum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"

class SourceUC(str, Enum):
    UC1 = "uc1"
    UC2 = "uc2"
    UC3 = "uc3"
    UC4 = "uc4"

class CameraStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"
    DISABLED = "disabled"

class OperatorRole(str, Enum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
