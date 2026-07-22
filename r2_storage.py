import os
import logging
import asyncio
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

class R2Storage:
    def __init__(self):
        self.account_id = os.getenv("R2_ACCOUNT_ID")
        self.access_key = os.getenv("R2_ACCESS_KEY_ID")
        self.secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        self.bucket_name = os.getenv("R2_BUCKET_NAME")
        
        self.enabled = bool(
            self.account_id and self.access_key and self.secret_key and self.bucket_name
        )
        
        if self.enabled:
            endpoint_url = f"https://{self.account_id}.r2.cloudflarestorage.com"
            self.client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto"
            )
            logger.info(f"Cloudflare R2 Storage initialized for bucket: {self.bucket_name}")
        else:
            self.client = None
            logger.warning("Cloudflare R2 credentials not fully set. Running without R2 sync.")

    def _download_file(self, object_name: str, target_path: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.download_file(self.bucket_name, object_name, target_path)
            logger.info(f"Successfully downloaded {object_name} from R2 to {target_path}")
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "404":
                logger.info(f"Object {object_name} not found in R2 bucket.")
            else:
                logger.error(f"Error downloading {object_name} from R2: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error downloading {object_name}: {e}")
            return False

    def _upload_file(self, local_path: str, object_name: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.client.upload_file(local_path, self.bucket_name, object_name)
            logger.info(f"Successfully uploaded {local_path} to R2 as {object_name}")
            return True
        except Exception as e:
            logger.error(f"Error uploading {local_path} to R2: {e}")
            return False

    def _upload_bytes(self, data: bytes, object_name: str, content_type: str = "image/jpeg") -> bool:
        if not self.enabled:
            return False
        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=object_name,
                Body=data,
                ContentType=content_type
            )
            logger.info(f"Successfully uploaded bytes to R2 as {object_name}")
            return True
        except Exception as e:
            logger.error(f"Error uploading bytes to R2: {e}")
            return False

    async def download_db(self, db_path: str = "minibar.db") -> bool:
        return await asyncio.to_thread(self._download_file, db_path, db_path)

    async def upload_db(self, db_path: str = "minibar.db") -> bool:
        return await asyncio.to_thread(self._upload_file, db_path, db_path)

    async def upload_photo(self, photo_bytes: bytes, object_name: str) -> bool:
        return await asyncio.to_thread(self._upload_bytes, photo_bytes, object_name)
