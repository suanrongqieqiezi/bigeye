"""
AES-256-GCM encryption for API communication.
Generates a random key at startup, displayed to the user.
"""
import os, base64, json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY = None  # 32-byte raw key, set by init()


def init(key_b64=None):
    global _KEY
    if key_b64:
        _KEY = base64.b64decode(key_b64)
        if len(_KEY) != 32:
            raise ValueError("key must be 32 bytes (44 base64 chars)")
    else:
        _KEY = AESGCM.generate_key(bit_length=256)
    return _KEY


def set_key_b64(b64_key):
    """Replace the crypto key at runtime from a base64 string."""
    global _KEY
    if b64_key:
        _KEY = base64.b64decode(b64_key)
        if len(_KEY) != 32:
            raise ValueError("key must be 32 bytes (44 base64 chars)")
    else:
        _KEY = None


def get_key_b64():
    """Return the key as a base64 string for display."""
    return base64.b64encode(_KEY).decode("ascii") if _KEY else ""


def encrypt(plaintext: str) -> str:
    """Encrypt a string, return base64-encoded ciphertext (nonce + data)."""
    if not _KEY:
        return plaintext
    aesgcm = AESGCM(_KEY)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(ciphertext_b64: str) -> str:
    """Decrypt a base64-encoded ciphertext, return plaintext string."""
    if not _KEY:
        return ciphertext_b64
    aesgcm = AESGCM(_KEY)
    raw = base64.b64decode(ciphertext_b64)
    nonce, ct = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
