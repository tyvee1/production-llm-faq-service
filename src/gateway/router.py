"""Tiered model routing — classify, dispatch to mini or 4o, log cost.

This is the integration hub: classify → choose model → run traced
pipeline → log per-request cost. Deliverable #3 of the project proposal
(measurable savings from tiered routing) is realised here.
"""

from src.config import settings
from src.cost.tracker import log_request
from src.gateway.classifier import QueryType, classify
from src.models import QueryResponse
from src.tracing import step_span, traced_pipeline


def select_model(query_type: QueryType) -> str:
    """Map a query type to the configured model name."""
    return settings.model_simple if query_type == "simple" else settings.model_complex


def route_query(question: str, top_k: int = 5) -> QueryResponse:
    """Full request flow: classify, dispatch, trace, log.

    The classification call is wrapped in its own named span. Before
    this, the routing decision reached Phoenix only as an unlabelled
    ``ChatCompletion`` — the same shape as the answer-generating call and
    the hallucination judge's call, so three LLM calls with three
    different jobs were indistinguishable in a trace. The decision that
    determines what the request costs is the one you most want to see by
    name when the bill moves.

    The span records the label it chose, which also makes the classifier's
    fallback visible: ``classify`` returns ``"complex"`` on malformed
    JSON, so a run where every request lands on the expensive tier looks
    identical to a run where every question genuinely was complex. With
    the label on the span, the two are distinguishable after the fact.
    """
    # One span around the whole request, so classification and the RAG
    # pipeline land in the SAME trace. Without it they are two sibling
    # roots — `rag_classification` in one trace, `rag_query` in another —
    # and no single trace can show classification, retrieval and
    # generation together, which is what an operator opens a trace to see.
    with step_span(
        "rag_request",
        **{"openinference.span.kind": "CHAIN", "input.value": question},
    ) as request_span:
        with step_span(
            "rag_classification",
            **{"openinference.span.kind": "LLM", "input.value": question},
        ) as span:
            query_type = classify(question)
            span.set_attribute("rag.classification", query_type)

        model = select_model(query_type)
        request_span.set_attribute("rag.classification", query_type)
        request_span.set_attribute("rag.model", model)

        response = traced_pipeline(question, model=model, top_k=top_k)
        log_request(model, response.tokens, response.cost_usd, query_type)
        return response
