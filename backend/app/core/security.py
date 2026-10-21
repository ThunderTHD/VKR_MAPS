"""Хэширование паролей (scrypt из стандартной библиотеки).

Формат хранения: ``scrypt$n$r$p$соль_base64$хэш_base64`` — параметры хранятся
вместе с хэшем, поэтому их можно усилить позже, не ломая старые пароли.
"""
import base64
import hashlib
import hmac
import os

SCRYPT_N = 2**14  # стоимость по CPU/памяти (~16 МБ памяти на одну проверку)
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    # Сравнение за постоянное время — не выдаёт по времени ответа, сколько символов совпало
    return hmac.compare_digest(digest, expected)
