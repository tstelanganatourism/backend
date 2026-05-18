"""
Core security utilities: password hashing, JWT creation/decoding, OTP generation.
All tokens include a JTI (JWT ID) for blacklisting support on logout.
"""
import uuid
import random
import string
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.timezone import get_ist_now

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


# ─── Password Utilities ──────────────────────────────────────────────────────

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# ─── OTP Utilities ───────────────────────────────────────────────────────────

def generate_otp(length: int = 6) -> str:
    """Generate a cryptographically safe numeric OTP."""
    return "".join(random.choices(string.digits, k=length))


# ─── Token Creation ──────────────────────────────────────────────────────────

def create_access_token(
    user_id: int,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> tuple[str, str]:
    """
    Create a signed JWT access token.
    Returns (token, jti) so the JTI can be stored/tracked.
    """
    jti = str(uuid.uuid4())
    expire = get_ist_now() + (
        expires_delta
        if expires_delta
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "type": "access",
        "exp": expire,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


def create_refresh_token(
    user_id: int,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> tuple[str, str]:
    """
    Create a signed JWT refresh token (longer-lived).
    Returns (token, jti).
    """
    jti = str(uuid.uuid4())
    expire = get_ist_now() + (
        expires_delta
        if expires_delta
        else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    payload = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "type": "refresh",
        "exp": expire,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, jti


# ─── Token Decoding ──────────────────────────────────────────────────────────

def decode_token(token: str, expected_type: str = "access") -> dict:
    """
    Decode and validate a JWT token.
    Raises HTTP 401 on any validation failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_exception

    token_type: str = payload.get("type", "")
    if token_type != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token type: expected '{expected_type}'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str = payload.get("sub", "")
    if not user_id:
        raise credentials_exception

    return payload


# ─── Aadhaar Secure Cryptography & Hashing (Phase 1) ──────────────────────────

import base64
import os
import hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class AadharCryptography:
    """
    Encrypts passenger Aadhaar numbers using AES-GCM-256.
    Key is derived securely using SHA-256 of the settings.SECRET_KEY.
    """
    def __init__(self):
        # Generate 32-byte key from settings.SECRET_KEY
        key_material = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
        self.cipher = AESGCM(key_material)

    def encrypt(self, plain_text: str) -> str:
        """Encrypt plain text Aadhaar and return base64 encoded string."""
        if not plain_text:
            return ""
        # 12-byte random IV
        nonce = os.urandom(12)
        encrypted_bytes = self.cipher.encrypt(nonce, plain_text.encode('utf-8'), None)
        # Prepend IV to ciphertext before encoding
        return base64.b64encode(nonce + encrypted_bytes).decode('utf-8')

    def decrypt(self, cipher_text: str) -> str:
        """Decrypt base64 encoded cipher text Aadhaar."""
        if not cipher_text:
            return ""
        data = base64.b64decode(cipher_text.encode('utf-8'))
        nonce = data[:12]
        encrypted_bytes = data[12:]
        decrypted_bytes = self.cipher.decrypt(nonce, encrypted_bytes, None)
        return decrypted_bytes.decode('utf-8')


class AadharHashing:
    """
    Generates a secure, salted SHA-256 hash of Aadhaar number to verify
    uniqueness or query duplicates in database without exposing raw numbers.
    """
    @staticmethod
    def hash_aadhar(aadhar: str) -> str:
        if not aadhar:
            return ""
        # Normalized (stripped whitespace) Aadhaar + Secret Key
        normalized = "".join(aadhar.split())
        salted = f"{normalized}:{settings.SECRET_KEY}"
        return hashlib.sha256(salted.encode('utf-8')).hexdigest()
