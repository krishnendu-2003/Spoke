"""Custom vocabulary: Whisper prompt biasing + post-cleanup replacements + hallucination filter."""

from __future__ import annotations

import re
import string
from functools import lru_cache

# Whisper's prompt limit is 224 tokens (Groq docs). Proper nouns tokenize badly
# ("TigerBeetle" ~ 4 tokens), so estimate conservatively and keep a margin.
WHISPER_PROMPT_TOKEN_LIMIT = 224
PROMPT_TOKEN_BUDGET = 200


def estimate_tokens(text: str) -> int:
    """Conservative GPT-2-BPE estimate: ~1 token per 2.5 chars, plus one per word boundary.

    Deliberately over-counts so the real prompt never exceeds Whisper's limit.
    """
    words = text.split()
    return sum(1 + int(len(w) / 2.5) for w in words)


def build_prompt(vocab: list[str], budget: int = PROMPT_TOKEN_BUDGET) -> str:
    """Comma-separated vocab list, truncated (keeping earliest = highest priority) to fit budget.

    A natural-sentence style prompt biases Whisper toward the spellings without making it
    hallucinate the list itself on silence (silence never reaches the API anyway).
    """
    terms = [t.strip() for t in vocab if t and t.strip()]
    if not terms:
        return ""
    prefix = "Vocabulary:"
    out: list[str] = []
    for term in terms:
        candidate = prefix + " " + ", ".join(out + [term]) + "."
        if estimate_tokens(candidate) > budget:
            break
        out.append(term)
    if not out:
        return ""
    return prefix + " " + ", ".join(out) + "."


@lru_cache(maxsize=8)
def _compile_replacements(items: tuple[tuple[str, str], ...]) -> list[tuple[re.Pattern[str], str]]:
    compiled = []
    # Longest keys first so "next js" wins over "next".
    for spoken, canonical in sorted(items, key=lambda kv: -len(kv[0])):
        spoken = spoken.strip()
        if not spoken:
            continue
        # Allow any run of whitespace/hyphens between words ("tiger  beetle", "tiger-beetle").
        parts = [re.escape(p) for p in re.split(r"[\s\-]+", spoken)]
        body = r"[\s\-]+".join(parts)
        pattern = re.compile(r"(?<![\w.\-])" + body + r"(?![\w\-]|\.\w)", re.IGNORECASE)
        compiled.append((pattern, canonical))
    return compiled


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    if not text or not replacements:
        return text
    for pattern, canonical in _compile_replacements(tuple(replacements.items())):
        text = pattern.sub(lambda _m, c=canonical: c, text)
    return text


_PUNCT_TABLE = str.maketrans("", "", string.punctuation + "¡¿…“”‘’«»")


def _normalize(text: str) -> str:
    return " ".join(text.lower().translate(_PUNCT_TABLE).split())


def is_hallucination(text: str, blocklist: list[str]) -> bool:
    """True if the transcript is empty/punctuation-only or matches the blocklist."""
    norm = _normalize(text)
    if not norm:
        return True
    for entry in blocklist:
        if entry.startswith("re:"):
            try:
                if re.search(entry[3:], text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        elif norm == _normalize(entry):
            return True
    return False
