"""Normalize TMDB keywords into faceted descriptors and compute content kNN.

Run with: uv run python -m arthouse.descriptors [--title TITLE]... [--min-df N]

The descriptor tables are derived data: `rebuild_descriptors` replaces them
from `film_keyword` every run, so raw keywords stay the source of truth.
"""

import argparse
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session

from arthouse.config import get_settings
from arthouse.library import (
    Base,
    Descriptor,
    FilmKeyword,
    Item,
    ItemDescriptor,
    normalize_title,
)

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")
_ERA = re.compile(r"^\d{4}s$")
_CENTURY = re.compile(r"^\d{1,2}(?:st|nd|rd|th) century$")

# Small, explicit map for variants TMDB ships as separate keywords. Anything
# broader risks merging distinct ideas; the facet default absorbs the rest.
_SYNONYMS = {
    "based on novel": "based on novel or book",
    "sci-fi": "science fiction",
    "sci fi": "science fiction",
    "paris france": "paris",
    "los angeles california": "los angeles",
    "seoul south korea": "south korea",
}

# Production trivia: how the film was released, not what it is about.
_TRIVIA = {"aftercreditsstinger", "duringcreditsstinger"}

_PRODUCTION = {
    "3d",
    "anthology",
    "black and white",
    "blockbuster",
    "cult film",
    "female director",
    "imax",
    "independent film",
    "low budget film",
    "prequel",
    "reboot",
    "remake",
    "semi autobiographical",
    "sequel",
    "short film",
    "silent film",
    "tv movie",
    "vistavision",
    "woman director",
}
_PRODUCTION_PREFIXES = ("based on ",)

_SETTING = {
    # where
    "africa",
    "beach",
    "boston",
    "brooklyn",
    "california",
    "castle",
    "chicago",
    "countryside",
    "desert",
    "dystopia",
    "england",
    "florida",
    "forest",
    "france",
    "future",
    "ghetto",
    "great britain",
    "helsinki",
    "high school",
    "hollywood",
    "hong kong",
    "hospital",
    "hotel",
    "india",
    "island",
    "italy",
    "japan",
    "las vegas",
    "london",
    "long island",
    "los angeles",
    "manhattan",
    "mars",
    "massachusetts",
    "mansion",
    "moon",
    "moscow",
    "new england",
    "new york city",
    "ocean",
    "office",
    "oslo",
    "paris",
    "philadelphia",
    "post-apocalyptic future",
    "prison",
    "restaurant",
    "san francisco",
    "school",
    "seoul",
    "small town",
    "south korea",
    "space",
    "texas",
    "tokyo",
    "underground",
    "wall street",
    # when
    "ancient rome",
    "cold war",
    "great depression",
    "medieval",
    "post world war ii",
    "victorian era",
    "vietnam war",
    "world war i",
    "world war ii",
}

_MOOD = {
    "absurd",
    "admiring",
    "adoring",
    "ambiguous",
    "amused",
    "angry",
    "antagonistic",
    "anxious",
    "apathetic",
    "audacious",
    "awestruck",
    "bewildered",
    "bitter",
    "bold",
    "callous",
    "cautionary",
    "cheerful",
    "commanding",
    "compassionate",
    "complicated",
    "complex",
    "critical",
    "depressing",
    "desperate",
    "disgusted",
    "disturbed",
    "dramatic",
    "dreary",
    "empathetic",
    "euphoric",
    "excited",
    "exhilarated",
    "frantic",
    "frightened",
    "grim",
    "inspirational",
    "intense",
    "introspective",
    "irreverent",
    "lighthearted",
    "meditative",
    "melancholy",
    "melodramatic",
    "paranoid",
    "philosophical",
    "playful",
    "provocative",
    "questioning",
    "reflective",
    "romantic",
    "sad",
    "sentimental",
    "somber",
    "surreal",
    "suspenseful",
    "tense",
    "thoughtful",
    "tragic",
    "vibrant",
    "wistful",
    "witty",
}

_STYLE = {
    "3d animation",
    "absurdism",
    "adult animation",
    "allegory",
    "animation",
    "anime",
    "avant garde",
    "body horror",
    "character study",
    "cyberpunk",
    "dark comedy",
    "documentary",
    "film noir",
    "found footage",
    "french noir",
    "gore",
    "grimdark",
    "historical drama",
    "historical fiction",
    "horror",
    "jidaigeki",
    "magic realism",
    "mockumentary",
    "musical",
    "neo-noir",
    "nonlinear timeline",
    "parody",
    "period drama",
    "period piece",
    "psychological drama",
    "psychological horror",
    "psychological thriller",
    "romcom",
    "satire",
    "stop motion",
    "supernatural horror",
    "surrealism",
    "symbolism",
    "told in flashback",
    "voiceover",
    "whodunit",
}


def canonical(name: str) -> str:
    """Lowercase, drop parentheticals, collapse punctuation and whitespace."""
    text = _PARENTHETICAL.sub("", name.lower())
    text = text.replace(",", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    return _SYNONYMS.get(text, text)


def facet_for(name: str) -> str | None:
    """Facet for a descriptor, or None when it is production trivia.

    Accepts raw or canonical names; explicit lexicons win over the `theme`
    fallback, and the order is intentional (a place named "1970s" is a setting,
    not a mood).
    """
    text = canonical(name)
    if not text or text in _TRIVIA:
        return None
    if text in _PRODUCTION or text.startswith(_PRODUCTION_PREFIXES):
        return "production"
    if text in _SETTING or _ERA.match(text) or _CENTURY.match(text):
        return "setting"
    if text in _MOOD:
        return "mood"
    if text in _STYLE:
        return "style"
    return "theme"


@dataclass
class DescriptorStats:
    keywords: int
    pairs: int
    dropped: int
    descriptors: int
    films: int
    facets: dict[str, int]


def rebuild_descriptors(engine: Engine) -> DescriptorStats:
    """Replace the derived descriptor tables from the raw TMDB keywords."""
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        rows = db.execute(
            select(FilmKeyword.source, FilmKeyword.source_id, FilmKeyword.keyword)
        ).all()
        canon: dict[str, str] = {}
        facets: dict[str, str | None] = {}
        pairs: set[tuple[str, str, str]] = set()
        dropped = 0
        for source, source_id, keyword in rows:
            name = canon[keyword] if keyword in canon else canonical(keyword)
            canon[keyword] = name
            if name not in facets:
                facets[name] = facet_for(name)
            facet = facets[name]
            if facet is None:
                dropped += 1
                continue
            pairs.add((source, source_id, name))

        vocabulary = {name: facet for name, facet in facets.items() if facet is not None and name}
        db.execute(delete(ItemDescriptor))
        db.execute(delete(Descriptor))
        db.add_all(Descriptor(name=name, facet=facet) for name, facet in sorted(vocabulary.items()))
        db.add_all(
            ItemDescriptor(source=source, source_id=source_id, name=name)
            for source, source_id, name in sorted(pairs)
        )
        db.commit()

    facet_counts = Counter(vocabulary.values())
    return DescriptorStats(
        keywords=len(rows),
        pairs=len(pairs),
        dropped=dropped,
        descriptors=len(vocabulary),
        films=len({(source, source_id) for source, source_id, _ in pairs}),
        facets=dict(facet_counts),
    )


@dataclass
class FacetReport:
    descriptors: int
    films: int
    facets: list[tuple[str, int]]
    top: dict[str, list[tuple[str, int]]]


def facet_report(engine: Engine, top: int = 8) -> FacetReport:
    """Vocabulary size per facet and the most widely shared descriptors."""
    with Session(engine) as db:
        facets = [
            (facet, count)
            for facet, count in db.execute(
                select(Descriptor.facet, func.count())
                .group_by(Descriptor.facet)
                .order_by(func.count().desc(), Descriptor.facet)
            ).all()
        ]
        films = (
            db.scalar(
                select(func.count()).select_from(
                    select(ItemDescriptor.source, ItemDescriptor.source_id).distinct().subquery()
                )
            )
            or 0
        )
        rows = db.execute(
            select(Descriptor.facet, ItemDescriptor.name, func.count())
            .join(Descriptor, Descriptor.name == ItemDescriptor.name)
            .group_by(Descriptor.facet, ItemDescriptor.name)
            .order_by(func.count().desc(), ItemDescriptor.name)
        ).all()
    top_by_facet: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
    for facet, name, count in rows:
        if len(top_by_facet[facet]) < top:
            top_by_facet[facet].append((name, count))
    return FacetReport(
        descriptors=sum(count for _, count in facets),
        films=films,
        facets=facets,
        top=dict(top_by_facet),
    )


@dataclass
class ItemVector:
    source: str
    source_id: str
    title: str
    year: int | None
    weights: dict[str, float]


@dataclass
class DescriptorMatrix:
    """TF-IDF vectors over binary descriptor presence, L2-normalized."""

    items: list[ItemVector]
    idf: dict[str, float]
    by_title: dict[str, list[int]] = field(default_factory=dict)


def load_matrix(engine: Engine, min_df: int = 3) -> DescriptorMatrix:
    """Build the film-by-descriptor matrix, dropping descriptors in < min_df films."""
    with Session(engine) as db:
        rows = db.execute(
            select(ItemDescriptor.source, ItemDescriptor.source_id, ItemDescriptor.name)
        ).all()
        films = {
            (item.source, item.source_id): (item.title, item.year)
            for item in db.scalars(select(Item).where(Item.media_type == "film"))
        }

    by_item: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for source, source_id, name in rows:
        by_item[(source, source_id)].add(name)

    document_frequency: Counter[str] = Counter()
    for names in by_item.values():
        document_frequency.update(names)
    kept = {name for name, count in document_frequency.items() if count >= min_df}
    item_names = {key: names & kept for key, names in by_item.items() if names & kept}

    # Recompute df over the surviving items so idf matches the matrix.
    document_frequency = Counter()
    for names in item_names.values():
        document_frequency.update(names)
    n = len(item_names)
    idf = {name: math.log((1 + n) / (1 + count)) + 1 for name, count in document_frequency.items()}

    items: list[ItemVector] = []
    by_title: defaultdict[str, list[int]] = defaultdict(list)
    for key in sorted(item_names):
        source, source_id = key
        title, year = films.get(key, (source_id, None))
        weights = {name: idf[name] for name in item_names[key]}
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        items.append(
            ItemVector(
                source=source,
                source_id=source_id,
                title=title,
                year=year,
                weights={name: weight / norm for name, weight in weights.items()},
            )
        )
        by_title[normalize_title(title)].append(len(items) - 1)
    return DescriptorMatrix(items=items, idf=idf, by_title=dict(by_title))


def _resolve(matrix: DescriptorMatrix, key: str | tuple[str, str]) -> int | None:
    if isinstance(key, tuple):
        for index, item in enumerate(matrix.items):
            if (item.source, item.source_id) == key:
                return index
        return None
    indices = matrix.by_title.get(normalize_title(key))
    return indices[0] if indices else None


def find_item(matrix: DescriptorMatrix, key: str | tuple[str, str]) -> ItemVector | None:
    """Resolve a title (first match) or a (source, source_id) key."""
    index = _resolve(matrix, key)
    return matrix.items[index] if index is not None else None


@dataclass
class Neighbor:
    item: ItemVector
    score: float


def neighbors(matrix: DescriptorMatrix, key: str | tuple[str, str], k: int = 10) -> list[Neighbor]:
    """Cosine neighbours of one film; [] when the film is unknown or isolated."""
    index = _resolve(matrix, key)
    if index is None:
        return []
    target = matrix.items[index]
    scored: list[Neighbor] = []
    for other_index, item in enumerate(matrix.items):
        if other_index == index:
            continue
        score = sum(weight * item.weights.get(name, 0.0) for name, weight in target.weights.items())
        if score > 0:
            scored.append(Neighbor(item=item, score=score))
    scored.sort(key=lambda neighbor: (-neighbor.score, neighbor.item.title.casefold()))
    return scored[:k]


def _label(item: ItemVector) -> str:
    return f"{item.title} ({item.year})" if item.year else item.title


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild faceted descriptors and print content neighbours."
    )
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="film title to find neighbours for (repeatable)",
    )
    parser.add_argument("--min-df", type=int, default=3, help="minimum films sharing a descriptor")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.data_dir / 'library.db'}")
    stats = rebuild_descriptors(engine)
    print(
        f"descriptors: {stats.keywords} keywords -> {stats.pairs} pairs "
        + f"({stats.dropped} trivia rows dropped), {stats.descriptors} distinct, "
        + f"{stats.films} films"
    )

    report = facet_report(engine)
    print("facets: " + ", ".join(f"{facet} {count}" for facet, count in report.facets))
    for facet, _ in report.facets:
        top = ", ".join(f"{name} ({count})" for name, count in report.top.get(facet, []))
        print(f"  {facet}: {top}")

    matrix = load_matrix(engine, min_df=cast(int, args.min_df))
    print(
        f"matrix: {len(matrix.items)} films, {len(matrix.idf)} descriptors "
        + f"(min_df={cast(int, args.min_df)})"
    )
    for title in cast(list[str], args.title):
        target = find_item(matrix, title)
        if target is None:
            print(f"\n{title}: not in the descriptor matrix")
            continue
        print(f"\n{_label(target)}")
        results = neighbors(matrix, (target.source, target.source_id))
        if not results:
            print("  no shared descriptors")
        for result in results:
            print(f"  {result.score:.3f}  {_label(result.item)}")


if __name__ == "__main__":
    main()
