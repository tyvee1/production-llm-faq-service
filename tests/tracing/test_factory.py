"""Tests for the tracing backend factory in src/tracing/__init__.py."""

import importlib

import pytest

import src.tracing
from src.config import settings


def test_each_backend_module_exports_required_symbols():
    """Every backend implements the same four-symbol contract."""
    from src.tracing import noop_backend, phoenix_backend

    for module in (noop_backend, phoenix_backend):
        assert callable(module.init_tracing)
        assert callable(module.flush)
        assert callable(module.trace_rag_query)
        assert callable(module.traced_pipeline)


def test_default_backend_is_phoenix():
    assert settings.tracing_backend == "phoenix"
    assert (
        src.tracing.traced_pipeline.__module__
        == "src.tracing.phoenix_backend"
    )


@pytest.mark.parametrize(
    "backend,expected_module",
    [
        ("phoenix", "src.tracing.phoenix_backend"),
        ("none", "src.tracing.noop_backend"),
    ],
)
def test_factory_selects_module_for_each_backend(
    monkeypatch, backend, expected_module
):
    monkeypatch.setattr(settings, "tracing_backend", backend)
    importlib.reload(src.tracing)
    assert src.tracing.traced_pipeline.__module__ == expected_module


def test_each_backend_exports_step_span():
    """Both backends must offer step_span with the same calling shape.

    The gateway router calls ``span.set_attribute`` on whatever the
    context manager yields, without checking which backend is live. If
    the no-op backend yielded ``None`` instead of a null object, every
    request would raise ``AttributeError`` the moment tracing was
    switched off — a failure that only shows up in the configuration
    nobody runs locally.
    """
    from src.tracing import noop_backend, phoenix_backend

    for module in (noop_backend, phoenix_backend):
        assert callable(module.step_span)
        with module.step_span("probe", **{"input.value": "x"}) as span:
            span.set_attribute("rag.classification", "simple")


def test_step_span_is_reexported_by_the_factory():
    import src.tracing

    assert "step_span" in src.tracing.__all__
    assert callable(src.tracing.step_span)
