from collections import defaultdict, deque
from functools import lru_cache
import hashlib
import hmac
from threading import Lock
import time

from fastapi import HTTPException

from app.config import settings


_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()

# INCR and EXPIRE must run as one atomic step: a crash between the two calls
# would leave the counter without a TTL and block the identifier forever.
_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def _key(scope: str, identifier: str) -> str:
    digest = hmac.new(settings.ai_safety_secret.encode(), identifier.casefold().encode(), hashlib.sha256).hexdigest()
    return f"rate:{scope}:{digest}"


@lru_cache(maxsize=1)
def _redis_client():
    """Reuse one pooled client instead of reconnecting on every request."""
    from redis import Redis

    return Redis.from_url(
        settings.celery_broker_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        socket_keepalive=True,
    )


@lru_cache(maxsize=1)
def _rate_limit_script():
    return _redis_client().register_script(_RATE_LIMIT_SCRIPT)


def reset_redis_clients() -> None:
    """Drop cached clients so configuration changes and tests start clean."""
    _redis_client.cache_clear()
    _rate_limit_script.cache_clear()


def enforce_rate_limit(
    scope: str,
    identifier: str,
    *,
    limit: int,
    window: int,
    unavailable_detail: str = "Защита от злоупотреблений временно недоступна",
) -> None:
    key = _key(scope, identifier)
    if settings.diagnosis_execution_mode == "celery":
        try:
            count = int(_rate_limit_script()(keys=[key], args=[window]))
            if count > limit:
                raise HTTPException(429, "Слишком много попыток. Повторите позже")
            return
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, unavailable_detail) from exc
    now = time.monotonic()
    with _lock:
        values = _attempts[key]
        while values and values[0] <= now - window:
            values.popleft()
        if len(values) >= limit:
            raise HTTPException(429, "Слишком много попыток. Повторите позже")
        values.append(now)


def enforce_auth_rate_limit(scope: str, identifier: str) -> None:
    enforce_rate_limit(
        scope,
        identifier,
        limit=settings.auth_rate_limit_attempts,
        window=settings.auth_rate_limit_window_seconds,
        unavailable_detail="Защита авторизации временно недоступна",
    )


def reset_local_rate_limits() -> None:
    """Test/development helper; production limits live in Redis."""
    with _lock:
        _attempts.clear()
    reset_redis_clients()
