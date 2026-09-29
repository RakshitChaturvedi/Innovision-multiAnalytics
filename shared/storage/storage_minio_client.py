import asyncio
import logging
from datetime import timedelta
from io import BytesIO

import certifi
import urllib3
from minio import Minio
from minio.error import S3Error
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

BUCKETS = {
    "frames": "innovision-frames",
    "snapshots": "innovision-snapshots",
    "reports": "innovision-reports",
}


class MinioSettings(BaseSettings):
    """MINIO_* from the environment (or .env), read ONCE per StorageClient.
    Same defaults as the service configs. MINIO_ENDPOINT="" disables MinIO."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    # Short, so an unreachable MinIO costs a frame seconds, not minutes.
    minio_timeout_s: float = 3.0


class StorageDisabled(RuntimeError):
    """MinIO is not configured (MINIO_ENDPOINT is empty)."""


class StorageClient:
    def __init__(self, settings: MinioSettings | None = None):
        # Config is read here, once: never per call (a missing variable used
        # to raise KeyError on every frame).
        self.settings = settings if settings is not None else MinioSettings()
        self.buckets = dict(BUCKETS)
        self.enabled = bool(self.settings.minio_endpoint.strip())
        self._client: Minio | None = None
        # True while MinIO is known to be unreachable; logged once per outage.
        self._outage = False

    @property
    def endpoint(self) -> str:
        return self.settings.minio_endpoint

    @property
    def client(self) -> Minio:
        if not self.enabled:
            raise StorageDisabled("MinIO is not configured (MINIO_ENDPOINT is empty)")
        if self._client is None:
            timeout = self.settings.minio_timeout_s
            http_client = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=timeout, read=timeout),
                retries=urllib3.Retry(
                    total=1, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]
                ),
                cert_reqs="CERT_REQUIRED",
                ca_certs=certifi.where(),
            )
            self._client = Minio(
                endpoint=self.settings.minio_endpoint,
                access_key=self.settings.minio_access_key,
                secret_key=self.settings.minio_secret_key,
                secure=self.settings.minio_secure,
                http_client=http_client,
            )
        return self._client

    # ---------------------------------------------------------- availability

    def mark_unreachable(self, exc: BaseException) -> None:
        """Log at WARNING once per outage (not once per frame)."""
        if not self._outage:
            self._outage = True
            logger.warning(
                "minio_unreachable endpoint=%s error=%r: redis misses are treated "
                "as FrameUnavailable until it is reachable again",
                self.endpoint, exc,
            )

    def mark_reachable(self) -> None:
        if self._outage:
            self._outage = False
            logger.info("minio_reachable_again endpoint=%s", self.endpoint)

    def probe(self) -> None:
        """Blocking reachability check (one request). Raises on failure."""
        self.client.bucket_exists(self.buckets["frames"])

    def upload(
        self,
        bucket_key: str,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        # upload bytes from memory, return obj key
        bucket = self.buckets[bucket_key]

        self.client.put_object(
            bucket_name=bucket,
            object_name=object_key,
            data=BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return object_key

    def download(self, bucket_key: str, object_key: str) -> bytes:
        # download obj, return bytes; always release the connection
        bucket = self.buckets[bucket_key]
        response = self.client.get_object(bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def presigned_url(
        self,
        bucket_key: str,
        object_key: str,
        expires_hours: int=1
    ):
        # generate presigned url for client-side access
        bucket = self.buckets[bucket_key]
        return self.client.presigned_get_object(
            bucket_name=bucket,
            object_name=object_key,
            expires=timedelta(hours=expires_hours),
        )

    def object_exists(self, bucket_key: str, object_key: str) -> bool:
        try:
            bucket = self.buckets[bucket_key]
            self.client.stat_object(bucket, object_key)
            return True
        except S3Error:
            return False


async def open_frame_storage(settings: MinioSettings | None = None) -> StorageClient | None:
    """
    Build the frame cold-store client once at service startup.

    Returns None when MinIO is not configured (logged once): every Redis miss
    is then permanent (FrameUnavailable). When MinIO is configured but not
    reachable the client is still returned, so it recovers by itself; the
    outage is logged once and Redis misses are FrameUnavailable meanwhile.
    """
    storage = StorageClient(settings)
    if not storage.enabled:
        logger.warning(
            "minio_not_configured: MINIO_ENDPOINT is empty, frames missing from "
            "redis are treated as FrameUnavailable"
        )
        return None
    try:
        await asyncio.to_thread(storage.probe)
    except Exception as exc:
        storage.mark_unreachable(exc)
    else:
        logger.info("minio_ready endpoint=%s", storage.endpoint)
    return storage
