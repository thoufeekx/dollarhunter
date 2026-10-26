import os
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key, data):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def get(self, key):
        return self._p(key).read_bytes()

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)


class R2Storage:
    def __init__(self, s):
        import boto3
        self.bucket = s.r2_bucket
        self.c = boto3.client("s3", endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
                              aws_access_key_id=s.r2_access_key_id, aws_secret_access_key=s.r2_secret_access_key,
                              region_name="auto")

    def put(self, key, data):
        self.c.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key):
        return self.c.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.c.delete_object(Bucket=self.bucket, Key=key)


def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend == "r2":
        if not (s.r2_account_id and s.r2_access_key_id and s.r2_secret_access_key):
            raise RuntimeError("DH_STORAGE_BACKEND=r2 but R2 credentials are missing")
        return R2Storage(s)
    return LocalStorage(s.storage_dir)
