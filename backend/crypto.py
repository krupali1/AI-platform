"""
Encrypts and decrypts values (Anthropic API keys) at rest, so the
database never holds a plaintext key. The encryption key itself lives
only in the server's environment (SECRET_KEY) - never in the database,
never sent to the browser.

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os
from cryptography.fernet import Fernet, InvalidToken

_raw_key = os.getenv("SECRET_KEY")
_fernet = Fernet(_raw_key.encode()) if _raw_key else None


def encrypt(value: str) -> str:
    if not _fernet:
        raise RuntimeError(
            "SECRET_KEY is not set - cannot store secrets. "
            "Generate one and add it to .env (see crypto.py docstring)."
        )
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str):
    if not _fernet or not token:
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        return None
