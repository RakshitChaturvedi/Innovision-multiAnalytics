from unittest.mock import MagicMock, patch
from datetime import timedelta

from minio.error import S3Error

from shared.storage.storage_minio_client import StorageClient

# Verifies that the MinIO client is initialized with the provided configuration.
@patch("shared.storage.storage_minio_client.Minio")
def test_initialization_creates_client(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
        secure=False,
    )

    mock_minio.assert_called_once_with(
        "localhost:9000",
        access_key="admin",
        secret_key="password",
        secure=False,
    )

# Verifies that required buckets are automatically created when they do not already exist.
@patch("shared.storage.storage_minio_client.Minio")
def test_initialization_creates_missing_buckets(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = False

    StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
        secure=False,
    )

    assert mock_client.make_bucket.call_count == 3

    mock_client.make_bucket.assert_any_call("snapshots")
    mock_client.make_bucket.assert_any_call("reports")
    mock_client.make_bucket.assert_any_call("clips")

# Verifies that upload delegates file uploads to the MinIO SDK with the correct parameters.
@patch("shared.storage.storage_minio_client.Minio")
def test_upload(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    storage = StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
    )

    storage.upload(
        bucket_name="reports",
        object_name="report.pdf",
        file_path="/tmp/report.pdf",
    )

    mock_client.fput_object.assert_called_once_with(
        "reports",
        "report.pdf",
        "/tmp/report.pdf",
    )

# Verifies that download delegates file retrieval to the MinIO SDK with the correct parameters.
@patch("shared.storage.storage_minio_client.Minio")
def test_download(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    storage = StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
    )

    storage.download(
        bucket_name="reports",
        object_name="report.pdf",
        file_path="/tmp/downloaded.pdf",
    )

    mock_client.fget_object.assert_called_once_with(
        "reports",
        "report.pdf",
        "/tmp/downloaded.pdf",
    )

# Verifies that a presigned URL is generated and returned correctly.
@patch("shared.storage.storage_minio_client.Minio")
def test_presigned_url(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    expected_url = "https://example.com/report.pdf"

    mock_client.presigned_get_object.return_value = expected_url

    storage = StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
    )

    result = storage.presigned_url(
        bucket_name="reports",
        object_name="report.pdf",
    )

    assert result == expected_url

    mock_client.presigned_get_object.assert_called_once()

# Verifies that object_exists returns True when the object is present in storage.
@patch("shared.storage.storage_minio_client.Minio")
def test_object_exists_true(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    storage = StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
    )

    assert storage.object_exists(
        "reports",
        "report.pdf",
    ) is True

    mock_client.stat_object.assert_called_once_with(
        "reports",
        "report.pdf",
    )

# Verifies that object_exists returns False when the object cannot be found.
@patch("shared.storage.storage_minio_client.Minio")
def test_object_exists_false(mock_minio):
    mock_client = MagicMock()
    mock_minio.return_value = mock_client

    mock_client.bucket_exists.return_value = True

    mock_client.stat_object.side_effect = S3Error(
        code="NoSuchKey",
        message="Object not found",
        resource="",
        request_id="",
        host_id="",
        response={}
    )
    
    storage = StorageClient(
        endpoint="localhost:9000",
        access_key="admin",
        secret_key="password",
    )

    assert storage.object_exists(
        "reports",
        "missing.pdf",
    ) is False
