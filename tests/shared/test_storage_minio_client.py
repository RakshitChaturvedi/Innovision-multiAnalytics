from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from minio.error import S3Error

from shared.storage.storage_minio_client import MinioSettings, StorageClient, StorageDisabled


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "admin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "password123")
    monkeypatch.setenv("MINIO_SECURE", "false")


@pytest.fixture
def mock_minio():
    with patch("shared.storage.storage_minio_client.Minio") as mock_minio_cls:
        mock_client = MagicMock()
        mock_minio_cls.return_value = mock_client
        yield mock_minio_cls, mock_client


@pytest.fixture
def storage(mock_env, mock_minio):
    _, mock_client = mock_minio
    return StorageClient()


# ─────────────────────────────────────────────────────────────────────────────
# Initialization Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestInitialization:

    @pytest.fixture(autouse=True)
    def _no_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no stray .env from the repo root

    def test_creates_client_with_env_vars(self, mock_env, mock_minio):
        mock_minio_cls, mock_client = mock_minio

        StorageClient().client

        kwargs = mock_minio_cls.call_args.kwargs
        assert (kwargs["endpoint"], kwargs["access_key"], kwargs["secret_key"], kwargs["secure"]) == (
            "localhost:9000", "admin", "password123", False,
        )
        assert kwargs["http_client"] is not None  # short timeouts, bounded retries

    def test_secure_true_when_env_set(self, mock_minio, monkeypatch):
        mock_minio_cls, mock_client = mock_minio
        monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
        monkeypatch.setenv("MINIO_SECURE", "true")

        StorageClient().client

        assert mock_minio_cls.call_args.kwargs["secure"] is True

    def test_missing_env_uses_service_defaults_not_keyerror(self, mock_minio, monkeypatch):
        """Before: os.environ["MINIO_*"] on first use -> KeyError on every frame."""
        mock_minio_cls, _ = mock_minio
        for var in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_SECURE"):
            monkeypatch.delenv(var, raising=False)

        storage = StorageClient()
        storage.client  # must not raise

        kwargs = mock_minio_cls.call_args.kwargs
        assert (kwargs["endpoint"], kwargs["access_key"], kwargs["secret_key"]) == (
            "minio:9000", "minioadmin", "minioadmin",
        )
        assert storage.enabled

    def test_env_is_read_once_at_construction(self, mock_env, mock_minio, monkeypatch):
        mock_minio_cls, _ = mock_minio
        storage = StorageClient()
        monkeypatch.setenv("MINIO_ENDPOINT", "elsewhere:9000")
        storage.client
        assert mock_minio_cls.call_args.kwargs["endpoint"] == "localhost:9000"

    def test_empty_endpoint_disables_storage(self, mock_minio, monkeypatch):
        monkeypatch.setenv("MINIO_ENDPOINT", "")
        storage = StorageClient()
        assert storage.enabled is False
        with pytest.raises(StorageDisabled):
            storage.client

    def test_settings_can_be_passed_explicitly(self, mock_minio):
        mock_minio_cls, _ = mock_minio
        StorageClient(MinioSettings(minio_endpoint="h:1", minio_access_key="a",
                                    minio_secret_key="b")).client
        assert mock_minio_cls.call_args.kwargs["endpoint"] == "h:1"

    def test_buckets_dict_set_correctly(self, storage):
        assert storage.buckets == {
            "frames": "innovision-frames",
            "snapshots": "innovision-snapshots",
            "reports": "innovision-reports",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Upload Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestUpload:

    def test_upload_calls_put_object(self, storage, mock_minio):
        _, mock_client = mock_minio
        data = b"hello world"

        storage.upload(
            bucket_key="snapshots",
            object_key="frames/cam_01/000001.jpg",
            data=data,
            content_type="image/jpeg",
        )

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["bucket_name"] == "innovision-snapshots"
        assert call_kwargs["object_name"] == "frames/cam_01/000001.jpg"
        assert call_kwargs["length"] == len(data)
        assert call_kwargs["content_type"] == "image/jpeg"
        assert isinstance(call_kwargs["data"], BytesIO)

    def test_upload_returns_object_key(self, storage, mock_minio):
        result = storage.upload(
            bucket_key="reports",
            object_key="reports/2026-06/rpt-001.pdf",
            data=b"pdf bytes",
        )
        assert result == "reports/2026-06/rpt-001.pdf"

    def test_upload_default_content_type(self, storage, mock_minio):
        _, mock_client = mock_minio
        storage.upload(bucket_key="frames", object_key="clips/cam_01/clip.mp4", data=b"data")
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["content_type"] == "application/octet-stream"

    def test_upload_invalid_bucket_key_raises(self, storage, mock_minio):
        with pytest.raises(KeyError):
            storage.upload(bucket_key="nope", object_key="x.jpg", data=b"data")


# ─────────────────────────────────────────────────────────────────────────────
# Download Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDownload:

    def test_download_calls_get_object(self, storage, mock_minio):
        _, mock_client = mock_minio
        mock_response = MagicMock()
        mock_response.read.return_value = b"file contents"
        mock_client.get_object.return_value = mock_response

        result = storage.download(bucket_key="snapshots", object_key="frames/cam_01/000001.jpg")

        mock_client.get_object.assert_called_once_with(
            "innovision-snapshots", "frames/cam_01/000001.jpg"
        )
        assert result == b"file contents"
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    def test_download_invalid_bucket_key_raises(self, storage, mock_minio):
        with pytest.raises(KeyError):
            storage.download(bucket_key="nope", object_key="x.jpg")


# ─────────────────────────────────────────────────────────────────────────────
# Presigned URL Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPresignedUrl:

    def test_presigned_url_returns_url(self, storage, mock_minio):
        _, mock_client = mock_minio
        expected_url = "https://example.com/report.pdf"
        mock_client.presigned_get_object.return_value = expected_url

        result = storage.presigned_url(bucket_key="reports", object_key="report.pdf")

        assert result == expected_url

    def test_presigned_url_default_expiry_one_hour(self, storage, mock_minio):
        from datetime import timedelta
        _, mock_client = mock_minio

        storage.presigned_url(bucket_key="reports", object_key="report.pdf")

        call_kwargs = mock_client.presigned_get_object.call_args.kwargs
        assert call_kwargs["expires"] == timedelta(hours=1)

    def test_presigned_url_custom_expiry(self, storage, mock_minio):
        from datetime import timedelta
        _, mock_client = mock_minio

        storage.presigned_url(bucket_key="reports", object_key="report.pdf", expires_hours=24)

        call_kwargs = mock_client.presigned_get_object.call_args.kwargs
        assert call_kwargs["expires"] == timedelta(hours=24)

    def test_presigned_url_uses_correct_bucket_and_key(self, storage, mock_minio):
        _, mock_client = mock_minio

        storage.presigned_url(bucket_key="snapshots", object_key="alerts/2026-06-12/abc.jpg")

        call_kwargs = mock_client.presigned_get_object.call_args.kwargs
        assert call_kwargs["bucket_name"] == "innovision-snapshots"
        assert call_kwargs["object_name"] == "alerts/2026-06-12/abc.jpg"


# ─────────────────────────────────────────────────────────────────────────────
# Object Exists Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestObjectExists:

    def test_object_exists_true(self, storage, mock_minio):
        _, mock_client = mock_minio
        mock_client.stat_object.return_value = MagicMock()

        result = storage.object_exists(bucket_key="reports", object_key="report.pdf")

        assert result is True
        mock_client.stat_object.assert_called_once_with("innovision-reports", "report.pdf")

    def test_object_exists_false_on_s3_error(self, storage, mock_minio):
        _, mock_client = mock_minio
        mock_client.stat_object.side_effect = S3Error(
            code="NoSuchKey",
            message="Object not found",
            resource="",
            request_id="",
            host_id="",
            response={},
        )

        result = storage.object_exists(bucket_key="reports", object_key="missing.pdf")

        assert result is False

    def test_object_exists_invalid_bucket_key_raises(self, storage, mock_minio):
        with pytest.raises(KeyError):
            storage.object_exists(bucket_key="nope", object_key="x.pdf")