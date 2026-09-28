import os
import logging

from datetime import timedelta
from io import BytesIO
from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)

BUCKETS = {
    "frames": "innovision-frames",
    "snapshots": "innovision-snapshots",
    "reports": "innovision-reports",
}


class StorageClient:
    def __init__(self):
        # env is read on first use, not at construction/import time
        self._client: Minio | None = None
        self.buckets = dict(BUCKETS)

    @property
    def client(self) -> Minio:
        if self._client is None:
            self._client = Minio(
                endpoint=os.environ["MINIO_ENDPOINT"],
                access_key=os.environ["MINIO_ACCESS_KEY"],
                secret_key=os.environ["MINIO_SECRET_KEY"],
                secure=os.environ.get("MINIO_SECURE", "false").lower() == "true"
            )
        return self._client

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
