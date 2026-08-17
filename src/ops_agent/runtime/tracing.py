from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from ..config import Settings

LOGGER = logging.getLogger(__name__)
_CONFIGURED = False


def configure_tracing(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except ImportError:
        LOGGER.info("OpenTelemetry SDK is not installed; tracing disabled")
        _CONFIGURED = True
        return

    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint or None
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception as exc:  # pragma: no cover - optional exporter
            LOGGER.warning("OTLP exporter unavailable (%s); tracing stays local", exc)
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def get_tracer():
    try:
        from opentelemetry import trace

        return trace.get_tracer("ops_agent.runtime")
    except ImportError:
        return None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    tracer = get_tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is None:
                continue
            current.set_attribute(key, value)
        yield
