from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised when a requested workspace operation is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """Resolve paths without allowing traversal or symlink escapes."""

    root: Path

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceError(f"workspace is not a directory: {root}")
        object.__setattr__(self, "root", root)

    def resolve(self, relative: str | Path = ".", *, must_exist: bool = False) -> Path:
        requested = Path(relative)
        if requested.is_absolute():
            raise WorkspaceError("workspace paths must be relative")
        candidate = self.root / requested
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (FileNotFoundError, RuntimeError) as exc:
            raise WorkspaceError(f"invalid workspace path: {relative}") from exc
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes workspace: {relative}")
        return resolved

    def file(self, relative: str | Path, *, must_exist: bool = True) -> Path:
        path = self.resolve(relative, must_exist=must_exist)
        if must_exist and not path.is_file():
            raise WorkspaceError(f"not a file: {relative}")
        return path

    def directory(self, relative: str | Path = ".") -> Path:
        path = self.resolve(relative, must_exist=True)
        if not path.is_dir():
            raise WorkspaceError(f"not a directory: {relative}")
        return path

    def relative(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.root).as_posix()

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def read_bytes(self, relative: str | Path, *, max_bytes: int) -> tuple[bytes, str]:
        path = self.file(relative)
        size = path.stat().st_size
        if size > max_bytes:
            raise WorkspaceError(f"file exceeds {max_bytes} byte limit")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise WorkspaceError(f"file exceeds {max_bytes} byte limit")
        return data, self.sha256(data)

    def atomic_write(
        self,
        relative: str | Path,
        data: bytes,
        *,
        expected_sha256: str | None,
        max_bytes: int,
    ) -> str:
        if len(data) > max_bytes:
            raise WorkspaceError(f"content exceeds {max_bytes} byte limit")
        target = self.resolve(relative, must_exist=False)
        parent = target.parent.resolve(strict=True)
        if not parent.is_relative_to(self.root):
            raise WorkspaceError(f"parent escapes workspace: {relative}")
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise WorkspaceError(f"refusing to overwrite non-regular file: {relative}")
            current = target.read_bytes()
            current_sha = self.sha256(current)
            if expected_sha256 is None:
                raise WorkspaceError("expected_sha256 is required when overwriting a file")
            if current_sha != expected_sha256:
                raise WorkspaceError("file changed since it was read")
            mode = target.stat().st_mode & 0o777
        else:
            if expected_sha256 is not None:
                raise WorkspaceError("expected_sha256 must be null when creating a file")
            mode = 0o644

        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, target)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self.sha256(data)
