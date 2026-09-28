from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timezone
from threading import Lock
from time import monotonic

import httpx

from app.config import settings
from app.schemas import WeatherDailyRead, WeatherForecastRead, WeatherWarningRead


class WeatherServiceError(RuntimeError):
    pass


class WeatherService:
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    DAILY_FIELDS = (
        "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
        "precipitation_probability_max,snowfall_sum,wind_speed_10m_max,"
        "wind_gusts_10m_max,et0_fao_evapotranspiration"
    )

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=settings.weather_timeout_seconds)
        # Bounded LRU: location strings are user-controlled, so an unbounded
        # cache would grow for the whole lifetime of the process.
        self._cache: OrderedDict[str, tuple[float, WeatherForecastRead]] = OrderedDict()
        self._lock = Lock()

    def get_forecast(self, location: str, language: str = "ru") -> WeatherForecastRead:
        location = location.strip()
        if not 2 <= len(location) <= 160:
            raise WeatherServiceError("Укажите корректный населённый пункт")
        language = language if language in {"ru", "lv", "en"} else "en"
        key = f"{language}:{location.casefold()}"
        now = monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self._cache.move_to_end(key)
                return cached[1].model_copy(deep=True)
            if cached:
                del self._cache[key]
            self._expire_locked(now)

        try:
            place = self._geocode(location, language)
            forecast = self._forecast(place)
        except WeatherServiceError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise WeatherServiceError("Погодный сервис временно недоступен") from exc

        with self._lock:
            self._expire_locked(monotonic())
            self._cache[key] = (monotonic() + settings.weather_cache_seconds, forecast)
            self._cache.move_to_end(key)
            while len(self._cache) > settings.weather_cache_entries:
                self._cache.popitem(last=False)
        return forecast.model_copy(deep=True)

    def _expire_locked(self, now: float) -> None:
        """Drop expired entries; the lock must already be held."""
        expired = [key for key, (expires_at, _) in self._cache.items() if expires_at <= now]
        for key in expired:
            del self._cache[key]

    def _geocode(self, location: str, language: str) -> dict:
        response = self._client.get(
            self.GEOCODING_URL,
            params={"name": location, "count": 1, "language": language, "format": "json"},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            raise WeatherServiceError("Населённый пункт не найден")
        place = results[0]
        if "latitude" not in place or "longitude" not in place:
            raise WeatherServiceError("Погодный сервис вернул неполные координаты")
        return place

    def _forecast(self, place: dict) -> WeatherForecastRead:
        response = self._client.get(
            self.FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": self.DAILY_FIELDS,
                "timezone": "auto",
                "forecast_days": settings.weather_forecast_days,
            },
        )
        response.raise_for_status()
        payload = response.json()
        daily_payload = payload.get("daily") or {}
        times = daily_payload.get("time") or []
        if not times:
            raise WeatherServiceError("Погодный сервис не вернул прогноз")

        def value(field: str, index: int, default=None):
            values = daily_payload.get(field) or []
            return values[index] if index < len(values) and values[index] is not None else default

        daily: list[WeatherDailyRead] = []
        for index, raw_date in enumerate(times):
            minimum = value("temperature_2m_min", index)
            maximum = value("temperature_2m_max", index)
            if minimum is None or maximum is None:
                raise WeatherServiceError("Погодный сервис вернул неполные температуры")
            daily.append(WeatherDailyRead(
                date=date.fromisoformat(raw_date),
                weather_code=value("weather_code", index),
                temperature_min_c=minimum,
                temperature_max_c=maximum,
                precipitation_mm=value("precipitation_sum", index, 0),
                precipitation_probability_percent=value("precipitation_probability_max", index),
                snowfall_cm=value("snowfall_sum", index, 0),
                wind_speed_max_kmh=value("wind_speed_10m_max", index, 0),
                wind_gusts_max_kmh=value("wind_gusts_10m_max", index),
                et0_mm=value("et0_fao_evapotranspiration", index),
            ))

        warnings = [warning for day in daily for warning in self._warnings_for_day(day)]
        return WeatherForecastRead(
            location=place.get("name") or "",
            country_code=place.get("country_code"),
            latitude=place["latitude"],
            longitude=place["longitude"],
            timezone=payload.get("timezone") or place.get("timezone") or "UTC",
            fetched_at=datetime.now(timezone.utc),
            daily=daily,
            warnings=warnings,
        )

    @staticmethod
    def _warnings_for_day(day: WeatherDailyRead) -> list[WeatherWarningRead]:
        warnings: list[WeatherWarningRead] = []
        if day.temperature_min_c <= settings.weather_frost_threshold_c:
            critical = day.temperature_min_c <= 0
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="frost",
                severity="critical" if critical else "warning",
                title="Заморозок" if critical else "Риск заморозка",
                advice="Защитите чувствительные растения и проверьте укрытия вечером.",
            ))
        if day.temperature_max_c >= settings.weather_heat_threshold_c:
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="heat",
                severity="critical" if day.temperature_max_c >= 35 else "warning",
                title="Сильная жара",
                advice="Проверьте влажность почвы утром; не поливайте автоматически без проверки.",
            ))
        if day.precipitation_mm >= settings.weather_heavy_rain_threshold_mm:
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="heavy_rain",
                severity="critical" if day.precipitation_mm >= 40 else "warning",
                title="Сильные осадки",
                advice="Проверьте дренаж и отложите полив и обработки до уточнения условий.",
            ))
        strongest_wind = max(day.wind_speed_max_kmh, day.wind_gusts_max_kmh or 0)
        if strongest_wind >= settings.weather_wind_threshold_kmh:
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="strong_wind",
                severity="critical" if strongest_wind >= 75 else "warning",
                title="Сильный ветер",
                advice="Закрепите опоры, контейнеры и лёгкие укрытия.",
            ))
        if day.snowfall_cm >= 1:
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="snow",
                severity="warning",
                title="Снег",
                advice="Проверьте укрытия и осторожно снимайте тяжёлый мокрый снег с ветвей.",
            ))
        if day.precipitation_mm <= 0.5 and (day.et0_mm or 0) >= 4 and day.temperature_max_c >= 20:
            warnings.append(WeatherWarningRead(
                date=day.date,
                kind="irrigation_check",
                severity="info",
                title="Проверить потребность в поливе",
                advice="Проверьте влажность почвы у корней; прогноз сам по себе не означает необходимость полива.",
            ))
        return warnings


weather_service = WeatherService()