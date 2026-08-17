"""Deterministic assistant-turn format analysis.

Voice-agent system prompts in this domain mandate a leading language tag such as
``<|ENGLISH|>`` on every assistant response. Detecting and repairing a missing
tag is a mechanical string operation, so the harness performs it here instead of
spending model budget and a taxonomy annotation on it.

The module reports findings; it never rewrites a turn on its own. Packet
preparation attaches the findings so the refiner can treat tag compliance as
already-decided input and concentrate on conversational quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata


TAG_RE = re.compile(r"<\|([A-Z_]+)\|>")
LEADING_TAG_RE = re.compile(r"^\s*<\|([A-Z_]+)\|>")

# Placeholders name the tag slot rather than a concrete language.
TAG_PLACEHOLDERS = frozenset({"IDENTIFIED_INPUT_LANGUAGE", "LANGUAGE_TAG"})

# Unicode script ranges that identify the languages this taxonomy covers.
SCRIPT_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("DEVANAGARI", "HINDI"),
    ("ARABIC", "ARABIC"),
    ("TAMIL", "TAMIL"),
)


@dataclass(slots=True)
class FormatFinding:
    """One deterministic format observation about a single assistant turn."""

    turn_id: str
    compliant: bool
    observed_tag: str | None = None
    proposed_tag: str | None = None
    proposed_text: str | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "compliant": self.compliant,
            "observed_tag": self.observed_tag,
            "proposed_tag": self.proposed_tag,
            "proposed_text": self.proposed_text,
            "issues": list(self.issues),
        }


def declared_language_tags(system_prompt: str) -> frozenset[str]:
    """Return the concrete language tags a system prompt actually declares."""

    return frozenset(
        tag for tag in TAG_RE.findall(system_prompt) if tag not in TAG_PLACEHOLDERS
    )


def requires_language_tag(system_prompt: str) -> bool:
    """True when the system prompt mandates a leading language tag."""

    return bool(TAG_RE.search(system_prompt))


# Romanised Hindi is written in Latin script, so script detection alone reads it
# as English and mis-tags the turn. These markers are Hindi function words with
# no common English homograph — "main", "the", "ho" and friends are deliberately
# absent because they collide with English.
ROMAN_HINDI_MARKERS = frozenset({
    "aap", "aapka", "aapki", "aapko", "aapse", "hain", "kya", "nahi", "nahin",
    "mera", "meri", "mujhe", "hoon", "hun", "karenge", "karna", "karke", "kijiye",
    "sakte", "sakta", "sakti", "chahiye", "kyunki", "kyun", "kaise", "kahan",
    "kitna", "kitne", "jayega", "jayegi", "jaye", "raha", "rahi", "rahe",
    "haan", "theek", "thik", "accha", "achha", "bhi", "yeh", "woh", "abhi",
    "bataye", "bataiye", "batata", "hoga", "hogi", "thi", "mein", "aur",
    "liye", "wala", "wali", "bilkul", "zaroor", "thoda", "bahut", "kripya",
    "dhanyavaad", "namaste", "baat", "karta", "karti", "diya", "gaya",
})
_WORD_RE = re.compile(r"[a-z]+")

# Two distinct markers keeps a stray loan-word from flipping an English turn.
ROMAN_HINDI_MIN_MARKERS = 2


def detect_script_language(text: str) -> str | None:
    """Infer a language tag from ``text``.

    Native scripts are decided by their Unicode block. Latin text is checked for
    romanised Hindi before falling back to English, because these transcripts
    routinely carry Hindi written in Latin characters.
    """

    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        script = name.split(" ", 1)[0]
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None
    dominant = max(counts, key=counts.__getitem__)
    for script, language in SCRIPT_LANGUAGES:
        if dominant == script:
            return language
    if dominant != "LATIN":
        return None
    stripped = LEADING_TAG_RE.sub("", text, count=1)
    markers = {
        word for word in _WORD_RE.findall(stripped.lower())
        if word in ROMAN_HINDI_MARKERS
    }
    if len(markers) >= ROMAN_HINDI_MIN_MARKERS:
        return "HINDI"
    return "ENGLISH"


def analyze_turn(
    turn_id: str, text: str, declared: frozenset[str], *, required: bool
) -> FormatFinding:
    """Analyse one assistant turn against the declared tag vocabulary."""

    if not required:
        return FormatFinding(turn_id=turn_id, compliant=True)

    leading = LEADING_TAG_RE.match(text)
    if leading is not None:
        tag = leading.group(1)
        if tag in TAG_PLACEHOLDERS:
            return FormatFinding(
                turn_id=turn_id,
                compliant=False,
                observed_tag=tag,
                issues=[f"leading tag <|{tag}|> is an unresolved placeholder"],
            )
        if declared and tag not in declared:
            return FormatFinding(
                turn_id=turn_id,
                compliant=False,
                observed_tag=tag,
                issues=[f"leading tag <|{tag}|> is not declared by the system prompt"],
            )
        return FormatFinding(turn_id=turn_id, compliant=True, observed_tag=tag)

    inferred = detect_script_language(text)
    if inferred is None or (declared and inferred not in declared):
        # Fall back to the sole declared tag when the script is ambiguous.
        inferred = next(iter(sorted(declared))) if len(declared) == 1 else None
    if inferred is None:
        return FormatFinding(
            turn_id=turn_id,
            compliant=False,
            issues=["mandatory leading language tag is absent and undeterminable"],
        )
    return FormatFinding(
        turn_id=turn_id,
        compliant=False,
        proposed_tag=inferred,
        proposed_text=f"<|{inferred}|> {text.lstrip()}",
        issues=["mandatory leading language tag is absent"],
    )


def analyze_conversation(
    system_prompt: str, turns: list[dict[str, object]]
) -> list[FormatFinding]:
    """Analyse every assistant turn in one conversation."""

    required = requires_language_tag(system_prompt)
    declared = declared_language_tags(system_prompt)
    findings = []
    for turn in turns:
        if turn.get("role") != "assistant":
            continue
        text = turn.get("text")
        if not isinstance(text, str):
            continue
        findings.append(
            analyze_turn(str(turn["turn_id"]), text, declared, required=required)
        )
    return findings


def summarize(findings: list[FormatFinding]) -> dict[str, object]:
    """Summarise findings for packet metadata and operator reporting."""

    non_compliant = [finding for finding in findings if not finding.compliant]
    return {
        "assistant_turns": len(findings),
        "compliant": len(findings) - len(non_compliant),
        "non_compliant": len(non_compliant),
        "auto_repairable": sum(
            1 for finding in non_compliant if finding.proposed_text is not None
        ),
    }
