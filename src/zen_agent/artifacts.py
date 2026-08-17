from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import ArtifactRecord


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if self.root == Path("/"):
            raise ValueError("artifact root cannot be filesystem root")
        self._private_dir(self.root)
        self.blobs = self.root / "blobs"
        self._private_dir(self.blobs)

    @staticmethod
    def _private_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)

    def put_bytes(self, payload: bytes, media_type: str) -> ArtifactRecord:
        digest = sha256(payload).hexdigest()
        directory = self.blobs / digest[:2]
        self._private_dir(directory)
        target = directory / digest
        if target.exists():
            observed = target.read_bytes()
            if observed != payload:
                raise RuntimeError("SHA-256 collision or artifact corruption")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".blob-", dir=directory)
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return ArtifactRecord(
            sha256=digest,
            relative_path=str(target.relative_to(self.root)),
            bytes=len(payload),
            media_type=media_type,
        )

    def put_json(self, value: Any) -> ArtifactRecord:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ).encode("utf-8")
        return self.put_bytes(payload, "application/json")

    def verify(self, record: ArtifactRecord) -> bool:
        target = (self.root / record.relative_path).resolve()
        if self.root not in target.parents or not target.is_file():
            return False
        payload = target.read_bytes()
        return len(payload) == record.bytes and sha256(payload).hexdigest() == record.sha256
