"""Server-side encrypted secret store for provider API keys.

Design notes
------------
* Raw API keys are NEVER written to logs, responses, the database (except for a
  non-reversible masked preview), or artifact files.
* Keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key derived
  from ``APP_MASTER_KEY`` via PBKDF2-HMAC-SHA256.
* The KDF salt is a **per-installation** random 32-byte value generated on
  first boot and persisted at ``var/secrets/.salt``. This means two different
  ResearchOS installations that happen to share the same ``APP_MASTER_KEY``
  still produce different encryption keys. The salt file is treated as
  configuration, not a secret, but is still created with restrictive
  permissions where the OS allows.
* Each ciphertext is persisted to a file under ``var/secrets/<ref>.enc`` with
  mode 0o600 where possible. Metadata (provider + masked preview) lives next
  to it in ``<ref>.meta.json``.
* This module intentionally has no HTTP or route concerns — all that lives in
  ``services/provider_secret_service.py`` and ``api/routes/providers.py``.

Migration note
--------------
Earlier builds of this MVP used a fixed hard-coded salt. If you are upgrading
an existing local install and previously stored credentials, the new derived
key will NOT decrypt those old ciphertexts. The remediation is documented in
``docs/local-run.md``:

  1. delete everything under ``var/secrets/``
  2. delete ``var/data/researchos.db`` (or just the rows of
     ``provider_credentials``)
  3. re-add credentials through the Settings modal

Tooling note: the abstraction is designed so we can later swap the
filesystem backend for a real secret manager (Vault / AWS / GCP) by
implementing the same interface.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import get_settings
from app.utils import get_logger, mask_secret, new_id

logger = get_logger(__name__)

_SALT_FILE_NAME = ".salt"
_SALT_LEGACY_FALLBACK = b"researchos-local-secret-store-v1"
_SALT_BYTES = 32
_KDF_ITERATIONS = 240_000


def _load_or_create_salt(secrets_root: Path) -> bytes:
    """Return the per-install salt, creating it on first boot.

    The file is plain bytes (no encoding), created with 0o600 where the OS
    supports it. Callers must guarantee ``secrets_root`` exists.
    """
    secrets_root.mkdir(parents=True, exist_ok=True)
    path = secrets_root / _SALT_FILE_NAME
    if path.exists():
        data = path.read_bytes()
        if len(data) >= 16:
            return data
        logger.warning(
            "salt file too short, regenerating",
            extra={"path": str(path), "len": len(data)},
        )
    data = secrets.token_bytes(_SALT_BYTES)
    path.write_bytes(data)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows / non-POSIX: ignore silently.
        pass
    logger.info("generated new per-install KDF salt", extra={"path": str(path)})
    return data


def _derive_key(master: str, salt: bytes | None = None) -> bytes:
    if not master or len(master) < 16:
        raise RuntimeError(
            "APP_MASTER_KEY must be set to at least 16 characters. "
            "Generate a long random string and put it in .env."
        )
    if salt is None:
        # Only used by the unit test that exercises the weak-master-key branch
        # before the settings cache is warmed up. Real callers always pass the
        # persisted salt through ``get_secret_store``.
        salt = _SALT_LEGACY_FALLBACK
    if len(salt) < 16:
        raise RuntimeError("KDF salt must be at least 16 bytes")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master.encode("utf-8")))


@dataclass
class SecretMeta:
    ref: str
    provider: str
    masked_preview: str


class SecretBackend(Protocol):
    def write(self, ref: str, plaintext: str, *, provider: str, masked_preview: str) -> None: ...
    def read(self, ref: str) -> str: ...
    def delete(self, ref: str) -> None: ...
    def exists(self, ref: str) -> bool: ...


class FilesystemSecretBackend:
    def __init__(self, root: Path, fernet: Fernet) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._fernet = fernet
        self._lock = Lock()

    def _paths(self, ref: str) -> tuple[Path, Path]:
        safe = ref.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.enc", self.root / f"{safe}.meta.json"

    @staticmethod
    def _chmod_private(p: Path) -> None:
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows / non-POSIX: ignore silently.
            pass

    def write(self, ref: str, plaintext: str, *, provider: str, masked_preview: str) -> None:
        with self._lock:
            enc_path, meta_path = self._paths(ref)
            token = self._fernet.encrypt(plaintext.encode("utf-8"))
            enc_path.write_bytes(token)
            self._chmod_private(enc_path)
            meta_path.write_text(
                json.dumps(
                    {"ref": ref, "provider": provider, "masked_preview": masked_preview},
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._chmod_private(meta_path)

    def read(self, ref: str) -> str:
        enc_path, _ = self._paths(ref)
        if not enc_path.exists():
            raise KeyError(f"secret ref not found: {ref}")
        try:
            return self._fernet.decrypt(enc_path.read_bytes()).decode("utf-8")
        except InvalidToken as e:  # pragma: no cover - config error
            raise RuntimeError(
                "Secret store cannot decrypt stored credential. "
                "APP_MASTER_KEY may have changed since encryption."
            ) from e

    def delete(self, ref: str) -> None:
        with self._lock:
            enc_path, meta_path = self._paths(ref)
            for p in (enc_path, meta_path):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        logger.warning("failed to remove secret file", extra={"ref": ref})

    def exists(self, ref: str) -> bool:
        enc_path, _ = self._paths(ref)
        return enc_path.exists()


class SecretStore:
    """High-level API used by services."""

    def __init__(self, backend: SecretBackend) -> None:
        self._backend = backend

    def put(self, *, provider: str, api_key: str) -> SecretMeta:
        ref = new_id("sec")
        masked = mask_secret(api_key)
        self._backend.write(ref, api_key, provider=provider, masked_preview=masked)
        # Important: never log the api key itself.
        logger.info(
            "stored provider credential",
            extra={"provider": provider, "ref": ref, "masked_preview": masked},
        )
        return SecretMeta(ref=ref, provider=provider, masked_preview=masked)

    def rotate(self, ref: str, *, provider: str, api_key: str) -> SecretMeta:
        masked = mask_secret(api_key)
        self._backend.write(ref, api_key, provider=provider, masked_preview=masked)
        logger.info(
            "rotated provider credential",
            extra={"provider": provider, "ref": ref, "masked_preview": masked},
        )
        return SecretMeta(ref=ref, provider=provider, masked_preview=masked)

    def get(self, ref: str) -> str:
        """Return the raw plaintext key. Caller must handle responsibly."""
        return self._backend.read(ref)

    def delete(self, ref: str) -> None:
        self._backend.delete(ref)
        logger.info("deleted provider credential", extra={"ref": ref})

    def exists(self, ref: str) -> bool:
        return self._backend.exists(ref)


_instance: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _instance
    if _instance is None:
        settings = get_settings()
        root = settings.resolve_path(settings.secrets_dir)
        salt = _load_or_create_salt(root)
        fernet = Fernet(_derive_key(settings.app_master_key, salt))
        _instance = SecretStore(FilesystemSecretBackend(root, fernet))
    return _instance


def reset_secret_store_cache() -> None:
    """Only for tests."""
    global _instance
    _instance = None
