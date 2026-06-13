"""
shared/storage/minio_client.py

Single entry point for all MinIO operations across the Innovision platform.

Contract rules enforced here:
  - Services write directly via this module. Clients never get direct access.
  - All object keys are deterministic (no UUIDs as top-level prefix).
  - Presigned URL TTL: 1 hour for alert snapshots, 24 hours for reports.
  - Presigned URLs are generated on dashboard load if expired.
"""

import io
import os
from datetime import timedelta

from dotenv import load_dotenv
from minio import Minio
from minio.error import S3Error

load_dotenv()

# ── Config (read from .env) ────────────────────────────────────────────────────
_ENDPOINT   = os.getenv("MINIO_ENDPOINT",   "localhost:9000")
_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "admin")
_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "password123")
_SECURE     = os.getenv("MINIO_SECURE",     "false").lower() == "true"

# ── Bucket names (from contract) ───────────────────────────────────────────────
BUCKET_SNAPSHOTS = "innovision-snapshots"   # frame JPEGs + alert snapshots
BUCKET_REPORTS   = "innovision-reports"     # generated PDF reports
BUCKET_CLIPS     = "innovision-clips"       # video clips (reserved, future)

ALL_BUCKETS = [BUCKET_SNAPSHOTS, BUCKET_REPORTS, BUCKET_CLIPS]

# ── Presigned URL TTLs (from contract) ─────────────────────────────────────────
TTL_ALERT_SNAPSHOT = timedelta(hours=1)    # alert snapshots: 1 hour
TTL_REPORT         = timedelta(hours=24)   # reports: 24 hours

# ── Internal client (singleton) ────────────────────────────────────────────────
_client: Minio = None


def _get_client() -> Minio:
    """Return the singleton MinIO client, creating it on first call."""
    global _client
    if _client is None:
        _client = Minio(
            _ENDPOINT,
            access_key=_ACCESS_KEY,
            secret_key=_SECRET_KEY,
            secure=_SECURE,
        )
    return _client


def ensure_buckets() -> None:
    """
    Create all required buckets if they do not already exist.
    Safe to call multiple times — will not duplicate buckets.
    Called automatically on first use.
    """
    client = _get_client()
    for bucket in ALL_BUCKETS:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            print(f"[minio] Created bucket: {bucket}")
        else:
            print(f"[minio] Bucket already exists: {bucket}")


# ── Object key builders (deterministic, per contract) ──────────────────────────
# UUIDs are NEVER top-level prefixes.

def key_frame(camera_id: str, frame_seq: int) -> str:
    """
    frames/{camera_id}/{frame_seq:08d}.jpg
    e.g. frames/cam-01/00000452.jpg
    """
    return f"frames/{camera_id}/{frame_seq:08d}.jpg"


def key_alert_snapshot(alert_type: str, date: str, alert_id: str) -> str:
    """
    alerts/{alert_type}/{YYYY-MM-DD}/{alert_id}.jpg
    e.g. alerts/intrusion/2026-06-12/abc123.jpg
    """
    return f"alerts/{alert_type}/{date}/{alert_id}.jpg"


def key_report(year_month: str, report_id: str) -> str:
    """
    reports/{YYYY-MM}/{report_id}.pdf
    e.g. reports/2026-06/rpt-001.pdf
    """
    return f"reports/{year_month}/{report_id}.pdf"


def key_clip(camera_id: str, date: str, start_timestamp: str) -> str:
    """
    clips/{camera_id}/{YYYY-MM-DD}/{start_timestamp}.mp4
    e.g. clips/cam-01/2026-06-12/14-05-32.mp4
    """
    return f"clips/{camera_id}/{date}/{start_timestamp}.mp4"


# ── Internal helper ─────────────────────────────────────────────────────────────

def _put_bytes(bucket: str, key: str, data: bytes, content_type: str) -> None:
    """Upload raw bytes to a bucket under the given key."""
    _get_client().put_object(
        bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


# ── Write operations ────────────────────────────────────────────────────────────

def upload_frame(camera_id: str, frame_seq: int, jpeg_bytes: bytes) -> str:
    """
    Upload a camera frame JPEG to innovision-snapshots.

    Args:
        camera_id:   Camera identifier, e.g. "cam-01"
        frame_seq:   Frame sequence number, e.g. 452
        jpeg_bytes:  Raw JPEG bytes (e.g. from cv2.imencode)

    Returns:
        Object key, e.g. "frames/cam-01/00000452.jpg"
        Store this key in the DB — never store the full URL.
    """
    key = key_frame(camera_id, frame_seq)
    _put_bytes(BUCKET_SNAPSHOTS, key, jpeg_bytes, "image/jpeg")
    return key


def upload_alert_snapshot(
    alert_type: str, date: str, alert_id: str, jpeg_bytes: bytes
) -> str:
    """
    Upload an alert snapshot JPEG to innovision-snapshots.

    Args:
        alert_type:  e.g. "intrusion", "motion", "crowd"
        date:        ISO date string, e.g. "2026-06-12"
        alert_id:    Unique alert identifier
        jpeg_bytes:  Raw JPEG bytes

    Returns:
        Object key, e.g. "alerts/intrusion/2026-06-12/abc123.jpg"
    """
    key = key_alert_snapshot(alert_type, date, alert_id)
    _put_bytes(BUCKET_SNAPSHOTS, key, jpeg_bytes, "image/jpeg")
    return key


def upload_report(year_month: str, report_id: str, pdf_bytes: bytes) -> str:
    """
    Upload a generated PDF report to innovision-reports.

    Args:
        year_month:  e.g. "2026-06"
        report_id:   Unique report identifier
        pdf_bytes:   Raw PDF bytes

    Returns:
        Object key, e.g. "reports/2026-06/rpt-001.pdf"
    """
    key = key_report(year_month, report_id)
    _put_bytes(BUCKET_REPORTS, key, pdf_bytes, "application/pdf")
    return key


def upload_clip(
    camera_id: str, date: str, start_timestamp: str, file_path: str
) -> str:
    """
    Upload a video clip from disk to innovision-clips.

    Args:
        camera_id:        e.g. "cam-01"
        date:             e.g. "2026-06-12"
        start_timestamp:  e.g. "14-05-32"
        file_path:        Local path to the .mp4 file

    Returns:
        Object key, e.g. "clips/cam-01/2026-06-12/14-05-32.mp4"
    """
    key = key_clip(camera_id, date, start_timestamp)
    _get_client().fput_object(BUCKET_CLIPS, key, file_path, "video/mp4")
    return key


# ── Presigned URL generation ────────────────────────────────────────────────────
# Clients NEVER access MinIO directly.
# API server calls these and returns the URL to the dashboard.

def presigned_alert_snapshot(
    alert_type: str, date: str, alert_id: str
) -> str:
    """
    Generate a 1-hour presigned GET URL for an alert snapshot.
    Call this on dashboard load if the previous URL has expired.

    Returns:
        Temporary URL valid for 1 hour.
    """
    key = key_alert_snapshot(alert_type, date, alert_id)
    return _get_client().presigned_get_object(
        BUCKET_SNAPSHOTS, key, expires=TTL_ALERT_SNAPSHOT
    )


def presigned_report(year_month: str, report_id: str) -> str:
    """
    Generate a 24-hour presigned GET URL for a report PDF.
    Call this on dashboard load if the previous URL has expired.

    Returns:
        Temporary URL valid for 24 hours.
    """
    key = key_report(year_month, report_id)
    return _get_client().presigned_get_object(
        BUCKET_REPORTS, key, expires=TTL_REPORT
    )
