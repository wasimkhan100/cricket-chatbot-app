"""OpenTelemetry tracing.

Spans always go to an in-memory ring buffer that the UI's observability panel reads.
Set CRICKET_OTLP_ENDPOINT to also ship them to Jaeger, Tempo or any OTLP collector.
"""

from collections import OrderedDict, deque
from threading import Lock
from typing import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult


class RecentTraces(SpanExporter):
    """Keeps the last N traces, grouped by trace id."""

    def __init__(self, max_traces: int = 50):
        self._traces: OrderedDict[str, list[dict]] = OrderedDict()
        self._max = max_traces
        self._lock = Lock()

    INTERESTING = ("chat.", "ollama.", "rag.", "tool.", "agent.")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for s in spans:
                tid = format(s.context.trace_id, "032x")
                # Keep chat traces only; health checks and dashboard polling would push them out of the buffer.
                if tid not in self._traces and not s.name.startswith(self.INTERESTING):
                    continue
                self._traces.setdefault(tid, []).append({
                    "name": s.name,
                    "span_id": format(s.context.span_id, "016x"),
                    "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                    "start_ns": s.start_time,
                    "duration_ms": round((s.end_time - s.start_time) / 1e6, 2),
                    "status": s.status.status_code.name,
                    "attributes": {k: v for k, v in (s.attributes or {}).items()},
                })
                self._traces.move_to_end(tid)
            while len(self._traces) > self._max:
                self._traces.popitem(last=False)
        return SpanExportResult.SUCCESS

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._traces.items())[-limit:]
        out = []
        for tid, spans in reversed(items):
            spans = sorted(spans, key=lambda s: s["start_ns"])
            root = next((s for s in spans if s["parent_id"] is None), spans[0])
            t0 = spans[0]["start_ns"]
            out.append({
                "trace_id": tid,
                "root": root["name"],
                "duration_ms": root["duration_ms"],
                "spans": [{**s, "offset_ms": round((s["start_ns"] - t0) / 1e6, 2)} for s in spans],
            })
        return out

    def shutdown(self) -> None:
        pass


recent_traces = RecentTraces()
_errors: deque[dict] = deque(maxlen=100)


def setup_tracing(service_name: str, otlp_endpoint: str | None) -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(recent_traces))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)


def tracer():
    return trace.get_tracer("cricket_chatbot")


def record_error(component: str, message: str) -> None:
    import time

    _errors.append({"ts": time.time(), "component": component, "message": message[:300]})


def recent_errors() -> list[dict]:
    return list(reversed(_errors))
