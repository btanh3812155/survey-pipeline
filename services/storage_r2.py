import os
import aiofiles
from config import settings

try:
    import aioboto3
except ImportError:
    aioboto3 = None


class R2StorageClient:
    def __init__(self):
        self.bucket = settings.R2_BUCKET_NAME
        self.is_configured = bool(
            aioboto3
            and settings.R2_ACCOUNT_ID
            and settings.R2_ACCESS_KEY_ID
            and settings.R2_SECRET_ACCESS_KEY
        )
        self.endpoint_url = (
            f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
            if settings.R2_ACCOUNT_ID
            else None
        )
        self.session = aioboto3.Session() if self.is_configured else None

        # local fallback directory
        self.local_dir = os.path.join(os.getcwd(), "data", "uploads")
        if not self.is_configured:
            os.makedirs(self.local_dir, exist_ok=True)

    async def upload_image(self, file_name: str, webp_bytes: bytes) -> str:
        """
        Uploads image to Cloudflare R2 asynchronously.
        Falls back to local file storage if R2 is not configured.
        """
        if self.is_configured:
            async with self.session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            ) as s3:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=file_name,
                    Body=webp_bytes,
                    ContentType="image/webp",
                )
            return f"{settings.R2_PUBLIC_URL_PREFIX}/{file_name}"
        else:
            # local storage for offline development
            local_path = os.path.join(self.local_dir, file_name)
            async with aiofiles.open(local_path, "wb") as f:
                await f.write(webp_bytes)
            return f"/local-storage/{file_name}"

    async def archive_sheet(self, file_name: str, csv_bytes: bytes) -> str:
        """
        Uploads master spreadsheet archive to Cloudflare R2 under archives/sheets/{file_name}.
        Falls back to saving in data/archives/sheets/{file_name} if R2 is not configured.
        """
        clean_name = os.path.basename(file_name)
        key = f"archives/sheets/{clean_name}"
        if self.is_configured:
            async with self.session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            ) as s3:
                await s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=csv_bytes,
                    ContentType="text/csv; charset=utf-8",
                )
            return f"{settings.R2_PUBLIC_URL_PREFIX}/{key}"
        else:
            local_path = os.path.join(os.getcwd(), "data", "archives", "sheets", clean_name)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            async with aiofiles.open(local_path, "wb") as f:
                await f.write(csv_bytes)
            return local_path


r2_client = R2StorageClient()
