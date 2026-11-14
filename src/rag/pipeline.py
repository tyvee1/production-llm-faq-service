"""End-to-end RAG flow: retrieve -> prompt -> generate -> respond."""

from contextlib import contextmanager

from src.config import settings
from src.models import QueryResponse
from src.rag.generator import generate
from src.rag.retriever import retrieve


class _UntracedStep:
    """Accepts the span API and discards it.

    Lets the pipeline set span attributes unconditionally instead of
    branching on whether tracing is active. A caller that has to write
    ``if span is not None`` is a caller that will eventually forget to.
    """

    def set_attribute(self, key: str, value) -> None:  # noqa: ANN001
        return None


@contextmanager
def _untraced(name: str, **attributes):
    """Default ``step`` factory: name the step, record nothing."""
    yield _UntracedStep()


def run_pipeline(
    question: str,
    model: str | None = None,
    top_k: int = 5,
    step=None,  # noqa: ANN001
) -> QueryResponse:
    """Run the full RAG pipeline and return a structured QueryResponse.

    ``step`` is an optional context-manager factory with the signature
    ``step(name, **attributes)``, yielding an object that accepts
    ``set_attribute``. It exists so the two expensive stages can be
    wrapped in named spans by whoever is doing the tracing, while this
    module stays independent of the tracing package.

    The alternative — importing the tracer here — is a forward
    dependency: ``src.rag`` is taught before ``src.tracing``, and
    ``tests/integration/test_dependency_graph.py`` enforces that order.
    Injecting the factory satisfies the observability requirement
    without weakening that rule, and leaves ``run_pipeline(question)``
    behaving exactly as before for every existing caller.
    """
    step = step or _untraced
    chosen_model = model or settings.model_complex

    # Wrap the work, not the aftermath. A span opened after the call it
    # describes records the metadata and measures nothing, which is how
    # a trace ends up showing an auto-instrumented vendor span as the
    # slowest child with no application span around it.
    with step(
        "rag_retrieval",
        **{
            # `openinference.span.kind` is required by the OpenInference
            # spec and is what tells Phoenix how to render the span. Set
            # as a plain string rather than importing the semantic-
            # conventions package, so this adds no dependency and the
            # lockfile is untouched. Without it the span still appears
            # and is still labelled, but it is typeless — the platform
            # cannot show it as a retrieval, and a relevance evaluator
            # looking for RETRIEVER spans will not find it.
            "openinference.span.kind": "RETRIEVER",
            "input.value": question,
            "rag.top_k": top_k,
        },
    ) as span:
        sources = retrieve(question, top_k=top_k)
        span.set_attribute("rag.retrieved_count", len(sources))
        span.set_attribute("rag.source_doc_ids", ",".join(s.doc_id for s in sources))
        # The conventional shape for retrieved documents: a list flattened
        # with indexed prefixes, so each document's id, score and content
        # are addressable. This is what renders as a document list rather
        # than as an opaque string.
        for i, source in enumerate(sources):
            span.set_attribute(f"retrieval.documents.{i}.document.id", source.doc_id)
            span.set_attribute(
                f"retrieval.documents.{i}.document.score", source.similarity_score
            )
            span.set_attribute(
                f"retrieval.documents.{i}.document.content", source.chunk_text[:500]
            )

    with step(
        "rag_generation",
        **{"openinference.span.kind": "LLM", "llm.model_name": chosen_model},
    ) as span:
        answer, usage, cost = generate(question, sources, chosen_model)
        span.set_attribute("input.value", question)
        span.set_attribute("output.value", answer)
        span.set_attribute("llm.token_count.prompt", usage.prompt_tokens)
        span.set_attribute("llm.token_count.completion", usage.completion_tokens)
        span.set_attribute("llm.token_count.total", usage.total)
        span.set_attribute("rag.cost_usd", cost)

    confidence = (
        sum(s.similarity_score for s in sources) / len(sources) if sources else 0.0
    )
    return QueryResponse(
        answer=answer,
        sources=sources,
        confidence=confidence,
        model=chosen_model,
        tokens=usage,
        cost_usd=cost,
    )
