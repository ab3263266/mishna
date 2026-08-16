"""Download the whole corpus from Sefaria into app/data/texts/.

Run once (needs network); the generated files are committed, and from then on
the app never talks to Sefaria. 4,192 mishnayot with six commentaries is about
40 MB of text, which gzips to a quarter of that - small enough to ship, and the
alternative (a live API call on the study screen) fails exactly when a learner
is on a train.

    .venv/Scripts/python.exe scripts/fetch_texts.py            # everything
    .venv/Scripts/python.exe scripts/fetch_texts.py berakhot   # one tractate

Three things about Sefaria's data shape drive the code below, and each one was
a silently wrong or missing text before it was handled:

1. **A commentary's numbering is its own, not the mishnah's.** Tiferet Yisrael
   is the clear case: "Yachin on Mishnah Berakhot 1:13" is the *thirteenth
   comment in chapter 1*, which lands on mishnah 2 - so reading `Yachin 1:3` as
   "the comment on mishnah 3" shows the wrong text with no error. The link
   graph (`anchorRef` -> `ref`) carries the real anchoring, so that is what
   decides which segments belong to which mishnah.

2. **No single edition is complete.** Mishnah Bikkurim's primary version is the
   one printed with the Gemara and holds only chapter 4; Torat Emet holds
   chapters 1-3 and stops. Yachin on Chullin's primary version covers one
   chapter of twelve. So editions are layered: the primary first, then other
   Hebrew editions, each filling only the gaps left by the ones before it.

3. **The largest books time out.** A whole-book request for Tosafot Yom Tov on
   Chullin returns 504 however long you wait, so a book that fails whole is
   re-fetched a chapter at a time.

4. **The primary edition is not the freest one.** Sefaria serves Bartenura and
   Ikar Tosafot Yom Tov from a CC-BY-NC edition by default, with a Public
   Domain edition of the same commentary sitting right behind it. Taking the
   default imported a non-commercial restriction across the whole corpus for
   nothing, so editions are now chosen by licence - see MAX_LICENCE_RANK.
"""

from __future__ import annotations

import gzip
import itertools
import json
import os
import pathlib
import re
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import quote

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.services.texts import COMMENTATORS, _flatten, sanitize  # noqa: E402

API = "https://www.sefaria.org/api"
TIMEOUT = httpx.Timeout(120.0, connect=15.0)
#: Politeness delay between requests. The whole run is ~1,000 calls against a
#: free public API that a lot of other people are also using.
PAUSE = 0.25
#: How many alternate editions to try when the primary leaves gaps. Some gaps
#: are real - a commentator who simply did not comment there - and without a
#: cap those would send us through every edition Sefaria holds.
MAX_ALTERNATES = 4

#: How restrictive a licence may be before the text is left out.
#:
#: 1 - Public Domain, CC0 and CC-BY only. The default, and the reason for all
#:     of this: a CC-BY-NC text quietly forbids ever charging for the service,
#:     selling it to a school, or putting an advert beside it, and CC-BY-SA
#:     drags its share-alike clause into whatever it is combined with. Neither
#:     is a decision worth inheriting from whichever edition Sefaria happens
#:     to serve first.
#: 2 - also accept CC-BY-SA (commercial use fine, derivatives must match).
#: 3 - accept anything, CC-BY-NC included. The fullest text, the fewest rights.
MAX_LICENCE_RANK = int(os.environ.get("MAX_LICENCE_RANK", "1"))

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "data"
OUT_DIR = DATA_DIR / "texts"

_ADDRESS = re.compile(r"^(\d+)(?:-(\d+))?$")

#: Distinguishes "the server would not answer" from "there is no such book".
#: They need opposite handling: one is worth retrying a different way, the
#: other means a commentary that does not exist, which is normal.
FAILED = object()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def get(client: httpx.Client, path: str, params: dict | None = None):
    """One GET with retries. None for a genuine 404, FAILED if it never came."""
    for attempt in range(4):
        try:
            response = client.get(f"{API}{path}", params=params)
        except httpx.HTTPError as exc:
            print(f"    ! {path}: {exc}")
            time.sleep(3 * (attempt + 1))
            continue
        if response.status_code == 200:
            time.sleep(PAUSE)
            return response.json()
        if response.status_code == 404:
            return None
        print(f"    ! {path}: HTTP {response.status_code}")
        time.sleep(3 * (attempt + 1))
    return FAILED


def ref_path(ref: str) -> str:
    return quote(ref.replace(" ", "_"))


def _edition(payload, chapters_fetched: list | None = None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    versions = payload.get("versions") or []
    if not versions:
        return None
    version = versions[0]
    return {
        "text": chapters_fetched if chapters_fetched is not None
        else version.get("text"),
        "license": version.get("license"),
        "version": version.get("versionTitle"),
        "he_title": payload.get("heTitle") or "",
        "available": [
            {"title": v.get("versionTitle"), "license": v.get("license")}
            for v in payload.get("available_versions") or []
            if v.get("language") == "he" and v.get("versionTitle")
        ],
    }


def fetch_edition(
    client: httpx.Client, title: str, version: str, chapters: int
) -> dict | None:
    """One edition of one book, as nested lists.

    `version=source` asks for the original-language text; without it the
    default can be a translation, and a Hebrew study app that quietly serves
    English is worse than one that serves nothing. `hebrew|<title>` asks for a
    named edition.
    """
    payload = get(client, f"/v3/texts/{ref_path(title)}", {"version": version})
    if payload is None:
        return None  # no such book - a missing commentary is normal
    if payload is not FAILED:
        edition = _edition(payload)
        if edition is not None:
            return edition

    # The book was too big for the gateway. Assemble it a chapter at a time.
    parts: list = []
    header = None
    for chapter in range(1, chapters + 1):
        piece = get(
            client, f"/v3/texts/{ref_path(f'{title}.{chapter}')}", {"version": version}
        )
        if not isinstance(piece, dict) or not (piece.get("versions") or []):
            parts.append([])
            continue
        header = header or piece
        parts.append(piece["versions"][0].get("text") or [])
    if header is None:
        return None
    print(f"    ~ {title}: assembled from {sum(1 for p in parts if p)} chapters")
    return _edition(header, chapters_fetched=parts)


def licence_rank(licence: str | None) -> int:
    """Lower is freer. Sefaria serves whichever edition it considers primary,
    and for Bartenura and Ikar Tosafot Yom Tov that happens to be a CC-BY-NC
    one - while a Public Domain edition of the same commentary sits right
    behind it. Taking the default therefore imported a non-commercial
    restriction over the whole corpus for no reason at all."""
    value = (licence or "").strip().lower()
    if value.startswith("public domain") or value == "cc0":
        return 0
    if value.startswith("cc-by-sa"):          # commercial ok, but viral
        return 2
    if value.startswith("cc-by"):             # not -nc, checked after -sa above
        return 1 if "-nc" not in value else 3
    return 3                                  # CC-BY-NC, unknown, anything else


def licence_allowed(licence: str | None) -> bool:
    return licence_rank(licence) <= MAX_LICENCE_RANK


_NIKUD = re.compile(r"[֑-ׇ]")


def vowel_count(edition: dict) -> int:
    """How pointed an edition is, from a sample. Used only to break a tie
    between editions whose licences are equally free."""
    sample = json.dumps(edition.get("text"), ensure_ascii=False)[:40000]
    return len(_NIKUD.findall(sample))


def fetch_segments(
    client: httpx.Client, title: str, needed: set[tuple[int, ...]], chapters: int
) -> tuple[dict[tuple[int, ...], str], dict, dict | None]:
    """{address: body} for as many of `needed` as any usable Hebrew edition holds.

    Editions are tried freest-licence first, and restricted ones are skipped
    entirely unless ALLOW_RESTRICTED is set. A gap is recoverable - a licence
    that forbids the thing you want to do with the corpus is not.
    """
    if not needed:
        return {}, {}, None
    probe = fetch_edition(client, title, "source", chapters)
    if probe is None:
        return {}, {}, None

    # The probe's own edition, plus every other Hebrew one, ordered by licence.
    candidates = [{"title": probe["version"], "license": probe["license"]}]
    for other in probe["available"]:
        if other["title"] != probe["version"]:
            candidates.append(other)
    candidates.sort(key=lambda c: licence_rank(c["license"]))
    fetched: dict[str, dict] = {probe["version"]: probe}

    # Among editions with equally free licences, prefer the vocalised one.
    # Sefaria carries a pointed Public Domain "ToratEmet" of Bartenura and Ikar
    # Tosafot Yom Tov for Avot - the most-studied tractate of the lot - beside
    # an unpointed "On Your Way", and licence alone cannot tell them apart.
    best = licence_rank(candidates[0]["license"])
    head = [c for c in candidates if licence_rank(c["license"]) == best]
    if len(head) > 1 and licence_allowed(candidates[0]["license"]):
        scored = []
        for candidate in head[:3]:
            edition = (
                probe if candidate["title"] == probe["version"]
                else fetch_edition(client, title, f"hebrew|{candidate['title']}",
                                   chapters)
            )
            if edition is not None:
                fetched[candidate["title"]] = edition
                scored.append((vowel_count(edition), candidate))
        if scored:
            scored.sort(key=lambda pair: -pair[0])
            if scored[0][0] > 0:
                print(f"    · {title}: preferring vocalised "
                      f"{scored[0][1]['title']!r}")
            reordered = [c for _, c in scored]
            candidates = reordered + [c for c in candidates if c not in reordered]

    found: dict[tuple[int, ...], str] = {}
    used: list[dict] = []
    skipped: list[str] = []

    def absorb(edition: dict) -> int:
        filled = 0
        for address in needed:
            if found.get(address):
                continue
            body = sanitize(_flatten(at(edition["text"], address)))
            if body:
                found[address] = body
                filled += 1
        if filled:
            used.append(edition)
        return filled

    tried = 0
    for candidate in candidates:
        if len(found) >= len(needed) or tried >= MAX_ALTERNATES:
            break
        if not licence_allowed(candidate["license"]):
            skipped.append(f"{candidate['title']} ({candidate['license']})")
            continue
        tried += 1
        edition = fetched.get(candidate["title"])
        if edition is None:
            edition = fetch_edition(
                client, title, f"hebrew|{candidate['title']}", chapters
            )
        if edition is not None and absorb(edition):
            print(f"    + {title}: {len(found)}/{len(needed)} "
                  f"from {candidate['title']!r} ({candidate['license']})")

    if skipped and len(found) < len(needed):
        print(f"    ! {title}: {len(needed) - len(found)} segments missing; "
              f"skipped restricted {', '.join(skipped)}")

    source = {
        "license": " · ".join(dict.fromkeys(e["license"] for e in used if e["license"])),
        "version": " · ".join(dict.fromkeys(e["version"] for e in used if e["version"])),
    }
    return found, source, probe


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def addresses(ref: str, index_title: str | None = None) -> list[tuple[int, ...]]:
    """The numeric address(es) a ref points at, e.g. 'X 1:13:2' -> [(1, 13, 2)].

    Read off the end of the ref rather than by stripping a known prefix. The
    prefix is not reliably known: Avot's commentary links anchor to "Pirkei
    Avot 1:1" while the tractate is "Mishnah Avot", so prefix-matching returned
    nothing and the tractate silently shipped without commentaries.

    Ranged refs ('1:13:1-3') expand to every address they cover, because a
    commentary segment is sometimes filed as one link spanning several pieces.
    """
    tail = ref.rsplit(" ", 1)[-1] if " " in ref else ""
    if not tail:
        return []

    spans: list[range] = []
    for part in tail.split(":"):
        match = _ADDRESS.match(part.strip())
        if match is None:
            return []
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start or end - start > 500:
            return []
        spans.append(range(start, end + 1))
    return [tuple(combo) for combo in itertools.product(*spans)]


def first_address(ref: str, index_title: str | None = None) -> tuple[int, ...]:
    found = addresses(ref, index_title)
    return found[0] if found else ()


def at(nested: object, address: tuple[int, ...]) -> object | None:
    """Walk a 1-based Sefaria address into the nested text arrays."""
    node = nested
    for index in address:
        if not isinstance(node, list) or not 1 <= index <= len(node):
            return None
        node = node[index - 1]
    return node


# --------------------------------------------------------------------------- #
# Assembling one tractate
# --------------------------------------------------------------------------- #


def collect_links(
    client: httpx.Client, book: str, chapters: int
) -> tuple[dict[tuple[int, int], dict[str, list[str]]], dict[str, str]]:
    """({(chapter, mishnah): {key: [refs]}}, {key: index_title}).

    Commentaries are matched on `collectiveTitle`, which is stable, and the
    index title is read back off the link rather than assumed. Building it from
    a template instead ("<name> on Mishnah <tractate>") silently produced
    nothing for Avot, which Sefaria files as "<name> on Pirkei Avot" - so the
    most-studied tractate in the Shas shipped with no commentaries at all.

    `with_text=0` matters: the same response with text attached is six times
    the size and eight times slower, and the commentary books are fetched in
    full anyway.
    """
    by_collective = {c.collective_title: c.key for c in COMMENTATORS}
    anchored: dict[tuple[int, int], dict[str, list[str]]] = {}
    index_titles: dict[str, Counter] = defaultdict(Counter)
    seen: set[tuple[int, int, str, str]] = set()

    for chapter in range(1, chapters + 1):
        links = get(client, f"/links/{ref_path(f'{book}.{chapter}')}",
                    {"with_text": "0"})
        if not isinstance(links, list):
            print(f"    ! no links for {book} chapter {chapter}")
            continue

        for link in links:
            if not isinstance(link, dict) or link.get("category") != "Commentary":
                continue
            key = by_collective.get((link.get("collectiveTitle") or {}).get("en"))
            if key is None:
                continue
            ref = link.get("ref")
            if not ref:
                continue
            if link.get("index_title"):
                index_titles[key][link["index_title"]] += 1

            expanded = link.get("anchorRefExpanded") or [link.get("anchorRef")]
            for anchor in expanded:
                if not anchor:
                    continue
                for address in addresses(anchor, book):
                    if len(address) != 2:
                        continue
                    marker = (*address, key, ref)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    anchored.setdefault(address, {}).setdefault(key, []).append(ref)

    titles = {key: counts.most_common(1)[0][0] for key, counts in index_titles.items()}
    return anchored, titles


def build(client: httpx.Client, entry: dict) -> dict | None:
    book = entry["sefaria_title"]
    chapters = entry["chapter_count"]
    print(f"  {entry['slug']:<16} {book}")

    anchored, titles = collect_links(client, book, chapters)

    # The mishnah itself. Every address is expected to exist; a gap here is a
    # blank study screen, which is why the layering above matters.
    wanted_mishnayot = {
        (chapter, number)
        for chapter, length in enumerate(entry["chapter_lengths"], 1)
        for number in range(1, length + 1)
    }
    bodies, mishnah_source, primary = fetch_segments(
        client, book, wanted_mishnayot, chapters
    )
    if primary is None:
        print(f"    ! could not fetch {book}")
        return None

    # Only the commentary addresses the links actually point at get fetched, so
    # a commentator with nothing to say about this tractate costs no requests.
    wanted_by_key: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    for by_key in anchored.values():
        for key, refs in by_key.items():
            for ref in refs:
                wanted_by_key[key].update(addresses(ref, titles[key]))

    segments: dict[str, dict[tuple[int, ...], str]] = {}
    sources = {"mishnah": mishnah_source}
    for commentator in COMMENTATORS:
        key = commentator.key
        if key not in titles:
            print(f"    - no {key}")
            continue
        found, source, edition = fetch_segments(
            client, titles[key], wanted_by_key.get(key, set()), chapters
        )
        if edition is None or not found:
            print(f"    - no {key}")
            continue
        segments[key] = found
        # The index title travels with the text: the app builds each
        # commentary's Sefaria reference from it, and it is not derivable.
        sources[key] = source | {"index_title": titles[key]}

    he_title = primary["he_title"]
    mishnayot: list[dict] = []
    missing_text = 0
    coverage = dict.fromkeys(segments, 0)
    ordinal = 0

    for chapter, length in enumerate(entry["chapter_lengths"], 1):
        for number in range(1, length + 1):
            ordinal += 1
            body = bodies.get((chapter, number), "")
            if not body:
                missing_text += 1

            commentaries: dict[str, str] = {}
            for key, refs in (anchored.get((chapter, number)) or {}).items():
                if key not in segments:
                    continue
                title = titles[key]
                pieces = [
                    segments[key][address]
                    for ref in sorted(refs, key=lambda r: first_address(r, title))
                    for address in addresses(ref, title)
                    if segments[key].get(address)
                ]
                if pieces:
                    commentaries[key] = " ".join(pieces)
                    coverage[key] += 1

            mishnayot.append(
                {
                    "ordinal": ordinal,
                    "chapter": chapter,
                    "number": number,
                    "ref": f"{book} {chapter}:{number}",
                    "he_ref": f"{he_title} {chapter}:{number}" if he_title else None,
                    "text": body,
                    "commentaries": commentaries,
                }
            )

    if ordinal != entry["mishnayot_count"]:
        print(f"    ! built {ordinal} mishnayot, expected {entry['mishnayot_count']}")
    if missing_text:
        print(f"    ! {missing_text} mishnayot came back empty")
    print("    " + " ".join(f"{key}={count}" for key, count in coverage.items())
          + f"  of {ordinal}")

    return {
        "slug": entry["slug"],
        "book": book,
        "name_he": entry["name_he"],
        "he_title": he_title,
        "sources": sources,
        "mishnayot": mishnayot,
    }


def write(payload: dict) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{payload['slug']}.json.gz"
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # mtime=0 keeps the gzip header byte-identical between runs, so re-fetching
    # an unchanged tractate does not show up as a diff.
    with gzip.GzipFile(target, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(blob)
    return target.stat().st_size


def main() -> int:
    only = set(sys.argv[1:])
    entries = json.loads((DATA_DIR / "tractates.json").read_text(encoding="utf-8"))
    if only:
        entries = [e for e in entries if e["slug"] in only]
        missing = only - {e["slug"] for e in entries}
        if missing:
            print(f"unknown tractate(s): {', '.join(sorted(missing))}")
            return 1

    total_bytes = 0
    failed: list[str] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for index, entry in enumerate(entries, 1):
            print(f"[{index}/{len(entries)}]", end=" ")
            payload = build(client, entry)
            if payload is None:
                failed.append(entry["slug"])
                continue
            total_bytes += write(payload)

    print(f"\n{len(entries) - len(failed)} tractates -> {OUT_DIR}")
    print(f"{total_bytes / 1e6:.1f} MB on disk (gzipped)")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
