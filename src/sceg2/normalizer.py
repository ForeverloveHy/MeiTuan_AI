from __future__ import annotations

import re
import unicodedata

_PUNCT = "，。！？；：、,.!?;:\n\t\r（）()【】[]{}<>《》‘’“”\"'"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.strip()
    for ch in _PUNCT:
        text = text.replace(ch, " ")
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?；;\n]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def contains_any(text: str, patterns: list[str]) -> bool:
    return any(p and p in text for p in patterns)


def contains_all(text: str, patterns: list[str]) -> bool:
    return all((p in text) for p in patterns if p)


def regex_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns if p)
