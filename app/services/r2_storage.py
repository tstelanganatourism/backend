import logging
import inspect
import time
from typing import Optional
import aiobotocore.session
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

class R2StorageService:
    def __init__(self):
        self.account_id = settings.R2_ACCOUNT_ID
        self.access_key = settings.R2_ACCESS_KEY_ID
        self.secret_key = settings.R2_SECRET_ACCESS_KEY
        self.bucket_name = settings.R2_BUCKET_NAME
        
        self.is_configured = all([
            self.account_id, 
            self.access_key, 
            self.secret_key, 
            self.bucket_name
        ])
        
        self._client = None
        self._client_context = None
        if self.is_configured:
            self.endpoint_url = f"https://{self.account_id}.r2.cloudflarestorage.com"
            self.session = aiobotocore.session.get_session()
            self._signed_url_cache: dict[str, tuple[float, str]] = {}
        else:
            logger.warning("R2StorageService is not fully configured in settings.")

    async def get_client(self):
        if not self.is_configured:
            raise ValueError("R2 Storage is not configured.")
        if self._client is None:
            self._client_context = self.session.create_client(
                's3',
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto"
            )
            self._client = await self._client_context.__aenter__()
        return self._client

    async def close(self):
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self._client = None
            self._client_context = None

    async def upload_file(self, file_content: bytes, object_name: str, content_type: str = "application/pdf") -> str:
        """
        Uploads a file to R2.
        Returns the object_name (the path in the bucket).
        """
        if not self.is_configured:
            raise ValueError("R2 Storage is not configured.")
            
        client = await self.get_client()
        try:
            await client.put_object(
                Bucket=self.bucket_name,
                Key=object_name,
                Body=file_content,
                ContentType=content_type
            )
            return object_name
        except ClientError as e:
            logger.error(f"Failed to upload to R2: {e}")
            raise e

    async def generate_presigned_url(self, object_name: str, expires_in: int = 900) -> str:
        """
        Generates a secure presigned URL for downloading a private object.
        Default expiration is 900 seconds (15 minutes).
        """
        if not self.is_configured:
            raise ValueError("R2 Storage is not configured.")

        now = time.monotonic()
        
        # Purge expired entries to prevent unbounded memory growth
        expired_keys = [k for k, v in self._signed_url_cache.items() if v[0] <= now]
        for k in expired_keys:
            del self._signed_url_cache[k]

        cached = self._signed_url_cache.get(object_name)
        if cached and cached[0] > now:
            return cached[1]
            
        client = await self.get_client()
        try:
            generated = client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': object_name},
                ExpiresIn=expires_in
            )
            url = await generated if inspect.isawaitable(generated) else generated
            self._signed_url_cache[object_name] = (now + max(60, expires_in - 60), url)
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise e

    async def delete_file(self, object_name: str) -> bool:
        """
        Deletes a file from R2. Returns True if successful.
        """
        if not self.is_configured:
            logger.warning(f"R2 Storage not configured. Skipping deletion of {object_name}.")
            return False
            
        client = await self.get_client()
        try:
            await client.delete_object(
                Bucket=self.bucket_name,
                Key=object_name
            )
            return True
        except ClientError as e:
            logger.error(f"Failed to delete {object_name} from R2: {e}")
            return False

    async def get_public_url(self, object_name: Optional[str]) -> Optional[str]:
        """
        Resolves a private R2 object key into a signed download URL.
        If it's already an HTTP URL or blank, returns as is.
        """
        if not object_name:
            return None
        if object_name.startswith("private/"):
            try:
                return await self.generate_presigned_url(object_name)
            except Exception:
                return None
        return object_name

r2_service = R2StorageService()
