from datetime import timedelta

from minio import Minio
from minio.error import S3Error


class StorageClient:
    def __init__(
        self,
        endpoint,
        access_key,
        secret_key,
        secure=False
    ):
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure
        )

        self.buckets = [
            "snapshots",
            "reports",
            "clips"
        ]

        for bucket in self.buckets:
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

    def upload(
        self,
        bucket_name,
        object_name,
        file_path
    ):
        self.client.fput_object(
            bucket_name,
            object_name,
            file_path
        )

    def download(
        self,
        bucket_name,
        object_name,
        file_path
    ):
        self.client.fget_object(
            bucket_name,
            object_name,
            file_path
        )

    def presigned_url(
        self,
        bucket_name,
        object_name,
        expiry_hours=1
    ):
        return self.client.presigned_get_object(
            bucket_name,
            object_name,
            expires=timedelta(hours=expiry_hours)
        )

    def object_exists(
        self,
        bucket_name,
        object_name
    ):
        try:
            self.client.stat_object(
                bucket_name,
                object_name
            )
            return True
        except S3Error:
            return False