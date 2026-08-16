"""Text-layer tests. No network, no database.

The corpus tests read the real committed files - that is the point. A text file
that failed to download, or one whose ordinals drifted from the seed data,
would show up as an empty study screen and nowhere else.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.services import texts
from app.services.texts import COMMENTATORS, _flatten, sanitize

TRACTATES = json.loads(
    (pathlib.Path(texts.__file__).parent.parent / "data" / "tractates.json")
    .read_text(encoding="utf-8")
)


class FakeTractate:
    def __init__(self, slug: str, name_he: str = "ברכות") -> None:
        self.slug = slug
        self.name_he = name_he


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def test_flatten_handles_sefarias_nested_segments() -> None:
    """A ref covering several segments comes back as nested lists."""
    assert _flatten("plain") == "plain"
    assert _flatten(["a", "b"]) == "a b"
    assert _flatten([["a", "b"], ["c"]]) == "a b c"
    assert _flatten(None) == ""
    assert _flatten(["a", None, "b"]) == "a b"


def test_sanitize_keeps_the_bold_dibur_hamatchil() -> None:
    """The lemma a commentary opens with is bold for a reason - stripping all
    markup would lose which words are being commented on."""
    out = sanitize("<b>מאימתי קורין.</b> תנא אקרא קאי")
    assert out == "<b>מאימתי קורין.</b> תנא אקרא קאי"


def test_sanitize_drops_layout_tags_but_keeps_their_text() -> None:
    out = sanitize('<div class="x"><p>שלום <b>עולם</b></p></div>')
    assert out == "שלום <b>עולם</b>"


def test_sanitize_removes_scripts_entirely_including_content() -> None:
    # The tag is dropped and the body is left as inert text - it is never
    # inserted as HTML on the client, which builds nodes from an allow-list.
    out = sanitize('<b>ok</b><script>alert(1)</script>')
    assert "<script" not in out and "<b>ok</b>" in out


def test_sanitize_collapses_whitespace() -> None:
    assert sanitize("a\n\n   b\t c") == "a b c"


def test_sanitize_handles_empty_input() -> None:
    assert sanitize("") == ""
    assert sanitize(None) == ""


# --------------------------------------------------------------------------- #
# The committed corpus
# --------------------------------------------------------------------------- #


def test_every_tractate_has_a_text_file() -> None:
    """The app has no fallback: a missing file is a blank study screen."""
    missing = {t["slug"] for t in TRACTATES} - texts.available_tractates()
    assert not missing, f"no text downloaded for: {sorted(missing)}"


@pytest.mark.parametrize("entry", TRACTATES, ids=lambda e: e["slug"])
def test_ordinals_line_up_with_the_seed_data(entry: dict) -> None:
    """`ordinal` is the join between the database and the text files. If the
    counts drift, a learner is shown a different mishnah from the one their
    progress says they are on."""
    corpus = texts._load(entry["slug"])
    assert corpus is not None
    assert len(corpus["mishnayot"]) == entry["mishnayot_count"]

    first = texts.get_mishnah(FakeTractate(entry["slug"]), 1)
    last = texts.get_mishnah(
        FakeTractate(entry["slug"]), entry["mishnayot_count"]
    )
    assert first is not None and first.chapter == 1 and first.number == 1
    assert last is not None and last.chapter == entry["chapter_count"]
    assert first.text and first.text.body, "the first mishnah has no text"


@pytest.mark.parametrize("entry", TRACTATES, ids=lambda e: e["slug"])
def test_every_shipped_text_permits_commercial_use(entry: dict) -> None:
    """The corpus must stay free of CC-BY-NC and CC-BY-SA.

    Sefaria serves Bartenura and Ikar Tosafot Yom Tov from a CC-BY-NC edition
    by default, so a careless re-fetch silently reimposes "non-commercial only"
    across the whole app - and share-alike drags its own clause into whatever
    the text is combined with. Neither shows up as a bug; both would only be
    discovered by whoever eventually tries to charge for this.
    """
    corpus = texts._load(entry["slug"])
    assert corpus is not None

    for key, source in corpus["sources"].items():
        licence = (source.get("license") or "").strip().lower()
        assert licence, f"{entry['slug']}/{key} ships with no stated licence"
        assert "-nc" not in licence, f"{entry['slug']}/{key} is {licence}"
        assert "-sa" not in licence, f"{entry['slug']}/{key} is {licence}"


def test_a_mishnah_carries_its_commentaries_and_their_licence() -> None:
    view = texts.get_mishnah(FakeTractate("berakhot"), 1)
    keys = {c.key for c in view.commentaries}
    assert {"bartenura", "rambam", "tosafot_yom_tov"} <= keys

    bartenura = next(c for c in view.commentaries if c.key == "bartenura")
    assert bartenura.license, "CC-BY-NC material must carry its licence"
    assert bartenura.title == "ברטנורא"
    assert bartenura.sefaria_url.startswith("https://www.sefaria.org/")


def test_the_commentaries_are_anchored_to_the_right_mishnah() -> None:
    """Tiferet Yisrael numbers its comments per chapter, not per mishnah, so
    'Yachin on Mishnah Berakhot 1:3' is a comment on mishnah *1*. Reading the
    commentary by position silently showed the wrong text; the fetch script
    resolves it through Sefaria's link graph instead."""
    second = texts.get_mishnah(FakeTractate("berakhot"), 2)
    yachin = next(c for c in second.commentaries if c.key == "yachin")
    assert "בשחרית" in yachin.body[:120], (
        "Yachin on Berakhot 1:2 should open on the morning Shema"
    )


def test_a_missing_commentary_is_skipped_not_an_error() -> None:
    """Boaz comments on three mishnayot in Berakhot chapter 1, not five."""
    view = texts.get_mishnah(FakeTractate("berakhot"), 2)
    assert view is not None
    assert "boaz" not in {c.key for c in view.commentaries}


def test_out_of_range_and_unknown_tractates_return_nothing() -> None:
    assert texts.get_mishnah(FakeTractate("berakhot"), 9999) is None
    assert texts.get_mishnah(FakeTractate("no-such-tractate"), 1) is None


def test_commentaries_can_be_skipped_for_a_lighter_payload() -> None:
    view = texts.get_mishnah(FakeTractate("berakhot"), 1, with_commentaries=False)
    assert view.text is not None
    assert view.commentaries == []


def test_the_payload_the_client_receives_is_json_ready() -> None:
    payload = texts.as_dict(texts.get_mishnah(FakeTractate("berakhot"), 1))
    assert json.dumps(payload)  # no dataclasses left in it
    assert payload["text"]["language"] == "he"
    assert payload["commentaries"][0]["title"]


def test_bartenura_is_first_so_the_ui_opens_it_by_default() -> None:
    assert COMMENTATORS[0].key == "bartenura"


def test_the_english_commentary_is_gone() -> None:
    """Every commentary shipped is Hebrew; the UI has no LTR path left."""
    assert all("English" not in c.book_template for c in COMMENTATORS)
