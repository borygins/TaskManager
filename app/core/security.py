from __future__ import annotations

import hashlib
import hmac
import secrets


EMPLOYEE_ID_LENGTH = 8


def normalize_employee_id(employee_id: str) -> str:
    normalized = employee_id.strip()
    if not normalized:
        raise ValueError("Табельный номер обязателен.")
    if not normalized.isdigit():
        raise ValueError("Табельный номер должен состоять только из цифр.")
    if len(normalized) > EMPLOYEE_ID_LENGTH:
        raise ValueError("Табельный номер не может быть больше 8 цифр.")
    return normalized.zfill(EMPLOYEE_ID_LENGTH)


def hash_password(password: str, salt: str | None = None) -> str:
    current_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        current_salt.encode("utf-8"),
        120_000,
    )
    return f"{current_salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected = stored_hash.split("$", 1)
    except ValueError:
        return False
    actual = hash_password(password=password, salt=salt).split("$", 1)[1]
    return hmac.compare_digest(actual, expected)
