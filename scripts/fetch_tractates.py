"""Build app/data/tractates.json from Sefaria.

Run once (needs network); the generated file is committed so seeding works
offline afterwards. Counts are not the kind of thing to type from memory - a
wrong mishnayot_count silently corrupts every completion estimate.

    .venv/Scripts/python.exe scripts/fetch_tractates.py
"""

from __future__ import annotations

import json
import pathlib
import sys

import httpx

SEDARIM: dict[str, list[str]] = {
    "zeraim": [
        "Berakhot", "Peah", "Demai", "Kilayim", "Sheviit", "Terumot",
        "Maasrot", "Maaser Sheni", "Challah", "Orlah", "Bikkurim",
    ],
    "moed": [
        "Shabbat", "Eruvin", "Pesachim", "Shekalim", "Yoma", "Sukkah",
        "Beitzah", "Rosh Hashanah", "Taanit", "Megillah", "Moed Katan",
        "Chagigah",
    ],
    "nashim": [
        "Yevamot", "Ketubot", "Nedarim", "Nazir", "Sotah", "Gittin",
        "Kiddushin",
    ],
    "nezikin": [
        "Bava Kamma", "Bava Metzia", "Bava Batra", "Sanhedrin", "Makkot",
        "Shevuot", "Eduyot", "Avodah Zarah", "Avot", "Horayot",
    ],
    "kodashim": [
        "Zevachim", "Menachot", "Chullin", "Bekhorot", "Arakhin", "Temurah",
        "Keritot", "Meilah", "Tamid", "Middot", "Kinnim",
    ],
    "taharot": [
        "Kelim", "Oholot", "Negaim", "Parah", "Tahorot", "Mikvaot", "Niddah",
        "Makhshirin", "Zavim", "Tevul Yom", "Yadayim", "Uktzin",
    ],
}

SEDER_HE = {
    "zeraim": "זרעים", "moed": "מועד", "nashim": "נשים",
    "nezikin": "נזיקין", "kodashim": "קדשים", "taharot": "טהרות",
}


def slugify(name: str) -> str:
    return name.lower().replace(" ", "_")


#: Sefaria's spelling differs from ours for a few tractates.
SEFARIA_ALIASES = {"Uktzin": "Oktzin"}


def fetch_shape(client: httpx.Client, title: str) -> list[int] | None:
    """Per-chapter mishnah counts, e.g. Berakhot -> [5, 8, 6, 7, 5, 8, 5, 8, 5].

    The index endpoint only gives totals; the shape endpoint gives the
    breakdown, which is what lets us store real chapter:mishnah addresses
    instead of a bare running number.
    """
    lookup = SEFARIA_ALIASES.get(title, title)
    for candidate in (f"Mishnah_{lookup.replace(' ', '_')}", lookup.replace(" ", "_")):
        response = client.get(f"https://www.sefaria.org/api/shape/{candidate}")
        if response.status_code == 200:
            payload = response.json()
            if payload and isinstance(payload, list):
                chapters = payload[0].get("chapters")
                if isinstance(chapters, list) and all(
                    isinstance(c, int) for c in chapters
                ):
                    return chapters
    return None


def fetch(client: httpx.Client, title: str) -> dict | None:
    lookup = SEFARIA_ALIASES.get(title, title)
    for candidate in (f"Mishnah_{lookup.replace(' ', '_')}", lookup.replace(" ", "_")):
        response = client.get(f"https://www.sefaria.org/api/index/{candidate}")
        if response.status_code == 200:
            return response.json()
    return None


def main() -> int:
    out: list[dict] = []
    order = 0
    problems: list[str] = []

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for seder, tractates in SEDARIM.items():
            for title in tractates:
                data = fetch(client, title)
                if data is None:
                    problems.append(f"{title}: not found")
                    continue

                schema = data.get("schema", {})
                lengths = schema.get("lengths") or []
                # lengths == [chapters, total_mishnayot] for a flat text.
                if len(lengths) < 2:
                    problems.append(f"{title}: unexpected lengths {lengths}")
                    continue

                chapters = fetch_shape(client, title)
                if chapters and sum(chapters) != int(lengths[1]):
                    problems.append(
                        f"{title}: shape sums to {sum(chapters)}, index says {lengths[1]}"
                    )
                    chapters = None

                order += 1
                out.append(
                    {
                        "slug": slugify(title),
                        "name_en": title,
                        "name_he": schema.get("heTitle", title).replace("משנה ", ""),
                        "seder": seder,
                        "seder_he": SEDER_HE[seder],
                        "order_index": order,
                        "chapter_count": int(lengths[0]),
                        "mishnayot_count": int(lengths[1]),
                        "chapter_lengths": chapters,
                    }
                )
                print(f"  {title:<16} {lengths[0]:>3} ch  {lengths[1]:>4} mishnayot")

    target = pathlib.Path(__file__).parent.parent / "app" / "data" / "tractates.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = sum(t["mishnayot_count"] for t in out)
    print(f"\n{len(out)} tractates, {total} mishnayot -> {target}")
    if problems:
        print("\nPROBLEMS (fix before trusting this file):")
        for p in problems:
            print("  -", p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
