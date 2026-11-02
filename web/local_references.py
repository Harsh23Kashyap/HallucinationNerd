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
import unicodedata

_NAME_NUMBER = re.compile(r"^\s*(?:\[\s*(\d+)\s*\]|(?:ref(?:erence)?[\s_\-]*)?(\d+))(?=\D|$)", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "by", "from", "via", "is", "are"}


def _words(text):
    # NFKC turns PDF ligatures ("ﬁ" in "Eﬃcient") back into plain letters before comparing
    text = unicodedata.normalize("NFKC", text or "")
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


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


def match_uploaded_references(parsed_refs, uploads, head_chars=4000):
    """Map bibliography keys to uploaded text.

    parsed_refs: {key: {"title": ..., ...}} from citation_resolver.resolve_references.
    uploads: list of (filename, text).
    Returns (matched {key: text}, matched_names {key: filename}, unmatched [filename]).
    """
    # Resolve proposals before assigning any file: duplicates must not be decided by
    # upload order, and already-used references must still participate in ambiguity checks.
    proposals = []
    for fname, text in uploads:
        key = None
        num = number_from_filename(fname)
        if num:
            if num in parsed_refs and text:
                key = num
        elif text:
            head = _words(text[:head_chars])
            candidates = [k for k, info in parsed_refs.items()
                          if any(_title_score(t, head) == 1.0
                                 for t in candidate_titles(info))]
            if len(candidates) == 1:
                key = candidates[0]
        proposals.append((fname, text, key))
    counts = {}
    for _, _, key in proposals:
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    matched, names, unmatched = {}, {}, []
    for fname, text, key in proposals:
        if key is not None and counts[key] == 1:
            matched[key], names[key] = text, fname
        else:
            unmatched.append(fname)
    return matched, names, unmatched
