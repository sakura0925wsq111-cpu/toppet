"""Monotonic latency instrumentation shared by the host protocol stages."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

logger = logging.getLogger("toppet.latency")


@dataclass(frozen=True)
class LatencyRecord:
    node: str
    session_id: str
    device_id: str
    monotonic_ms: float
    duration_ms: float | None = None
    sequence: int | None = None
    buffer_bytes: int | None = None


class LatencyTracker:
    def __init__(self, max_records: int = 10_000) -> None:
        self._records: deque[LatencyRecord] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def record(
        self,
        node: str,
        *,
        session_id: str,
        device_id: str,
        started_at: float | None = None,
        duration_ms: float | None = None,
        sequence: int | None = None,
        buffer_bytes: int | None = None,
    ) -> LatencyRecord:
        now = time.monotonic()
        if duration_ms is None and started_at is not None:
            duration_ms = (now - started_at) * 1000.0
        record = LatencyRecord(
            node=node,
            session_id=session_id,
            device_id=device_id,
            monotonic_ms=round(now * 1000.0, 3),
            duration_ms=None if duration_ms is None else round(duration_ms, 3),
            sequence=sequence,
            buffer_bytes=buffer_bytes,
        )
        with self._lock:
            self._records.append(record)
        logger.info("latency_event %s", json.dumps(asdict(record), ensure_ascii=False))
        return record

    def records(self) -> list[dict[str, object]]:
        with self._lock:
            return [asdict(record) for record in self._records]

    def summary(self) -> dict[str, object]:
        grouped: dict[str, list[float]] = defaultdict(list)
        with self._lock:
            snapshot = list(self._records)
        for record in snapshot:
            if record.duration_ms is not None:
                grouped[record.node].append(record.duration_ms)
        return {
            "record_count": len(snapshot),
            "events": {
                node: {
                    "count": len(values),
                    "p50_ms": _percentile(values, 0.50),
                    "p95_ms": _percentile(values, 0.95),
                }
                for node, values in sorted(grouped.items())
            },
        }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 3)
