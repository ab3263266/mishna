"""Mishnah text and commentaries.

This is the part of the app people actually come for. The scoring exists to
get someone to open it; this is what they read once they have.

Sourcing rules:

* The whole corpus is downloaded once by `scripts/fetch_texts.py` and committed
  under `app/data/texts/` - one gzipped JSON file per tractate. Nothing here
  touches the network: a study screen that depends on Sefaria being reachable
  is a study screen that breaks on a train, during a rate-limit, or on the
  morning Sefaria is down. The texts are centuries old and do not change, so
  there is no invalidation problem to trade against.
* Each passage carries its licence and version through to the UI. Two of the
  commentaries are CC-BY-NC and must be attributed; quietly stripping that is
  not an option.
* A missing commentary is normal, not an error. Not every mishnah has a
  Tosafot Yom Tov, and the screen must degrade to "the ones that exist".
"""

from __future__ import annotations

import gzip
import json
import logging
import pathlib
import re
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

SEFARIA_WEB = "https://www.sefaria.org"

#: One `<slug>.json.gz` per tractate, written by scripts/fetch_texts.py.
DATA_DIR = pathlib.Path(__file__).parent.parent / "data" / "texts"


@dataclass(frozen=True, slots=True)
class Commentator:
    key: str
    name_he: str
    #: Sefaria book title template. `{book}` is the tractate's Sefaria title,
    #: "Mishnah " prefix included. Formatting it gives the `index_title` that
    #: identifies this commentary in Sefaria's link graph, which is how the
    #: fetch script decides what belongs to which mishnah.
    book_template: str
    note: str = ""


#: Ordered by how a daily learner actually uses them: Bartenura first because
#: it is the one people mean by "the commentary", then the Rambam, then the
#: heavier iyun material. The UI opens the first and collapses the rest.
COMMENTATORS: tuple[Commentator, ...] = (
    Commentator("bartenura", "ברטנורא", "Bartenura on {book}"),
    Commentator("rambam", "פירוש הרמב״ם", "Rambam on {book}"),
    Commentator("ikar_tosafot", "עיקר תוספות יום טוב",
                "Ikar Tosafot Yom Tov on {book}"),
    Commentator("tosafot_yom_tov", "תוספות יום טוב",
                "Tosafot Yom Tov on {book}"),
    Commentator("yachin", "תפארת ישראל – יכין", "Yachin on {book}"),
    Commentator("boaz", "תפארת ישראל – בועז", "Boaz on {book}"),
)
COMMENTATORS_BY_KEY = {c.key: c for c in COMMENTATORS}


@dataclass(slots=True)
class Passage:
    key: str
    title: str
    ref: str
    he_ref: str | None
    language: str
    body: str
    license: str | None = None
    version_title: str | None = None
    note: str = ""

    @property
    def sefaria_url(self) -> str:
        return f"{SEFARIA_WEB}/{self.ref.replace(' ', '_')}"


@dataclass(slots=True)
class MishnahView:
    ordinal: int
    chapter: int
    number: int
    tractate_he: str
    ref: str
    he_ref: str | None
    text: Passage | None
    commentaries: list[Passage] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Cleaning Sefaria's markup - used by the fetch script, before anything is
# written to disk. Kept here because what counts as "safe markup" is a property
# of what this app renders, not of the downloader.
# --------------------------------------------------------------------------- #


def _flatten(value) -> str:
    """Sefaria returns a string, or nested lists of strings for multi-segment
    passages. Join them into one HTML blob."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(part for part in (_flatten(v) for v in value) if part)
    return str(value)


_FOOTNOTE = re.compile(r"<sup[^>]*>.*?</sup>\s*<i[^>]*class=\"footnote\".*?</i>", re.S)
_ALLOWED = re.compile(r"</?(?:b|i|em|strong|br|span|big|small)\b[^>]*>", re.I)


def sanitize(html: str) -> str:
    """Keep Sefaria's light inline markup, drop everything else.

    The texts arrive as HTML fragments with `<b>` lemmas that carry real
    meaning - the dibur hamatchil in a commentary is bold for a reason - so
    stripping all tags would lose information. Anything not on the allow-list
    is removed rather than escaped, because it is layout noise, and the result
    is inserted with textContent-safe rendering on the client.
    """
    if not html:
        return ""
    html = _FOOTNOTE.sub("", html)

    # Drop any tag that is not explicitly allowed.
    def keep(match: re.Match) -> str:
        return match.group(0) if _ALLOWED.fullmatch(match.group(0)) else ""

    html = re.sub(r"<[^>]+>", keep, html)
    return re.sub(r"\s+", " ", html).strip()


# --------------------------------------------------------------------------- #
# Reading the corpus
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=8)
def _load(slug: str) -> dict | None:
    """One tractate's texts, parsed and held in memory.

    A tractate is a few hundred kilobytes of JSON, and a learner reads the same
    one for weeks, so the LRU keeps the hot tractate resident and the parse cost
    is paid once per process rather than once per request. Eight slots covers a
    handful of concurrent learners without turning into a memory sink.
    """
    path = DATA_DIR / f"{slug}.json.gz"
    if not path.exists():
        logger.warning("no text file for tractate %s at %s", slug, path)
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.exception("could not read %s", path)
        return None
    # Index by ordinal once, so lookups are a dict hit rather than a scan.
    payload["by_ordinal"] = {m["ordinal"]: m for m in payload["mishnayot"]}
    return payload


def available_tractates() -> set[str]:
    """Slugs that have a text file - what `scripts/fetch_texts.py` produced."""
    if not DATA_DIR.exists():
        return set()
    return {path.name.removesuffix(".json.gz") for path in DATA_DIR.glob("*.json.gz")}


def _passage(
    key: str, ref: str, he_ref: str | None, body: str, source: dict
) -> Passage:
    commentator = COMMENTATORS_BY_KEY.get(key)
    return Passage(
        key=key,
        title=commentator.name_he if commentator else "משנה",
        ref=ref,
        he_ref=he_ref,
        language="he",
        body=body,
        license=source.get("license"),
        version_title=source.get("version"),
        note=commentator.note if commentator else "",
    )


def get_mishnah(
    tractate, ordinal: int, *, with_commentaries: bool = True
) -> MishnahView | None:
    """One mishnah with its commentaries, straight off disk."""
    corpus = _load(tractate.slug)
    if corpus is None:
        return None
    entry = corpus["by_ordinal"].get(ordinal)
    if entry is None:
        return None

    sources = corpus.get("sources", {})
    text_body = entry.get("text") or ""
    text = (
        _passage("mishnah", entry["ref"], entry.get("he_ref"), text_body,
                 sources.get("mishnah", {}))
        if text_body
        else None
    )

    commentaries: list[Passage] = []
    if with_commentaries:
        stored = entry.get("commentaries") or {}
        for commentator in COMMENTATORS:
            body = stored.get(commentator.key)
            # Absent commentaries are normal - not every mishnah has every
            # commentator - so a miss is skipped, never surfaced as an error.
            if not body:
                continue
            source = sources.get(commentator.key, {})
            commentaries.append(
                _passage(
                    commentator.key,
                    commentator.book_template.format(book=corpus["book"])
                    + f" {entry['chapter']}:{entry['number']}",
                    None,
                    body,
                    source,
                )
            )

    return MishnahView(
        ordinal=ordinal,
        chapter=entry["chapter"],
        number=entry["number"],
        tractate_he=tractate.name_he,
        ref=entry["ref"],
        he_ref=entry.get("he_ref"),
        text=text,
        commentaries=commentaries,
    )


def as_dict(view: MishnahView) -> dict:
    def passage(p: Passage | None) -> dict | None:
        if p is None:
            return None
        return {
            "key": p.key,
            "title": p.title,
            "ref": p.ref,
            "he_ref": p.he_ref,
            "language": p.language,
            "body": p.body,
            "license": p.license,
            "version": p.version_title,
            "note": p.note,
            "url": p.sefaria_url,
        }

    return {
        "ordinal": view.ordinal,
        "chapter": view.chapter,
        "number": view.number,
        "tractate": view.tractate_he,
        "ref": view.ref,
        "he_ref": view.he_ref,
        "text": passage(view.text),
        "commentaries": [passage(c) for c in view.commentaries],
    }
