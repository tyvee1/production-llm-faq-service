"""Phoenix-backed RAG tracing.

Embedded mode (the default): ``phoenix.launch_app()`` starts an
in-process Phoenix UI at ``http://{host}:{port}`` — no Docker, no
signup. ``phoenix.otel.register(...)`` then registers an OpenTelemetry
``TracerProvider`` so spans are exported to that Phoenix instance, and
``OpenAIInstrumentor`` auto-traces every OpenAI SDK call.

The decorator emits a root ``rag_query`` span carrying RAG metadata
(latency, confidence, cost, top_k, model) plus a child ``rag_generation``
span carrying token counts and the answer text. The trace_id (32-char
hex) is injected back into the returned ``QueryResponse``.

When tracing is uninitialised (e.g. during tests, before ``init_tracing``
runs), OpenTelemetry returns a no-op tracer; the decorator still runs,
spans are dropped silently, and ``trace_id`` is ``None``.
"""

import functools
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from src.config import settings
from src.models import QueryResponse
from src.rag import run_pipeline
from src.tracing.base import summarize_sources

_tracer_provider = None
_phoenix_session = None


def init_tracing() -> None:
    """Launch embedded Phoenix (if configured) and register the tracer.

    Idempotent — safe to call from multiple lifespan startups.
    """
    global _tracer_provider, _phoenix_session

    if settings.phoenix_embedded and _phoenix_session is None:
        import phoenix as px

        # Phoenix deprecated `launch_app(host=, port=)` kwargs in favour of
        # PHOENIX_HOST / PHOENIX_PORT env vars; setdefault preserves any
        # explicit override (e.g. PHOENIX_HOST_ROOT_PATH set by make
        # serve-proxy alongside these in the Udacity Workspace flow).
        os.environ.setdefault("PHOENIX_WORKING_DIR", settings.phoenix_working_dir)
        os.environ.setdefault("PHOENIX_HOST", settings.phoenix_host)
        os.environ.setdefault("PHOENIX_PORT", str(settings.phoenix_port))
        _phoenix_session = px.launch_app()

    if _tracer_provider is None:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from phoenix.otel import register

        endpoint = (
            f"http://{settings.phoenix_host}:{settings.phoenix_port}/v1/traces"
        )
        _tracer_provider = register(
            project_name=settings.phoenix_project_name,
            endpoint=endpoint,
            verbose=False,
        )
        OpenAIInstrumentor().instrument(tracer_provider=_tracer_provider)


def flush() -> None:
    """Force-flush queued spans on shutdown."""
    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush()
        except Exception:
            # Shutdown path — never let a flush failure crash the server
            pass


def _set_str_attr(span, key: str, value) -> None:
    """OTel attributes must be primitives; coerce non-primitives to str."""
    if isinstance(value, (str, int, float, bool)):
        span.set_attribute(key, value)
    else:
        span.set_attribute(key, str(value))


@contextmanager
def step_span(name: str, **attributes) -> Iterator[Any]:
    """Open a named span around one pipeline step.

    Exists so a step that happens *outside* ``traced_pipeline`` can still
    appear in Phoenix under a name a reader recognises. The concrete case
    is classification: ``route_query`` classifies the question in order to
    decide which model tier to dispatch to, which necessarily happens
    before the pipeline it is choosing a model for. Without this, the
    classifier's cost and latency were visible only as a bare
    ``ChatCompletion`` span with no indication of what it was deciding.

    Note on shape: a span opened here has no parent, so it is the root of
    its own trace rather than a child of ``rag_query``. That is a true
    picture of the call graph rather than a limitation worked around —
    classification is not part of the request it precedes. The trace
    export in ``scripts/seed_traces.py`` filters to traces containing a
    ``rag_query`` root, so adding this leaves the slowest-step arithmetic
    for ``/query`` untouched.
    """
    tracer = otel_trace.get_tracer("src.tracing.phoenix_backend")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            _set_str_attr(span, key, value)
        yield span


def trace_rag_query(
    fn: Callable[..., QueryResponse],
) -> Callable[..., QueryResponse]:
    """Wrap a function returning ``QueryResponse`` with a Phoenix root span."""

    @functools.wraps(fn)
    def wrapper(question: str, *args, **kwargs) -> QueryResponse:
        tracer = otel_trace.get_tracer("src.tracing.phoenix_backend")
        with tracer.start_as_current_span("rag_query") as span:
            # CHAIN: this span orchestrates the stages rather than being
            # one. Required by the OpenInference spec on every span.
            span.set_attribute("openinference.span.kind", "CHAIN")
            span.set_attribute("input.value", question)
            for k, v in kwargs.items():
                _set_str_attr(span, f"rag.input.{k}", v)

            start = time.perf_counter()
            try:
                response = fn(question, *args, **kwargs)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise

            latency_ms = (time.perf_counter() - start) * 1000

            # A `rag_generation` span used to be opened HERE, after `fn`
            # had already returned. It carried the right attributes and
            # wrapped no work: zero duration, no child, and the vendor's
            # auto-instrumented `ChatCompletion` left as the only span
            # actually covering the model call. The real span now lives
            # inside `run_pipeline` around `generate(...)`. Opening a
            # second one here would duplicate the name within one trace
            # and make `summarize_traces` pick between two spans of which
            # only one has token counts.

            span.set_attribute("output.value", response.answer)
            span.set_attribute("rag.latency_ms", latency_ms)
            span.set_attribute("rag.confidence", response.confidence)
            span.set_attribute("rag.cost_usd", response.cost_usd)
            span.set_attribute("rag.top_k", len(response.sources))
            span.set_attribute("rag.model", response.model)
            span.set_attribute(
                "rag.sources", str(summarize_sources(response))
            )

            ctx = span.get_span_context()
            trace_id = (
                format(ctx.trace_id, "032x") if ctx and ctx.is_valid else None
            )
            return response.model_copy(update={"trace_id": trace_id})

    return wrapper


@trace_rag_query
def traced_pipeline(
    question: str, model: str | None = None, top_k: int = 5
) -> QueryResponse:
    """``rag.run_pipeline`` with Phoenix instrumentation.

    ``step_span`` is handed to the pipeline so the retrieval and
    generation stages are wrapped where the work happens, producing
    ``rag_retrieval`` and ``rag_generation`` as real children of
    ``rag_query`` rather than as metadata recorded afterwards.
    """
    return run_pipeline(question, model=model, top_k=top_k, step=step_span)
