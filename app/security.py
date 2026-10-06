import base64, hashlib, hmac, os
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .config import settings

PBKDF2_ITERATIONS = 310_000

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ITERATIONS, base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256": return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def _serializer():
    if not settings.app_secret: raise RuntimeError("APP_SECRET belum di-set.")
    return URLSafeTimedSerializer(settings.app_secret, salt="48group-session")

def make_session_token(user_id: int) -> str:
    return _serializer().dumps({"user_id": user_id})

def read_session_token(token: str, max_age: int = 60*60*24*30):
    try:
        return int(_serializer().loads(token, max_age=max_age)["user_id"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None

def _fernet():
    if not settings.webhook_encryption_key: raise RuntimeError("WEBHOOK_ENCRYPTION_KEY belum di-set.")
    return Fernet(settings.webhook_encryption_key.encode())

def encrypt_webhook(url: str) -> str:
    return _fernet().encrypt(url.encode()).decode()

def decrypt_webhook(token: str) -> str:
    try: return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc: raise RuntimeError("Webhook tidak bisa didekripsi; periksa WEBHOOK_ENCRYPTION_KEY.") from exc

def mask_webhook(url: str) -> str:
    return url[:30] + "••••••••" + url[-6:] if len(url) > 36 else url[:8] + "••••"
