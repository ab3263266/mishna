"""Text-layer tests. No network, no database."""

from __future__ import annotations

import pytest

from app.services.texts import COMMENTATORS, _flatten, sanitize


class FakeTractate:
    def __init__(self, sefaria_title: str, name_en: str) -> None:
        self.sefaria_title = sefaria_title
        self.name_en = name_en


class FakeMishnah:
    def __init__(self, chapter: int, number: int) -> None:
        self.chapter = chapter
        self.number = number


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


@pytest.mark.parametrize(
    "key,expected",
    [
        ("bartenura", "Bartenura on Mishnah Berakhot 1:1"),
        ("rambam", "Rambam on Mishnah Berakhot 1:1"),
        ("tosafot_yom_tov", "Tosafot Yom Tov on Mishnah Berakhot 1:1"),
        ("yachin", "Yachin on Mishnah Berakhot 1:1"),
        ("kulp", "English Explanation of Mishnah Berakhot 1:1"),
    ],
)
def test_commentary_refs_match_sefarias_naming(key: str, expected: str) -> None:
    commentator = next(c for c in COMMENTATORS if c.key == key)
    book = "Mishnah Berakhot"
    assert (
        commentator.ref_template.format(
            book=book, short=book.removeprefix("Mishnah "), ch=1, n=1
        )
        == expected
    )


def test_refs_use_sefarias_spelling_not_ours() -> None:
    """Uktzin is filed as Oktzin. Building refs from our own `name_en` would
    404 every text in the tractate."""
    from app.services.texts import _refs_for

    tractate = FakeTractate(sefaria_title="Mishnah Oktzin", name_en="Uktzin")
    base, commentaries = _refs_for(tractate, FakeMishnah(3, 12))

    assert base == "Mishnah Oktzin 3:12"
    assert commentaries["bartenura"] == "Bartenura on Mishnah Oktzin 3:12"


def test_ref_falls_back_when_sefaria_title_is_missing() -> None:
    from app.services.texts import _refs_for

    tractate = FakeTractate(sefaria_title="", name_en="Berakhot")
    base, _ = _refs_for(tractate, FakeMishnah(1, 1))
    assert base == "Mishnah Berakhot 1:1"


def test_bartenura_is_first_so_the_ui_opens_it_by_default() -> None:
    assert COMMENTATORS[0].key == "bartenura"
