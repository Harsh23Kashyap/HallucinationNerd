"""Match reference files the user uploads to the document's bibliography.

Used when the reviewer supplies the cited papers themselves, so their text does not have to be
fetched online. A file is matched to a bibliography entry by, in order:

1. a number in its file name: "3.pdf", "[3] Smith 2020.pdf", "ref3.pdf", "ref_3.txt";
2. the entry's title appearing, word for word, in the opening text of the file. The title is
   the parsed one or, when that is poor (short forms like "Smith et al. Title. Venue 2020."),
   the first piece of the raw entry that is neither the author list nor the venue.

A match is only made when it is unambiguous. Files that match nothing are reported back, never
guessed.
"""
import re

_NAME_NUMBER = re.compile(r"^\s*(?:\[\s*(\d+)\s*\]|(?:ref(?:erence)?[\s_\-]*)?(\d+))(?=\D|$)", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "by", "from", "via", "is", "are"}


def _words(text):
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def number_from_filename(name):
    """The reference number a file name gives, or None."""
    m = _NAME_NUMBER.match(name or "")
    return (m.group(1) or m.group(2)) if m else None


def _title_score(title, head_words):
    """Share of the title's words found, in order, as one run in the file's opening text."""
    t = _words(title)
    if len(t) < 3:
        return 0.0
    n, k = len(head_words), len(t)
    best = 0
    for i in range(n):
        if head_words[i] != t[0]:
            continue
        run = 0
        while run < k and i + run < n and head_words[i + run] == t[run]:
            run += 1
        best = max(best, run)
    return best / k


_AUTHORS = re.compile(r"\bet al\b|^\s*(\[\d+\]|\d+\.)?\s*([A-Z][\w'\-]+,?\s+([A-Z]\.\s*)+,?\s*(and\s+)?)+$")
_VENUE = re.compile(r"\b(19|20)\d{2}\b|\b(proceedings|conference|journal|arxiv|vol\.?|pp\.?)\b", re.I)


def candidate_titles(info):
    """Titles to try for one bibliography entry: the parsed title, and the first sentence-like
    piece of the raw entry that is neither the author list nor the venue/year."""
    out = [info.get("title", "")]
    raw = re.sub(r"^\s*\[\d+\]\s*", "", info.get("raw", "") or "")
    for seg in re.split(r"\.\s+", raw):
        if _AUTHORS.search(seg) or _VENUE.search(seg) or len(_words(seg)) < 3:
            continue
        out.append(seg)
        break
    return [t for t in out if t]


def match_uploaded_references(parsed_refs, uploads, head_chars=4000, min_score=0.8):
    """Map bibliography keys to uploaded text.

    parsed_refs: {key: {"title": ..., ...}} from citation_resolver.resolve_references.
    uploads: list of (filename, text).
    Returns (matched {key: text}, matched_names {key: filename}, unmatched [filename]).
    """
    matched, names, left = {}, {}, []
    for fname, text in uploads:
        num = number_from_filename(fname)
        if num and num in parsed_refs and num not in matched and text:
            matched[num], names[num] = text, fname
        else:
            left.append((fname, text))
    unmatched = []
    for fname, text in left:
        head = _words((text or "")[:head_chars])
        scores = sorted(((max(_title_score(t, head) for t in candidate_titles(info) or [""]), key)
                         for key, info in parsed_refs.items() if key not in matched), reverse=True)
        top = scores[0] if scores else (0.0, None)
        second = scores[1][0] if len(scores) > 1 else 0.0
        if text and top[0] >= min_score and top[0] > second:
            matched[top[1]], names[top[1]] = text, fname
        else:
            unmatched.append(fname)
    return matched, names, unmatched
