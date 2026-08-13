"""Mishnah text and commentaries, from Sefaria.

This is the part of the app people actually come for. The scoring exists to
get someone to open it; this is what they read once they have.

Sourcing rules:

* Everything is fetched from Sefaria's public API and cached permanently in
  `text_cache`. The texts do not change, so there is no invalidation - the
  cache exists so the study screen renders when Sefaria is slow, rate-limiting
  or unreachable.
* Each passage carries its licence and version through to the UI. Two of the
  commentaries are CC-BY-NC and must be attributed; quietly stripping that is
  not an option.
* A missing commentary is normal, not an error. Not every mishnah has a
  Tosafot Yom Tov, and the screen must degrade to "the ones that exist".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Mishnah, TextCache, Tractate

logger = logging.getLogger(__name__)

SEFARIA_API = "https://www.sefaria.org/api/v3/texts"
SEFARIA_WEB = "https://www.sefaria.org"
TIMEOUT = httpx.Timeout(12.0, connect=5.0)


@dataclass(frozen=True, slots=True)
class Commentator:
    key: str
    name_he: str
    #: Sefaria ref template. `{book}` is the tractate's Sefaria title with the
    #: "Mishnah " prefix already included where the commentary expects it.
    ref_template: str
    language: str = "he"
    note: str = ""


#: Ordered by how a daily learner actually uses them: Bartenura first because
#: it is the one people mean by "the commentary", then the Rambam, then the
#: heavier iyun material. The UI opens the first and collapses the rest.
COMMENTATORS: tuple[Commentator, ...] = (
    Commentator("bartenura", "ברטנורא", "Bartenura on {book} {ch}:{n}"),
    Commentator("rambam", "פירוש הרמב״ם", "Rambam on {book} {ch}:{n}"),
    Commentator(
        "ikar_tosafot", "עיקר תוספות יום טוב",
        "Ikar Tosafot Yom Tov on {book} {ch}:{n}",
    ),
    Commentator(
        "tosafot_yom_tov", "תוספות יום טוב",
        "Tosafot Yom Tov on {book} {ch}:{n}",
    ),
    Commentator("yachin", "תפארת ישראל – יכין", "Yachin on {book} {ch}:{n}"),
    Commentator("boaz", "תפארת ישראל – בועז", "Boaz on {book} {ch}:{n}"),
    Commentator(
        "kulp", "English explanation", "English Explanation of {book} {ch}:{n}",
        language="en", note="Dr. Joshua Kulp, Mishnah Yomit",
    ),
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
# Fetching
# --------------------------------------------------------------------------- #


def _flatten(value) -> str:
    """Sefaria returns a string, or nested lists of strings for multi-segment
    passages. Join them into one HTML blob."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value if v)
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


def _fetch_from_sefaria(ref: str) -> dict | None:
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{SEFARIA_API}/{ref}")
    except httpx.HTTPError as exc:
        logger.warning("sefaria unreachable for %s: %s", ref, exc)
        return None

    if response.status_code != 200:
        logger.info("sefaria %s for %s", response.status_code, ref)
        return None
    return response.json()


def load_passage(
    session: Session, ref: str, kind: str, *, allow_network: bool = True
) -> Passage | None:
    """Cache-first read of a single Sefaria ref."""
    cached = session.get(TextCache, (ref, kind))
    if cached is not None:
        return _passage_from_cache(cached, kind)

    if not allow_network:
        return None

    payload = _fetch_from_sefaria(ref)
    if payload is None:
        return None

    versions = payload.get("versions") or []
    if not versions:
        return None
    version = versions[0]
    body = sanitize(_flatten(version.get("text")))
    if not body:
        return None

    row = TextCache(
        ref=ref,
        kind=kind,
        he_ref=payload.get("heRef"),
        language=version.get("language") or "he",
        body=body,
        license=version.get("license"),
        version_title=version.get("versionTitle"),
    )
    session.merge(row)
    session.flush()
    return _passage_from_cache(row, kind)


def _passage_from_cache(row: TextCache, kind: str) -> Passage:
    commentator = COMMENTATORS_BY_KEY.get(kind)
    return Passage(
        key=kind,
        title=commentator.name_he if commentator else "משנה",
        ref=row.ref,
        he_ref=row.he_ref,
        language=row.language,
        body=row.body,
        license=row.license,
        version_title=row.version_title,
        note=commentator.note if commentator else "",
    )


# --------------------------------------------------------------------------- #
# Assembling a mishnah
# --------------------------------------------------------------------------- #


def _refs_for(tractate: Tractate, mishnah: Mishnah) -> tuple[str, dict[str, str]]:
    book = tractate.sefaria_title or f"Mishnah {tractate.name_en}"
    # Tiferet Yisrael is filed under the bare tractate name, not "Mishnah X".
    short = book.removeprefix("Mishnah ")
    base = f"{book} {mishnah.chapter}:{mishnah.number}"
    commentary_refs = {
        c.key: c.ref_template.format(book=book, short=short,
                                     ch=mishnah.chapter, n=mishnah.number)
        for c in COMMENTATORS
    }
    return base, commentary_refs


def get_mishnah(
    session: Session,
    tractate: Tractate,
    ordinal: int,
    *,
    with_commentaries: bool = True,
    allow_network: bool = True,
) -> MishnahView | None:
    mishnah = session.execute(
        select(Mishnah).where(
            Mishnah.tractate_id == tractate.id, Mishnah.ordinal == ordinal
        )
    ).scalar_one_or_none()
    if mishnah is None:
        return None

    base_ref, commentary_refs = _refs_for(tractate, mishnah)
    text = load_passage(session, base_ref, "mishnah", allow_network=allow_network)

    commentaries: list[Passage] = []
    if with_commentaries:
        for commentator in COMMENTATORS:
            passage = load_passage(
                session,
                commentary_refs[commentator.key],
                commentator.key,
                allow_network=allow_network,
            )
            # Absent commentaries are normal - not every mishnah has every
            # commentator - so a miss is skipped, never surfaced as an error.
            if passage is not None:
                commentaries.append(passage)

    return MishnahView(
        ordinal=ordinal,
        chapter=mishnah.chapter,
        number=mishnah.number,
        tractate_he=tractate.name_he,
        ref=base_ref,
        he_ref=text.he_ref if text else None,
        text=text,
        commentaries=commentaries,
    )


def prefetch(session: Session, tractate: Tractate, ordinals: list[int]) -> int:
    """Warm the cache for upcoming mishnayot.

    Called after a study session so tomorrow's text is already local. Failures
    are swallowed: a cold cache is a slow screen, not a broken one.
    """
    warmed = 0
    for ordinal in ordinals:
        try:
            if get_mishnah(session, tractate, ordinal) is not None:
                warmed += 1
        except Exception:  # pragma: no cover - best effort by design
            logger.exception("prefetch failed for %s %s", tractate.slug, ordinal)
    return warmed


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
