"""
backend/services/storage_service.py

S3 abstraction layer.
All file uploads and downloads go through this service.
Never call boto3 directly from any router or service.

S3 key structure:
    {depot_id}/invoices/{invoice_id}/{filename}
    {depot_id}/temperature/{date}/{filename}
    {depot_id}/returns/{return_id}/{filename}
    {depot_id}/compliance-reports/{report_type}/{YYYY-MM}/{filename}
    models/{model_name}/{version}.pkl
"""

import logging
import mimetypes
import uuid
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from backend.config import settings

logger = logging.getLogger("flowsync.services.storage")

# Signed URL expiry — 24 hours for compliance report downloads
SIGNED_URL_EXPIRY_SECS = 86_400


class StorageService:
    """
    Usage:
        storage = StorageService()
        url     = await storage.upload(file_bytes, "invoices", depot_id, invoice_id)
        signed  = storage.get_signed_url(s3_key)
    """

    def __init__(self):
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client(
                "s3",
                aws_access_key_id     = settings.aws_access_key,
                aws_secret_access_key = settings.aws_secret_key,
                region_name           = settings.aws_region,
            )
        return self._s3

    def upload_bytes(
        self,
        data:        bytes,
        folder:      str,
        depot_id:    str,
        entity_id:   Optional[str] = None,
        filename:    Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> str:
        """
        Upload raw bytes to S3.

        Args:
            data:         file content as bytes
            folder:       'invoices' | 'temperature' | 'returns' | 'compliance-reports'
            depot_id:     UUID string
            entity_id:    invoice_id, return_id etc. (optional subfolder)
            filename:     original filename (auto-generated if None)
            content_type: MIME type (auto-detected if None)

        Returns:
            S3 key string (store this in DB, not the full URL)
        """
        filename     = filename or f"{uuid.uuid4()}.bin"
        content_type = content_type or _detect_content_type(filename)

        if entity_id:
            key = f"{depot_id}/{folder}/{entity_id}/{filename}"
        else:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            key = f"{depot_id}/{folder}/{date_str}/{filename}"

        try:
            self.s3.put_object(
                Bucket      = settings.s3_bucket,
                Key         = key,
                Body        = data,
                ContentType = content_type,
            )
            logger.info(
                f"Uploaded {len(data)} bytes → "
                f"s3://{settings.s3_bucket}/{key}"
            )
            return key

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            raise RuntimeError(f"File upload failed: {e}")

    def get_signed_url(
        self,
        s3_key:      str,
        expiry_secs: int = SIGNED_URL_EXPIRY_SECS,
    ) -> str:
        """
        Generate a pre-signed URL for secure file download.
        URL expires after expiry_secs (default 24 hours).
        Use for compliance report downloads.

        Args:
            s3_key:      key returned by upload_bytes()
            expiry_secs: URL lifetime in seconds

        Returns:
            Pre-signed HTTPS URL string
        """
        try:
            url = self.s3.generate_presigned_url(
                "get_object",
                Params     = {
                    "Bucket": settings.s3_bucket,
                    "Key":    s3_key,
                },
                ExpiresIn  = expiry_secs,
            )
            return url
        except ClientError as e:
            logger.error(f"Signed URL generation failed: {e}")
            raise RuntimeError(f"Could not generate download URL: {e}")

    def delete(self, s3_key: str) -> bool:
        """
        Delete a file from S3.
        Returns True on success.
        Never called on audit_trail or temperature_log photos —
        those must be immutable.
        """
        try:
            self.s3.delete_object(
                Bucket = settings.s3_bucket,
                Key    = s3_key,
            )
            logger.info(f"Deleted s3://{settings.s3_bucket}/{s3_key}")
            return True
        except ClientError as e:
            logger.error(f"S3 delete failed: {e}")
            return False

    def get_public_url(self, s3_key: str) -> str:
        """
        Return public URL for non-sensitive files.
        Only use for files that don't need access control.
        """
        return (
            f"https://{settings.s3_bucket}.s3."
            f"{settings.aws_region}.amazonaws.com/{s3_key}"
        )


def _detect_content_type(filename: str) -> str:
    """Detect MIME type from filename extension."""
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


# Module-level singleton — import and use directly
storage = StorageService()