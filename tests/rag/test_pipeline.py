"""Tests for src.rag.pipeline.run_pipeline."""

from unittest.mock import patch

from src.config import settings
from src.models import QueryResponse, Source, TokenUsage
from src.rag import run_pipeline


def test_run_pipeline_returns_full_query_response():
    sources = [
        Source(doc_id="p1", chunk_text="A", similarity_score=0.9),
        Source(doc_id="p2", chunk_text="B", similarity_score=0.7),
    ]
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20)

    with patch("src.rag.pipeline.retrieve", return_value=sources) as retrieve_fn, \
         patch(
             "src.rag.pipeline.generate",
             return_value=("the answer", usage, 0.0042),
         ) as generate_fn:
        response = run_pipeline("Which paddle is best?", model="gpt-4o-mini", top_k=3)

    retrieve_fn.assert_called_once_with("Which paddle is best?", top_k=3)
    generate_fn.assert_called_once_with(
        "Which paddle is best?", sources, "gpt-4o-mini"
    )

    assert isinstance(response, QueryResponse)
    assert response.answer == "the answer"
    assert response.sources == sources
    assert response.confidence == 0.8  # mean of 0.9 and 0.7
    assert response.model == "gpt-4o-mini"
    assert response.tokens == usage
    assert response.cost_usd == 0.0042
    assert response.cached is False
    assert response.trace_id is None


def test_run_pipeline_defaults_to_complex_model():
    with patch("src.rag.pipeline.retrieve", return_value=[]), \
         patch(
             "src.rag.pipeline.generate",
             return_value=("ans", TokenUsage(prompt_tokens=1, completion_tokens=1), 0.0),
         ) as generate_fn:
        response = run_pipeline("Q")

    assert response.model == settings.model_complex
    generate_fn.assert_called_once_with("Q", [], settings.model_complex)


def test_run_pipeline_zero_confidence_when_no_sources():
    with patch("src.rag.pipeline.retrieve", return_value=[]), \
         patch(
             "src.rag.pipeline.generate",
             return_value=("ans", TokenUsage(prompt_tokens=1, completion_tokens=1), 0.0),
         ):
        response = run_pipeline("Q")

    assert response.sources == []
    assert response.confidence == 0.0


# --- step spans -------------------------------------------------------
#
# The defect these cover: a span can carry exactly the right attributes
# and still wrap no work, if it is opened after the call it describes.
# Asserting the span NAMES is not enough to catch that, so these record
# the interleaving of span entry/exit against the calls themselves.


class _RecordingSpan:
    def __init__(self, log, name):
        self._log, self._name = log, name
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


def _recording_step(log, spans):
    """A ``step`` factory that logs enter/exit around each named stage."""
    from contextlib import contextmanager

    @contextmanager
    def step(name, **attributes):
        span = _RecordingSpan(log, name)
        span.attributes.update(attributes)
        spans[name] = span
        log.append(("enter", name))
        try:
            yield span
        finally:
            log.append(("exit", name))

    return step


def _stub_sources():
    return [
        Source(doc_id="p1", chunk_text="A", similarity_score=0.9),
        Source(doc_id="p2", chunk_text="B", similarity_score=0.7),
    ]


def test_each_stage_is_wrapped_by_a_span_around_the_call_itself():
    log, spans = [], {}
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20)

    def fake_retrieve(question, top_k=5):
        log.append(("call", "retrieve"))
        return _stub_sources()

    def fake_generate(question, sources, model):
        log.append(("call", "generate"))
        return "ans", usage, 0.0042

    with patch("src.rag.pipeline.retrieve", side_effect=fake_retrieve), patch(
        "src.rag.pipeline.generate", side_effect=fake_generate
    ):
        run_pipeline("Q", model="gpt-4o-mini", step=_recording_step(log, spans))

    # Each call sits strictly between its own span's enter and exit.
    assert log == [
        ("enter", "rag_retrieval"),
        ("call", "retrieve"),
        ("exit", "rag_retrieval"),
        ("enter", "rag_generation"),
        ("call", "generate"),
        ("exit", "rag_generation"),
    ]


def test_stage_spans_carry_the_attributes_an_operator_searches_on():
    log, spans = [], {}
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20)

    with patch("src.rag.pipeline.retrieve", return_value=_stub_sources()), patch(
        "src.rag.pipeline.generate", return_value=("ans", usage, 0.0042)
    ):
        run_pipeline("Q", model="gpt-4o-mini", step=_recording_step(log, spans))

    retrieval = spans["rag_retrieval"].attributes
    assert retrieval["rag.top_k"] == 5
    assert retrieval["rag.retrieved_count"] == 2
    assert retrieval["rag.source_doc_ids"] == "p1,p2"

    generation = spans["rag_generation"].attributes
    assert generation["llm.model_name"] == "gpt-4o-mini"
    assert generation["llm.token_count.prompt"] == 100
    assert generation["llm.token_count.completion"] == 20
    assert generation["llm.token_count.total"] == 120
    assert generation["rag.cost_usd"] == 0.0042


def test_pipeline_runs_untraced_when_no_step_factory_is_given():
    """The default must stay a no-op, or every existing caller changes."""
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1)
    with patch("src.rag.pipeline.retrieve", return_value=_stub_sources()), patch(
        "src.rag.pipeline.generate", return_value=("ans", usage, 0.0)
    ):
        response = run_pipeline("Q")
    assert isinstance(response, QueryResponse)
    assert response.answer == "ans"


def test_stage_spans_declare_an_openinference_span_kind():
    """`openinference.span.kind` is required by the spec and is what the
    tracing backend uses to render a span as a retrieval or a model call.
    Omitting it leaves the span labelled but typeless, so an evaluator
    looking for RETRIEVER spans cannot find it."""
    log, spans = [], {}
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1)
    with patch("src.rag.pipeline.retrieve", return_value=_stub_sources()), patch(
        "src.rag.pipeline.generate", return_value=("ans", usage, 0.0)
    ):
        run_pipeline("Q", step=_recording_step(log, spans))

    assert spans["rag_retrieval"].attributes["openinference.span.kind"] == "RETRIEVER"
    assert spans["rag_generation"].attributes["openinference.span.kind"] == "LLM"


def test_retrieved_documents_use_the_conventional_indexed_shape():
    """A joined string of ids is readable but opaque to the platform.
    The convention flattens the list so each document's id, score and
    content are addressable."""
    log, spans = [], {}
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1)
    with patch("src.rag.pipeline.retrieve", return_value=_stub_sources()), patch(
        "src.rag.pipeline.generate", return_value=("ans", usage, 0.0)
    ):
        run_pipeline("Q", step=_recording_step(log, spans))

    attrs = spans["rag_retrieval"].attributes
    assert attrs["retrieval.documents.0.document.id"] == "p1"
    assert attrs["retrieval.documents.0.document.score"] == 0.9
    assert attrs["retrieval.documents.0.document.content"] == "A"
    assert attrs["retrieval.documents.1.document.id"] == "p2"
    # One entry per retrieved source, and no more.
    ids = [k for k in attrs if k.endswith(".document.id")]
    assert len(ids) == 2
