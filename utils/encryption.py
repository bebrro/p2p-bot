"""
AES-128 шифрование для хранения API ключей.
ENCRYPTION_KEY задаётся в .env один раз и никогда не меняется.
Если не задан — генерируется и выводится в консоль.
"""
import os
from cryptography.fernet import Fernet

_cipher: Fernet | None = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is not None:
        return _cipher

    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        import sys
        msg = (
            "\n[WARNING] ENCRYPTION_KEY not set in .env!\n"
            f"Add this line to your .env file:\n\nENCRYPTION_KEY={key}\n\n"
            "Without this key, all stored API keys will be lost on bot restart.\n"
        )
        sys.stdout.buffer.write(msg.encode("utf-8"))
        sys.stdout.buffer.flush()
    _cipher = Fernet(key.encode() if isinstance(key, str) else key)
    return _cipher


def encrypt(plain: str) -> str:
    """Шифрует строку (API ключ). Возвращает base64-токен."""
    return _get_cipher().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    """Расшифровывает токен обратно в строку."""
    return _get_cipher().decrypt(token.encode()).decode()
