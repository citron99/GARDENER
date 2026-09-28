import base64
import hashlib
import hmac
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import settings


class AttributionTokenError(ValueError):
    pass


def lead_idempotency_key(user_id: int, product_id: int, diagnosis_id: int | None) -> str:
    context = str(diagnosis_id) if diagnosis_id is not None else "catalog"
    return hashlib.sha256(f"{user_id}:{product_id}:{context}".encode()).hexdigest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_click_id(click_id: str, *, now: int | None = None) -> str:
    expires = (now or int(time.time())) + settings.partner_click_ttl_days * 86400
    payload = f"{click_id}.{expires}".encode()
    signature = hmac.new(
        settings.partner_attribution_secret.encode(), payload, hashlib.sha256,
    ).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_click_token(token: str, *, now: int | None = None) -> str:
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = _decode(payload_part)
        signature = _decode(signature_part)
        click_id, expires_text = payload.decode().rsplit(".", 1)
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AttributionTokenError("Некорректный click ID") from exc
    expected = hmac.new(
        settings.partner_attribution_secret.encode(), payload, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise AttributionTokenError("Подпись click ID не подтверждена")
    if expires < (now or int(time.time())):
        raise AttributionTokenError("Срок действия click ID истёк")
    if len(click_id) != 36:
        raise AttributionTokenError("Некорректный click ID")
    return click_id


def add_click_id(url: str, signed_click_id: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("ai_garden_click_id", signed_click_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def hash_partner_key(value: str) -> str:
    """Hash a partner postback key with a server-side secret.

    A plain digest would let anyone with a copy of the table verify guessed
    keys offline, so the value is keyed with PARTNER_ATTRIBUTION_SECRET.
    """
    return hmac.new(
        settings.partner_attribution_secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def legacy_partner_key_hash(value: str) -> str:
    """Pre-HMAC scheme, kept only to recognise keys stored before the upgrade."""
    return hashlib.sha256(value.encode()).hexdigest()


def verify_partner_key(value: str, stored_hash: str | None) -> tuple[bool, bool]:
    """Return (matches, needs_rehash) for a supplied postback key."""
    if not stored_hash:
        return False, False
    if hmac.compare_digest(hash_partner_key(value), stored_hash):
        return True, False
    if hmac.compare_digest(legacy_partner_key_hash(value), stored_hash):
        return True, True
    return False, False
