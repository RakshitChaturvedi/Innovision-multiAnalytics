import os
from dotenv import load_dotenv
from minio import Minio

load_dotenv()

def create_buckets():
    client = Minio(
        os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("MINIO_ACCESS_KEY", "admin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "password123"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )

    buckets = [
        "innovision-snapshots",
        "innovision-reports",
        "innovision-clips",
    ]

    for bucket in buckets:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            print(f"[OK] Created bucket: {bucket}")
        else:
            print(f"[--] Already exists: {bucket}")

if __name__ == "__main__":
    create_buckets()