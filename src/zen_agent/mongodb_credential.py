from __future__ import annotations

from dataclasses import dataclass
import getpass
import os
from typing import Callable


_MISSING = object()


@dataclass(slots=True)
class EphemeralMongoCredential:
    uri: str
    previous: str | object = _MISSING
    injected: bool = False

    @classmethod
    def prompt(
        cls,
        reader: Callable[[str], str] = getpass.getpass,
    ) -> "EphemeralMongoCredential":
        uri = reader("MongoDB URI (hidden; process memory only): ").strip()
        if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
            raise ValueError("MongoDB URI must start with mongodb:// or mongodb+srv://")
        if len(uri) > 8192:
            raise ValueError("MongoDB URI exceeds safe length")
        return cls(uri)

    def inject(self) -> None:
        if self.injected:
            raise RuntimeError("MongoDB credential is already injected")
        self.previous = os.environ.get("MONGODB_URI", _MISSING)
        os.environ["MONGODB_URI"] = self.uri
        self.injected = True

    def restore(self) -> None:
        if not self.injected:
            return
        if self.previous is _MISSING:
            os.environ.pop("MONGODB_URI", None)
        else:
            os.environ["MONGODB_URI"] = str(self.previous)
        self.uri = ""
        self.injected = False
