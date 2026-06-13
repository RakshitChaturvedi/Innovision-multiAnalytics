"""
tests/shared/test_minio_client.py

Tests for shared/storage/minio_client.py

Run with:
    pytest tests/shared/test_minio_client.py -v

Requirements:
    - MinIO must be running (docker-compose up -d minio)
    - pip install pytest minio python-dotenv
"""

import pytest
from unittest.mock import MagicMock, patch
from shared.storage import minio_client as mc


# ── Key builder tests (no MinIO connection needed) ──────────────────────────────
# These test that key names are always built correctly per contract.

class TestKeyBuilders:

    def test_frame_key_format(self):
        key = mc.key_frame("cam-01", 452)
        assert key == "frames/cam-01/00000452.jpg"

    def test_frame_key_zero_padded_to_8_digits(self):
        key = mc.key_frame("cam-02", 1)
        assert key == "frames/cam-02/00000001.jpg"

    def test_frame_key_large_seq(self):
        key = mc.key_frame("cam-01", 99999999)
        assert key == "frames/cam-01/99999999.jpg"

    def test_alert_snapshot_key_format(self):
        key = mc.key_alert_snapshot("intrusion", "2026-06-12", "abc123")
        assert key == "alerts/intrusion/2026-06-12/abc123.jpg"

    def test_alert_snapshot_key_different_types(self):
        assert mc.key_alert_snapshot("motion", "2026-06-12", "xyz") == \
               "alerts/motion/2026-06-12/xyz.jpg"
        assert mc.key_alert_snapshot("crowd", "2026-01-01", "id1") == \
               "alerts/crowd/2026-01-01/id1.jpg"

    def test_report_key_format(self):
        key = mc.key_report("2026-06", "rpt-001")
        assert key == "reports/2026-06/rpt-001.pdf"

    def test_clip_key_format(self):
        key = mc.key_clip("cam-01", "2026-06-12", "14-05-32")
        assert key == "clips/cam-01/2026-06-12/14-05-32.mp4"

    def test_no_uuid_as_top_level_prefix(self):
        """Contract: UUIDs are never top-level prefixes."""
        import re
        uuid_pattern = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            re.IGNORECASE
        )
        keys = [
            mc.key_frame("cam-01", 1),
            mc.key_alert_snapshot("intrusion", "2026-06-12", "some-uuid-here"),
            mc.key_report("2026-06", "some-uuid-here"),
            mc.key_clip("cam-01", "2026-06-12", "14-05-32"),
        ]
        for key in keys:
            assert not uuid_pattern.match(key), \
                f"Key '{key}' starts with a UUID — violates contract"


# ── Upload / presigned URL tests (MinIO mocked) ─────────────────────────────────
# We mock the MinIO client so these tests run without a real server.

class TestUploadOperations:

    @patch("shared.storage.minio_client._get_client")
    def test_upload_frame_calls_put_object(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        key = mc.upload_frame("cam-01", 5, b"fakejpeg")

        assert key == "frames/cam-01/00000005.jpg"
        mock_client.put_object.assert_called_once()
        call_args = mock_client.put_object.call_args
        assert call_args[0][0] == mc.BUCKET_SNAPSHOTS      # correct bucket
        assert call_args[0][1] == "frames/cam-01/00000005.jpg"  # correct key
        assert call_args[1]["content_type"] == "image/jpeg"

    @patch("shared.storage.minio_client._get_client")
    def test_upload_alert_snapshot_goes_to_snapshots_bucket(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        key = mc.upload_alert_snapshot("intrusion", "2026-06-12", "alert-1", b"fakejpeg")

        assert key == "alerts/intrusion/2026-06-12/alert-1.jpg"
        call_args = mock_client.put_object.call_args
        assert call_args[0][0] == mc.BUCKET_SNAPSHOTS
        assert call_args[1]["content_type"] == "image/jpeg"

    @patch("shared.storage.minio_client._get_client")
    def test_upload_report_goes_to_reports_bucket(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        key = mc.upload_report("2026-06", "rpt-001", b"%PDF-fake")

        assert key == "reports/2026-06/rpt-001.pdf"
        call_args = mock_client.put_object.call_args
        assert call_args[0][0] == mc.BUCKET_REPORTS
        assert call_args[1]["content_type"] == "application/pdf"

    @patch("shared.storage.minio_client._get_client")
    def test_upload_returns_key_for_db_storage(self, mock_get_client):
        """Returned key must be storable in DB (not a full URL)."""
        mock_get_client.return_value = MagicMock()

        key = mc.upload_frame("cam-01", 1, b"bytes")

        assert key.startswith("frames/")
        assert "localhost" not in key  # not a URL
        assert "http" not in key


class TestPresignedUrls:

    @patch("shared.storage.minio_client._get_client")
    def test_alert_snapshot_ttl_is_1_hour(self, mock_get_client):
        """Contract: alert snapshot presigned URLs expire in 1 hour."""
        from datetime import timedelta
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mc.presigned_alert_snapshot("intrusion", "2026-06-12", "alert-1")

        call_kwargs = mock_client.presigned_get_object.call_args[1]
        assert call_kwargs["expires"] == timedelta(hours=1)

    @patch("shared.storage.minio_client._get_client")
    def test_report_ttl_is_24_hours(self, mock_get_client):
        """Contract: report presigned URLs expire in 24 hours."""
        from datetime import timedelta
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mc.presigned_report("2026-06", "rpt-001")

        call_kwargs = mock_client.presigned_get_object.call_args[1]
        assert call_kwargs["expires"] == timedelta(hours=24)

    @patch("shared.storage.minio_client._get_client")
    def test_presigned_alert_uses_correct_bucket(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mc.presigned_alert_snapshot("motion", "2026-06-12", "a1")

        call_args = mock_client.presigned_get_object.call_args[0]
        assert call_args[0] == mc.BUCKET_SNAPSHOTS

    @patch("shared.storage.minio_client._get_client")
    def test_presigned_report_uses_correct_bucket(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mc.presigned_report("2026-06", "rpt-001")

        call_args = mock_client.presigned_get_object.call_args[0]
        assert call_args[0] == mc.BUCKET_REPORTS


# ── Integration test (needs real MinIO running) ─────────────────────────────────
# Run only when MinIO is up: pytest -m integration

@pytest.mark.integration
class TestIntegration:

    def test_ensure_buckets_creates_all_three(self):
        """All 3 buckets must exist after ensure_buckets()."""
        mc.ensure_buckets()
        client = mc._get_client()
        assert client.bucket_exists(mc.BUCKET_SNAPSHOTS)
        assert client.bucket_exists(mc.BUCKET_REPORTS)
        assert client.bucket_exists(mc.BUCKET_CLIPS)

    def test_upload_and_retrieve_frame(self):
        """Upload a frame and verify it exists in MinIO."""
        mc.ensure_buckets()
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # JPEG magic bytes

        key = mc.upload_frame("test-cam", 1, fake_jpeg)

        client = mc._get_client()
        obj = client.get_object(mc.BUCKET_SNAPSHOTS, key)
        data = obj.read()
        assert data == fake_jpeg

        # cleanup
        client.remove_object(mc.BUCKET_SNAPSHOTS, key)

    def test_upload_and_presign_alert_snapshot(self):
        """Upload alert snapshot and get a presigned URL back."""
        mc.ensure_buckets()
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        mc.upload_alert_snapshot("intrusion", "2026-06-12", "test-alert-1", fake_jpeg)
        url = mc.presigned_alert_snapshot("intrusion", "2026-06-12", "test-alert-1")

        assert url.startswith("http")
        assert "innovision-snapshots" in url

        # cleanup
        mc._get_client().remove_object(
            mc.BUCKET_SNAPSHOTS,
            mc.key_alert_snapshot("intrusion", "2026-06-12", "test-alert-1")
        )
