"""Input-side validators: prompt injection detection and PII redaction.

Lightweight regex/heuristic implementations chosen over Guardrails Hub
validators for capstone reliability. The Hub validators (e.g.
``hub://guardrails/detect_pii``) are the production upgrade path covered
in the Guardrails module's content; LLM Guard's ML-backed scanners (in
the sibling ``llm_guard/`` package) are a second upgrade path that runs
locally without external accounts.

To add a new injection pattern, append a ``re.compile(...)`` to
``INJECTION_PATTERNS``. To add a new PII type, add an entry to
``PII_PATTERNS`` and a matching redaction string in ``PII_REDACTIONS``.
"""

import re

INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(an?\s+)?", re.IGNORECASE),
    re.compile(r"\bpretend\s+(to\s+be|you\s+are)\b", re.IGNORECASE),
    re.compile(r"^\s*system\s*[:>]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*assistant\s*[:>]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*\|?\s*im_start\s*\|?\s*>", re.IGNORECASE),
    re.compile(
        r"\b(reveal|show|print|leak|repeat|tell\s+me)\s+(your|the)\s+"
        r"(system|initial|original|hidden|secret)\s+(prompt|instructions?|rules?|message)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(bypass|override|turn\s+off|disable|ignore)\s+(your|the|all)?\s*"
        r"(safety|guardrails?|filters?|restrictions?|content\s+policy|policies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(jailbreak\s+mode|DAN\s+mode|developer\s+mode|do\s+anything\s+now)\b",
        re.IGNORECASE,
    ),
    # --- Deliverable 6 additions ---------------------------------------
    # Five patterns added for the capstone. Each one covers an attack
    # family the eleven shipped patterns above do not reach, and each was
    # chosen so that no phrasing a pickleball customer would plausibly use
    # can match it. The tightness matters more than the coverage here: a
    # false positive on this layer returns the canned refusal to a paying
    # customer, and because the regex layer short-circuits before the
    # DeBERTa scanner, nothing downstream gets the chance to overrule it.
    #
    # D6-1 — Chat-template role forgery.
    # The shipped set already blocks `system:` / `assistant:` at the start
    # of a line and the ChatML `<|im_start|>` marker. It does not cover the
    # Llama-family (`[INST]`, `<<SYS>>`) or Alpaca-family (`### Instruction:`)
    # delimiters, which are the same attack expressed in a different
    # model's template. Any of these appearing in a customer question is
    # an attempt to open a forged turn, never a product enquiry.
    re.compile(
        r"(\[/?INST\]|<</?SYS>>|^\s*#{2,}\s*(instruction|system|response)\s*:)",
        re.IGNORECASE | re.MULTILINE,
    ),
    # D6-2 — Encoded-payload smuggling.
    # Asks the model to decode a payload and act on what comes out,
    # which routes around every pattern that matches on plain-language
    # instruction text. Requires BOTH an encoding name and an act-on-it
    # verb in sequence, so a hypothetical support question that merely
    # mentions base64 in passing does not match.
    re.compile(
        r"\b(base64|rot13|hex|url|percent)[\s-]?(?:en|de)coded?\b[^.?!]{0,40}?"
        r"\b(decode|execute|run|follow|obey|interpret|then)\b",
        re.IGNORECASE,
    ),
    # D6-3 — Retrieval-context exfiltration.
    # The shipped set protects the system prompt but not the retrieved
    # corpus, which in a multi-tenant deployment is the more valuable
    # target: it is the thing that differs per customer. Deliberately
    # excludes the word "product" so that "list all the products you
    # carry" — a legitimate and common FAQ question — stays silent.
    re.compile(
        r"\b(list|dump|output|print|show|export|enumerate|recite)\s+"
        r"(me\s+)?(all\s+)?(of\s+)?(the|your)\s+(retrieved\s+|raw\s+|internal\s+)?"
        r"(context|chunks?|documents?|embeddings?|vector\s*(db|database|store)|"
        r"knowledge\s*base|corpus|sources?\s+verbatim)\b",
        re.IGNORECASE,
    ),
    # D6-4 — Persistent instruction override.
    # The shipped `ignore previous instructions` family is backward-
    # looking. This is its forward-looking twin: it does not ask the model
    # to discard anything, it asserts a replacement policy, which is why
    # the existing patterns miss it entirely.
    re.compile(
        r"\b(your\s+)?(new|updated|revised|real|actual)\s+(system\s+)?"
        r"(instructions?|rules?|prompt|policy|policies|directives?)\s+(are|is|:)",
        re.IGNORECASE,
    ),
    # D6-5 — Code and tool execution.
    # Targets the agentic failure mode rather than the chat one. This
    # service has no tool-calling surface today, so nothing here can
    # currently succeed — the pattern is a tripwire that fires if someone
    # starts probing for one, and a blocked request is a signal worth
    # having in the cost log before the capability exists rather than
    # after.
    #
    # Two narrowings, both forced by a near-miss control that the first
    # draft failed. "query" was in the noun list and matched "Can I run
    # this query past your support team?" — ordinary English, and exactly
    # the phrasing of a customer who is about to be handed a refusal.
    # SQL is still covered by its own noun, so dropping "query" costs no
    # real coverage. The trailing lookahead then kills the "run it past
    # someone" / "run it by someone" idiom generally, rather than
    # patching the one sentence that happened to be tested.
    re.compile(
        r"\b(execute|run|eval(?:uate)?|interpret)\s+(the\s+)?(following|this|these)\s+"
        r"(code|script|python|javascript|command|sql|shell|bash|payload)\b"
        r"(?!\s+(past|by)\b)",
        re.IGNORECASE,
    ),
]

PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # --- Deliverable 6 additions ---------------------------------------
    # Two sensitive-data types the shipped four do not cover. Both are
    # written strictly rather than permissively, for the same reason the
    # injection additions are: `detect_pii` runs on every single question,
    # and a false positive here puts a `pii_redacted:` marker on an
    # otherwise clean customer answer.
    #
    # IPv4, with per-octet range validation (0-255) rather than the usual
    # \d{1,3} shorthand. The loose form matches "999.999.999.999" and,
    # more to the point in this corpus, risks matching dotted numeric runs
    # in product specifications. Four valid octets in sequence does not
    # occur anywhere in the 36-product catalogue; a support transcript
    # pasted by a customer is the realistic source.
    "ip_address": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\b"
    ),
    # IBAN, ISO 13616 shape: two-letter country code, two check digits,
    # then 11-30 alphanumerics. The length floor is what keeps it off
    # product model codes — "T700" and "X5" are far too short to reach it,
    # and the leading two-letter-then-two-digit requirement excludes the
    # alphanumeric SKU patterns used elsewhere in the catalogue. Case-
    # sensitive on purpose: a real IBAN is written in uppercase, and
    # allowing lowercase widens the match surface for no gain.
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b"),
    # --- Moved, not added ----------------------------------------------
    # `credit_card` was the fourth entry in the shipped dict. It is last
    # now, and the move is load-bearing rather than cosmetic.
    #
    # `detect_pii` iterates this dict in insertion order and each pattern
    # runs against the text the *previous* pattern already redacted. So
    # declaration order decides which label a given string gets, and the
    # shipped pattern — any run of 13 to 16 digits with spaces or hyphens
    # allowed between them — is loose enough to swallow a good deal that
    # is not a card number. A control caught it consuming the digits
    # inside "GB82 WEST 1234 5698 7654 32", so a European customer's bank
    # details were being redacted correctly but reported to the caller as
    # `pii_redacted: credit_card`. The data was safe; the telemetry was
    # wrong, and a label that is wrong in the log is worse than no label,
    # because someone will later count card-number incidents from it.
    #
    # The general rule this is an instance of: in a redaction chain,
    # specific patterns must precede permissive ones. The alternative fix
    # — tightening the card pattern with a Luhn check — is the better
    # production answer, but it changes the behaviour of shipped code the
    # rubric does not ask me to touch, so it is noted in WRITEUP §6 as
    # follow-up rather than done here.
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
}

PII_REDACTIONS: dict[str, str] = {
    "email": "[REDACTED_EMAIL]",
    "phone": "[REDACTED_PHONE]",
    "ssn": "[REDACTED_SSN]",
    # --- Deliverable 6 additions ---
    "ip_address": "[REDACTED_IP]",
    "iban": "[REDACTED_IBAN]",
    "credit_card": "[REDACTED_CARD]",
}


def detect_prompt_injection(text: str) -> str | None:
    """Return a reason string if the input looks like prompt injection, else None."""
    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return f"prompt_injection: matched pattern {pattern.pattern!r}"
    return None


def detect_pii(text: str) -> tuple[str, list[str]]:
    """Redact known PII kinds from ``text``. Returns ``(redacted, kinds_found)``."""
    found: list[str] = []
    redacted = text
    for kind, pattern in PII_PATTERNS.items():
        if pattern.search(redacted):
            found.append(kind)
            redacted = pattern.sub(PII_REDACTIONS[kind], redacted)
    return redacted, found
