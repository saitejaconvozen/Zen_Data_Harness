"""Deterministic dialogue-act coherence checks.

`downstream_coherence` is self-reported by the model that made the edit, and an
independent verifier reviewing the same packet has been observed agreeing with a
wrong label. In conversation #426 a substantive answer ending "Shall I share more
details?" was replaced with "Could you please repeat?", the recorded next user
turn was "yeah is there a pick up and drop", and both models called that
PRESERVED. "yeah" answers the first question and cannot answer the second.

That specific failure is mechanically detectable: the replacement changed the
turn's dialogue act while the recorded reply still answers the original act.
These checks run in the harness, so they cannot be reasoned away.
"""

from __future__ import annotations

import re


# A reply that only makes sense as an answer to a yes/no or offer question.
AFFIRMATION = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "right",
    "ji", "haan", "han", "haa", "ha", "jee", "bilkul", "theek", "thik",
    "sahi", "achha", "accha", "hmm", "hm",
})
NEGATION = frozenset({
    "no", "nope", "nah", "nahi", "nahin", "na", "mat", "nope",
})

# Asking the caller to repeat or clarify — the act that most often replaces a
# real answer and silently breaks the recorded continuation.
CLARIFICATION_RE = re.compile(
    r"(didn'?t|did not)\s+(quite\s+)?(catch|understand|get)"
    r"|could you (please )?(repeat|say that again|clarify)"
    r"|say that again"
    r"|come again"
    r"|repeat that"
    r"|not sure (what|I understood)"
    r"|kya aap (dobara|phir se)"
    r"|samajh nahi"
    # Devanagari: these transcripts switch script mid-call, so the romanised
    # patterns alone miss half the clarification requests.
    r"|समझ नहीं"
    r"|दोबारा (बताइए|बताएं|कहिए|कहें)"
    r"|फिर से (बताइए|बताएं|कहिए)"
    r"|सुनाई नहीं",
    re.I,
)
LEADING_TAG_RE = re.compile(r"^\s*<\|[A-Z_]+\|>\s*")
MARKER_RE = re.compile(r"\b(PATIENCE|WAITING)\s+[\d.]+\b")


def _normalise(text: str) -> str:
    text = LEADING_TAG_RE.sub("", text or "")
    text = MARKER_RE.sub(" ", text)
    return text.strip()


def is_question(text: str) -> bool:
    return "?" in _normalise(text)


def is_clarification_request(text: str) -> bool:
    return bool(CLARIFICATION_RE.search(_normalise(text)))


def opens_with_reply_token(text: str) -> bool:
    """True when the user turn opens with a bare yes/no-style acknowledgement."""

    body = _normalise(text).lower()
    body = re.sub(r"^\[voice\]\s*", "", body)
    words = re.findall(r"[a-zऀ-ॿ]+", body)
    if not words:
        return False
    return words[0] in AFFIRMATION or words[0] in NEGATION


def coherence_violation(
    source_text: str, golden_text: str, next_user_text: str | None
) -> str | None:
    """Return why a replacement breaks the recorded reply, or None if it holds.

    Conservative by design: it reports only cases where the recorded reply
    demonstrably answers the source turn and cannot answer the replacement.
    """

    if not next_user_text:
        return None
    if not opens_with_reply_token(next_user_text):
        return None

    source_asks = is_question(source_text)
    golden_asks = is_question(golden_text)

    # The reply acknowledges something the replacement no longer asks.
    if source_asks and not golden_asks:
        return (
            "the recorded reply opens with an acknowledgement answering the "
            "source turn's question, which the replacement no longer asks"
        )
    # Substituting a request-to-repeat for a real question: an acknowledgement
    # is not a plausible response to "could you repeat that?".
    if source_asks and is_clarification_request(golden_text):
        return (
            "the replacement asks the caller to repeat, but the recorded reply "
            "is an acknowledgement of the source turn's question"
        )
    return None


def audit_decision(rows: list[dict], packet: dict) -> list[dict]:
    """Flag replacements whose PRESERVED label the transcript contradicts."""

    order = packet.get("turns") or []
    position = {turn["turn_id"]: index for index, turn in enumerate(order)}
    source_text = {
        turn["turn_id"]: turn.get("text", "")
        for turn in order
        if turn.get("role") == "assistant"
    }
    violations = []
    for row in rows:
        if row.get("action") != "REPLACE":
            continue
        if row.get("downstream_coherence") != "PRESERVED":
            continue
        index = position.get(row["turn_id"])
        if index is None:
            continue
        following = next(
            (turn for turn in order[index + 1:] if turn.get("role") == "user"), None
        )
        reason = coherence_violation(
            source_text.get(row["turn_id"], ""),
            row.get("golden_text", ""),
            (following or {}).get("text"),
        )
        if reason:
            violations.append({"turn_id": row["turn_id"], "reason": reason})
    return violations
