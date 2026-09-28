from collections import defaultdict
from threading import Lock

_lock = Lock()
_requests: dict[tuple[str, str, int], int] = defaultdict(int)
_duration_count: dict[tuple[str, str], int] = defaultdict(int)
_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
_unhandled_errors: dict[tuple[str, str], int] = defaultdict(int)
_in_progress = 0


def request_started() -> None:
    global _in_progress
    with _lock:
        _in_progress += 1


def request_finished(method: str, route: str, status_code: int, duration_seconds: float) -> None:
    global _in_progress
    key = (method, route)
    with _lock:
        _in_progress = max(0, _in_progress - 1)
        _requests[(method, route, status_code)] += 1
        _duration_count[key] += 1
        _duration_sum[key] += duration_seconds


def unhandled_error(method: str, route: str) -> None:
    with _lock:
        _unhandled_errors[(method, route)] += 1


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_metrics(gauges: dict[str, float | int]) -> str:
    with _lock:
        requests = dict(_requests)
        counts = dict(_duration_count)
        sums = dict(_duration_sum)
        errors = dict(_unhandled_errors)
        in_progress = _in_progress
    lines = [
        "# HELP ai_garden_http_requests_total Completed HTTP requests.",
        "# TYPE ai_garden_http_requests_total counter",
    ]
    for (method, route, status), value in sorted(requests.items()):
        lines.append(
            f'ai_garden_http_requests_total{{method="{_escape(method)}",route="{_escape(route)}",status="{status}"}} {value}'
        )
    lines.extend((
        "# HELP ai_garden_http_request_duration_seconds_sum Total HTTP request duration.",
        "# TYPE ai_garden_http_request_duration_seconds_sum counter",
    ))
    for (method, route), value in sorted(sums.items()):
        labels = f'method="{_escape(method)}",route="{_escape(route)}"'
        lines.append(f"ai_garden_http_request_duration_seconds_sum{{{labels}}} {value:.9f}")
        lines.append(f"ai_garden_http_request_duration_seconds_count{{{labels}}} {counts[(method, route)]}")
    lines.extend((
        "# HELP ai_garden_http_requests_in_progress Current HTTP requests in this process.",
        "# TYPE ai_garden_http_requests_in_progress gauge",
        f"ai_garden_http_requests_in_progress {in_progress}",
        "# HELP ai_garden_unhandled_errors_total Unhandled API errors.",
        "# TYPE ai_garden_unhandled_errors_total counter",
    ))
    for (method, route), value in sorted(errors.items()):
        lines.append(
            f'ai_garden_unhandled_errors_total{{method="{_escape(method)}",route="{_escape(route)}"}} {value}'
        )
    for name, value in sorted(gauges.items()):
        safe_name = "".join(character if character.isalnum() or character == "_" else "_" for character in name)
        lines.extend((f"# TYPE ai_garden_{safe_name} gauge", f"ai_garden_{safe_name} {value}"))
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    global _in_progress
    with _lock:
        _requests.clear()
        _duration_count.clear()
        _duration_sum.clear()
        _unhandled_errors.clear()
        _in_progress = 0
