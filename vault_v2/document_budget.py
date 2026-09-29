"""Admission errors for whole local document requests, never silent truncation."""

import json

from .errors import VaultError

DOCUMENT_CONTEXT_TOKENS = 8192
DOCUMENT_OUTPUT_TOKENS = 2048
DOCUMENT_TEMPLATE_RESERVE = 256
DOCUMENT_INPUT_TOKENS = DOCUMENT_CONTEXT_TOKENS - DOCUMENT_OUTPUT_TOKENS - DOCUMENT_TEMPLATE_RESERVE


class DocumentRequestRefused(VaultError):
    """A document attempt cannot publish model or baseline proposals."""

    def __init__(self, message: str, *, documents: tuple[str, ...] = (),
                 reading_notes: tuple[str, ...] = ()):
        self.public_message = message
        self.documents = tuple(documents)
        self.reading_notes = tuple(reading_notes)
        detail = message
        if self.documents:
            detail += "\nDocuments in this refused request:\n" + "\n".join(self.documents)
        if self.reading_notes:
            detail += "\nLocal reading notes (not model analysis):\n" + "\n".join(self.reading_notes)
        super().__init__(detail)


class DocumentBudgetExceeded(DocumentRequestRefused):
    """The whole attempt was refused before inference or proposal publication."""


class DocumentOutputIncomplete(DocumentRequestRefused):
    """Inference ran, but its incomplete output must not become proposals."""


def estimate_document_tokens(system: str, messages: list[dict]) -> int:
    """Conservative measured-prose estimate, not a tokenizer or server attestation.

    The minimum of the EN/RU/mixed minima is 4.439236... bytes/token;
    round DOWN to 4.43 and use it for every request rather than guessing its
    writing system. Add 25% and round UP, using integer arithmetic. The complete
    canonical envelope matches tools/calibrate_document_budget.py. See raw
    docs/document-budget-calibration.json for the measured model and limits.
    """
    raw = json.dumps({"system": system, "messages": messages}, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return (len(raw) * 125 + 442) // 443


def admit_document_request(system: str, messages: list[dict], *,
                           documents: tuple[str, ...] = (),
                           reading_notes: tuple[str, ...] = ()) -> None:
    """Refuse the complete over-budget request without any model call."""
    estimate = estimate_document_tokens(system, messages)
    if estimate > DOCUMENT_INPUT_TOKENS:
        raise DocumentBudgetExceeded(
            f"request too large: estimated {estimate} input tokens exceed the "
            f"{DOCUMENT_INPUT_TOKENS}-token input budget; the whole request was refused "
            "before model inference. Try fewer documents or a shorter request. "
            "No document was silently shortened and no proposals were published.",
            documents=documents, reading_notes=reading_notes)
