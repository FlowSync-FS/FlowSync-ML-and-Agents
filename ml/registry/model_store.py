"""
ml/registry/model_store.py

S3-backed model registry.
Every .pkl stored in S3 with version timestamp.
model_registry table tracks active version per model name.
Rollback: set is_active=False, previous version auto-activates.

All inference files call load() here — never load directly.
All training notebooks call save() here — never joblib.dump() directly.
"""

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import boto3
import joblib
from botocore.exceptions import ClientError

logger = logging.getLogger("flowsync.registry")

# Local fallback directory — used when S3 credentials are not configured (dev env)
_LOCAL_SAVE_DIR = Path(__file__).parent.parent / "models" / "saved"


class ModelStore:
    """
    Usage in training notebook:
        store = ModelStore()
        store.save(model, "demand_global", metadata={"mape": 0.12})
        store.save(model, f"demand_{depot_id}", metadata={"mape": 0.09})

    Usage in inference:
        store = ModelStore()
        model = store.load("demand_global")
        model = store.load(f"demand_{depot_id}", fallback="demand_global")
    """

    def __init__(self, bucket: str = None, db_session=None):
        import os
        self.bucket     = bucket or os.getenv("S3_BUCKET", "flowsync-dev")
        self.db         = db_session
        self._s3        = None  # lazy init

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client("s3")
        return self._s3

    # ── Save ─────────────────────────────────────────────────────────────────

    def save(
        self,
        model: Any,
        model_name: str,
        metadata: dict = None,
    ) -> str:
        """
        Serialize model → S3, falling back to local disk when S3 is unavailable.
        Returns the S3 key or local path string.

        Args:
            model:      Any joblib-serializable object
            model_name: e.g. "demand_global", "expiry_risk", "demand_depot_{id}"
            metadata:   dict of metrics to store (mape, auc, threshold, etc.)
        """
        version = datetime.now().strftime("%Y%m%d_%H%M%S")
        s3_key  = f"models/{model_name}/{version}.pkl"

        buffer = io.BytesIO()
        joblib.dump(model, buffer)
        buffer.seek(0)

        try:
            self.s3.upload_fileobj(buffer, self.bucket, s3_key)
            logger.info(
                f"Saved {model_name} v{version} "
                f"→ s3://{self.bucket}/{s3_key}"
            )
            if self.db:
                self._register(model_name, version, s3_key, metadata or {})
            return s3_key

        except Exception as e:
            logger.warning(
                f"S3 save failed for {model_name} ({e}) — "
                "saving locally (dev mode)"
            )
            return self._save_local(model, model_name, metadata)

    # ── Load ─────────────────────────────────────────────────────────────────

    def load(
        self,
        model_name: str,
        fallback: Optional[str] = None,
    ) -> Any:
        """
        Load active model version from S3, falling back to local disk.
        If model_name not found and fallback provided, tries fallback too.
        Raises FileNotFoundError if neither found anywhere.

        Args:
            model_name: e.g. "demand_global" or "demand_{depot_id}"
            fallback:   e.g. "demand_global" (used when per-depot model missing)
        """
        try:
            s3_key = self._get_active_key(model_name)
            return self._download(s3_key)
        except Exception as e:
            logger.warning(f"S3 load failed for {model_name}: {e} — trying local")

        local_path = _LOCAL_SAVE_DIR / f"{model_name}_latest.pkl"
        if local_path.exists():
            logger.info(f"Loading {model_name} from local: {local_path}")
            data = joblib.load(local_path)
            return data["model"] if isinstance(data, dict) and "model" in data else data

        if fallback:
            logger.info(f"Falling back to {fallback}")
            fallback_local = _LOCAL_SAVE_DIR / f"{fallback}_latest.pkl"
            if fallback_local.exists():
                data = joblib.load(fallback_local)
                return data["model"] if isinstance(data, dict) and "model" in data else data
            try:
                s3_key = self._get_active_key(fallback)
                return self._download(s3_key)
            except Exception:
                pass

        raise FileNotFoundError(
            f"Model not found: {model_name} (checked S3 and local)"
        )

    # ── Rollback ──────────────────────────────────────────────────────────────

    def rollback(self, model_name: str) -> bool:
        """
        Deactivate current model, promote previous version.
        Called when a newly deployed model produces bad predictions.

        Returns True if rollback succeeded.
        """
        if not self.db:
            logger.error("Cannot rollback — no db_session provided")
            return False

        # Deactivate current
        self.db.execute("""
            UPDATE model_registry
            SET is_active = FALSE
            WHERE model_name = :name AND is_active = TRUE
        """, {"name": model_name})

        # Activate most recent inactive version
        self.db.execute("""
            UPDATE model_registry
            SET is_active = TRUE
            WHERE id = (
                SELECT id FROM model_registry
                WHERE model_name = :name AND is_active = FALSE
                ORDER BY trained_at DESC
                LIMIT 1
            )
        """, {"name": model_name})

        self.db.commit()
        logger.warning(
            f"Rolled back {model_name} to previous version"
        )
        return True

    # ── Metadata ──────────────────────────────────────────────────────────────

    def get_metadata(self, model_name: str) -> dict:
        """Return metadata dict for the active version of a model."""
        if not self.db:
            return {}
        row = self.db.execute("""
            SELECT metadata FROM model_registry
            WHERE model_name = :name AND is_active = TRUE
            ORDER BY trained_at DESC LIMIT 1
        """, {"name": model_name}).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row.metadata)
        except Exception:
            return {}

    def list_versions(self, model_name: str) -> list:
        """Return all versions for a model, newest first."""
        if not self.db:
            return []
        rows = self.db.execute("""
            SELECT version, s3_key, trained_at, is_active, metadata
            FROM model_registry
            WHERE model_name = :name
            ORDER BY trained_at DESC
        """, {"name": model_name}).fetchall()
        return [dict(r) for r in rows]

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_active_key(self, model_name: str) -> str:
        """Get S3 key for the active version of a model."""
        if self.db:
            row = self.db.execute("""
                SELECT s3_key FROM model_registry
                WHERE model_name = :name AND is_active = TRUE
                ORDER BY trained_at DESC LIMIT 1
            """, {"name": model_name}).fetchone()
            if row:
                return row.s3_key

        # Fallback: list S3 keys directly and take latest
        response = self.s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=f"models/{model_name}/",
        )
        if "Contents" not in response:
            raise FileNotFoundError(
                f"No S3 objects found for models/{model_name}/"
            )
        keys = sorted(
            [obj["Key"] for obj in response["Contents"]],
            reverse=True,
        )
        return keys[0]

    def _download(self, s3_key: str) -> Any:
        """Download and deserialize model from S3."""
        buffer = io.BytesIO()
        self.s3.download_fileobj(self.bucket, s3_key, buffer)
        buffer.seek(0)
        return joblib.load(buffer)

    def _save_local(self, model: Any, model_name: str, metadata: dict = None) -> str:
        """Save model to local disk when S3 is unavailable (dev/test environments)."""
        _LOCAL_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOCAL_SAVE_DIR / f"{model_name}_latest.pkl"
        joblib.dump({"model": model, "metadata": metadata or {}}, path)
        logger.info(f"Saved {model_name} locally → {path}")
        return str(path)

    def _register(
        self,
        model_name: str,
        version: str,
        s3_key: str,
        metadata: dict,
    ):
        """Write version to model_registry, deactivate previous."""
        self.db.execute("""
            UPDATE model_registry
            SET is_active = FALSE
            WHERE model_name = :name AND is_active = TRUE
        """, {"name": model_name})

        self.db.execute("""
            INSERT INTO model_registry
                (model_name, version, s3_key, trained_at, is_active, metadata)
            VALUES
                (:name, :ver, :key, NOW(), TRUE, :meta)
        """, {
            "name": model_name,
            "ver":  version,
            "key":  s3_key,
            "meta": json.dumps(metadata),
        })
        self.db.commit()
        logger.info(
            f"Registered {model_name} v{version} in model_registry"
        )