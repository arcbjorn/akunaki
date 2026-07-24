"""Process-wide metrics registry and Prometheus text exposition.

Counters and gauges only — no dependency, no client library. The exposition
format is the Prometheus text format, which is also what an OpenTelemetry
collector scrapes, so this satisfies the "OTel-compatible metrics" intent in
operations.md without pulling an SDK into a core install that a CI job proves
boots with a minimal dependency set.

**Why this exists.** Logs say what happened at one moment; they cannot answer
"is this worker still doing periodic work?". A schedule that silently stops
firing, a queue that only ever dead-letters, a connector rejecting every fetch
— each is invisible in a log stream nobody is tailing, and each is one counter
away from being obvious.

**PHI-free by construction.** Metric values are counts, never measurements, and
label values are bounded, low-cardinality tokens (provider, outcome class).
Never label a metric with a tenant id, a user id, or anything derived from
health data: label sets become permanent series in the scraper.

Lives in ``application`` rather than ``domain`` because it is process state, not
a deterministic rule — the domain stays pure and computes the same output for
the same input, with no counters to mutate.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# Label values are interned into series keys, so they must be bounded tokens.
# This guards the "never label with a tenant id" rule at the point of use.
_MAX_LABEL_VALUE_LENGTH = 64


class MetricError(ValueError):
    """Invalid metric definition or usage."""


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _render_labels(label_names: Sequence[str], label_values: tuple[str, ...]) -> str:
    """Render a ``{k="v",…}`` clause, or empty string when unlabelled."""
    if not label_names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label_value(value)}"'
        for name, value in zip(label_names, label_values, strict=True)
    )
    return "{" + pairs + "}"


def _format_value(value: float) -> str:
    """Render a sample value, keeping whole numbers integral."""
    if value.is_integer():
        return str(int(value))
    return repr(value)


@dataclass(slots=True)
class _Series:
    """One metric family: a name, help text, type, and its labelled samples."""

    name: str
    help_text: str
    metric_type: str
    label_names: tuple[str, ...]
    samples: dict[tuple[str, ...], float] = field(default_factory=dict)


class _Metric:
    """Shared behaviour for the metric handles handed to call sites."""

    def __init__(self, series: _Series, lock: threading.Lock) -> None:
        self._series = series
        self._lock = lock

    def _key(self, labels: Mapping[str, str] | None) -> tuple[str, ...]:
        """Validate and order label values into a stable series key."""
        names = self._series.label_names
        supplied = labels or {}
        if set(supplied) != set(names):
            msg = (
                f"metric {self._series.name!r} expects labels {sorted(names)}, "
                f"got {sorted(supplied)}"
            )
            raise MetricError(msg)
        values: list[str] = []
        for name in names:
            value = supplied[name]
            if len(value) > _MAX_LABEL_VALUE_LENGTH:
                msg = (
                    f"label {name!r} value exceeds {_MAX_LABEL_VALUE_LENGTH} chars; "
                    "labels must be bounded, low-cardinality tokens"
                )
                raise MetricError(msg)
            values.append(value)
        return tuple(values)

    def value(self, **labels: str) -> float:
        """Current value of one series (0.0 when never touched)."""
        key = self._key(labels)
        with self._lock:
            return self._series.samples.get(key, 0.0)


class Counter(_Metric):
    """A monotonically increasing count."""

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Add ``amount`` (must be non-negative) to a series."""
        if amount < 0:
            msg = "counters increase only; amount must be non-negative"
            raise MetricError(msg)
        key = self._key(labels)
        with self._lock:
            self._series.samples[key] = self._series.samples.get(key, 0.0) + amount


class Gauge(_Metric):
    """A value that can go up or down."""

    def set(self, value: float, **labels: str) -> None:
        """Set a series to an absolute value."""
        key = self._key(labels)
        with self._lock:
            self._series.samples[key] = value


class MetricsRegistry:
    """Thread-safe registry of metric families.

    A single lock guards every series. Mutations are dictionary updates under
    that lock, so the worker's heartbeat thread and the API's request threads
    can record concurrently without a per-metric locking protocol.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[str, _Series] = {}

    def _register(
        self,
        *,
        name: str,
        help_text: str,
        metric_type: str,
        label_names: Sequence[str],
    ) -> _Series:
        if not name:
            msg = "metric name must be non-empty"
            raise MetricError(msg)
        with self._lock:
            existing = self._series.get(name)
            if existing is not None:
                # Re-registration is normal (module reimport, test reuse), but
                # a changed shape means two call sites disagree about the
                # metric — which would silently corrupt the exposition.
                if existing.metric_type != metric_type or existing.label_names != tuple(
                    label_names
                ):
                    msg = f"metric {name!r} already registered with a different shape"
                    raise MetricError(msg)
                return existing
            series = _Series(
                name=name,
                help_text=help_text,
                metric_type=metric_type,
                label_names=tuple(label_names),
            )
            self._series[name] = series
            return series

    def counter(self, name: str, help_text: str, *, labels: Sequence[str] = ()) -> Counter:
        """Register (or fetch) a counter."""
        return Counter(
            self._register(
                name=name, help_text=help_text, metric_type="counter", label_names=labels
            ),
            self._lock,
        )

    def gauge(self, name: str, help_text: str, *, labels: Sequence[str] = ()) -> Gauge:
        """Register (or fetch) a gauge."""
        return Gauge(
            self._register(name=name, help_text=help_text, metric_type="gauge", label_names=labels),
            self._lock,
        )

    def render(self) -> str:
        """Render every family in the Prometheus text exposition format.

        A family with no recorded samples still emits its HELP/TYPE header, so
        a scraper sees a declared-but-idle metric rather than nothing at all.
        Unlabelled families emit an explicit zero for the same reason: "no
        failures yet" and "this build cannot fail" must not look alike.
        """
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._series):
                series = self._series[name]
                lines.append(f"# HELP {name} {series.help_text}")
                lines.append(f"# TYPE {name} {series.metric_type}")
                if not series.samples and not series.label_names:
                    lines.append(f"{name} 0")
                    continue
                for key in sorted(series.samples):
                    labels = _render_labels(series.label_names, key)
                    lines.append(f"{name}{labels} {_format_value(series.samples[key])}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop all recorded samples, keeping registrations (tests only)."""
        with self._lock:
            for series in self._series.values():
                series.samples.clear()


# The process-wide registry. Metric handles below are module-level so a call
# site records with one attribute access and no plumbing through constructors.
REGISTRY = MetricsRegistry()

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# --- Jobs -----------------------------------------------------------------

JOBS_SETTLED = REGISTRY.counter(
    "akunaki_jobs_settled_total",
    "Durable jobs reaching a terminal outcome, by disposition.",
    labels=("disposition",),
)
JOBS_DEAD_LETTERED = REGISTRY.counter(
    "akunaki_jobs_dead_letters_total",
    "Jobs dead-lettered, including those reaped after lease expiry.",
)
JOBS_LEASE_LOST = REGISTRY.counter(
    "akunaki_jobs_lease_lost_total",
    "Job attempts whose lease was lost mid-execution (no false success).",
)

# Split by result so a schedule that is genuinely firing and one that is
# silently deduped every tick are distinguishable. Collapsed into a single
# count, a stalled schedule is indistinguishable from a healthy one.
JOBS_SCHEDULED = REGISTRY.counter(
    "akunaki_jobs_scheduled_total",
    "Periodic schedule fires by the leader, by enqueue result.",
    labels=("job_type", "result"),
)

# --- Connectors and webhooks ---------------------------------------------

CONNECTOR_FETCH = REGISTRY.counter(
    "akunaki_connector_fetch_total",
    "Provider fetch attempts by outcome class.",
    labels=("provider", "result"),
)

# --- Worker liveness ------------------------------------------------------

WORKER_LIVENESS = REGISTRY.gauge(
    "akunaki_worker_liveness",
    "1 while a worker loop is running in this process, 0 once it has stopped.",
)
WORKER_LEADER = REGISTRY.gauge(
    "akunaki_worker_leader",
    "1 while this process holds the reaper/scheduler leader lease, else 0.",
)

__all__ = [
    "CONNECTOR_FETCH",
    "CONTENT_TYPE",
    "JOBS_DEAD_LETTERED",
    "JOBS_LEASE_LOST",
    "JOBS_SCHEDULED",
    "JOBS_SETTLED",
    "REGISTRY",
    "WORKER_LEADER",
    "WORKER_LIVENESS",
    "Counter",
    "Gauge",
    "MetricError",
    "MetricsRegistry",
]
