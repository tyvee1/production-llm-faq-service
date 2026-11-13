"""No-op tracing backend.

Selected when ``settings.tracing_backend == "none"``. The decorator is
a pass-through; ``trace_id`` is always ``None``. Useful for environments
where no tracing infrastructure is reachable (CI, scratch dev) and for
tests that want to exercise the request flow without span machinery.
"""

import functools
from contextlib import contextmanager
from typing import Callable, Iterator

from src.models import QueryResponse
from src.rag import run_pipeline


class _NullSpan:
    """Accepts the span API and discards it.

    Yielded by ``step_span`` so callers can set attributes
    unconditionally instead of branching on which backend is active. A
    caller that has to write ``if span is not None`` is a caller that
    will eventually forget to.
    """

    def set_attribute(self, key: str, value) -> None:  # noqa: ANN001
        return None


def init_tracing() -> None:
    return None


def flush() -> None:
    return None


@contextmanager
def step_span(name: str, **attributes) -> Iterator[_NullSpan]:
    """No-op counterpart to the Phoenix ``step_span``."""
    yield _NullSpan()


def trace_rag_query(
    fn: Callable[..., QueryResponse],
) -> Callable[..., QueryResponse]:
    @functools.wraps(fn)
    def wrapper(question: str, *args, **kwargs) -> QueryResponse:
        response = fn(question, *args, **kwargs)
        return response.model_copy(update={"trace_id": None})

    return wrapper


@trace_rag_query
def traced_pipeline(
    question: str, model: str | None = None, top_k: int = 5
) -> QueryResponse:
    return run_pipeline(question, model=model, top_k=top_k)
