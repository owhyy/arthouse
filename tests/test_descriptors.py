from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from arthouse.descriptors import (
    canonical,
    facet_for,
    find_item,
    load_matrix,
    neighbors,
    rebuild_descriptors,
)
from arthouse.library import Descriptor, ItemDescriptor, ItemTag

FILM_A = "https://boxd.it/aaa"
FILM_B = "https://boxd.it/bbb"


def _add_tags(engine: Engine, source_id: str, tags: list[str], origin: str = "keyword") -> None:
    with Session(engine) as db:
        for tag in tags:
            db.add(
                ItemTag(
                    source="letterboxd",
                    source_id=source_id,
                    provider="tmdb",
                    origin=origin,
                    tag=tag,
                )
            )
        db.commit()


def _count(engine: Engine, model: type[Descriptor] | type[ItemDescriptor]) -> int:
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(model)) or 0


def test_canonical_normalizes_variants() -> None:
    assert canonical("Artificial Intelligence (A.I.)") == "artificial intelligence"
    assert canonical("Paris, France") == "paris"
    assert canonical("  Based  on Novel ") == "based on novel or book"
    assert canonical("Sci-Fi") == "science fiction"
    assert canonical("Neo-Noir") == "neo-noir"


def test_facet_for_classifies_and_drops_trivia() -> None:
    assert facet_for("Based on Novel or Book") == "production"
    assert facet_for("Woman Director") == "production"
    assert facet_for("1970s") == "setting"
    assert facet_for("19th century") == "setting"
    assert facet_for("Paris, France") == "setting"
    assert facet_for("Melancholy") == "mood"
    assert facet_for("Neo-Noir") == "style"
    assert facet_for("Class Differences") == "theme"
    assert facet_for("aftercreditsstinger") is None
    assert facet_for("duringcreditsstinger") is None
    assert facet_for("") is None


def test_film_descriptors_ignore_genres(library_db: Engine) -> None:
    _add_tags(library_db, FILM_A, ["Drama"], origin="genre")
    _add_tags(library_db, FILM_A, ["neo-noir"], origin="keyword")

    stats = rebuild_descriptors(library_db)

    assert stats.tags == 1  # the genre is display-only
    with Session(library_db) as db:
        names = set(db.scalars(select(Descriptor.name)))
    assert names == {"neo-noir"}


def test_descriptors_are_keyed_by_media_type(library_db: Engine) -> None:
    with Session(library_db) as db:
        db.add_all(
            [
                ItemTag(
                    source="rateyourmusic",
                    source_id="1",
                    provider="musicbrainz",
                    origin="release_group",
                    tag="jazz",
                ),
                ItemTag(
                    source="letterboxd",
                    source_id=FILM_A,
                    provider="tmdb",
                    origin="keyword",
                    tag="jazz",
                ),
            ]
        )
        db.commit()

    stats = rebuild_descriptors(library_db)

    assert stats.descriptors == 2  # same name, one row per medium
    with Session(library_db) as db:
        keys = {
            (descriptor.media_type, descriptor.name)
            for descriptor in db.scalars(select(Descriptor))
        }
    assert keys == {("music", "jazz"), ("film", "jazz")}


def test_rebuild_is_idempotent_and_dedupes(library_db: Engine) -> None:
    _add_tags(
        library_db,
        FILM_A,
        ["Neo-Noir", "Melancholy", "Based on Novel", "aftercreditsstinger"],
    )
    _add_tags(
        library_db,
        FILM_B,
        ["neo-noir", "Loneliness", "based on novel or book", "Paris, France"],
    )

    first = rebuild_descriptors(library_db)
    second = rebuild_descriptors(library_db)

    assert first == second
    assert first.tags == 8
    assert first.dropped == 1  # aftercreditsstinger
    assert first.pairs == 7  # 3 for A + 4 for B; both "based on novel" spellings share one name
    assert first.descriptors == 5
    assert first.items == 2
    assert first.facets == {"mood": 1, "production": 1, "setting": 1, "style": 1, "theme": 1}
    assert _count(library_db, Descriptor) == 5
    assert _count(library_db, ItemDescriptor) == 7

    with Session(library_db) as db:
        names = set(db.scalars(select(Descriptor.name)))
        pairs = {
            (name, source_id)
            for name, source_id in db.execute(
                select(ItemDescriptor.name, ItemDescriptor.source_id)
            ).all()
        }
    assert "paris" in names
    assert "aftercreditsstinger" not in names
    assert ("based on novel or book", FILM_A) in pairs
    assert ("based on novel or book", FILM_B) in pairs


def test_load_matrix_drops_rare_descriptors(library_db: Engine) -> None:
    _add_tags(library_db, FILM_A, ["neo-noir", "melancholy"])
    _add_tags(library_db, FILM_B, ["neo-noir"])
    rebuild_descriptors(library_db)

    shared = load_matrix(library_db, min_df=2)
    assert set(shared.idf) == {"neo-noir"}
    assert len(shared.items) == 2

    full = load_matrix(library_db, min_df=1)
    assert set(full.idf) == {"neo-noir", "melancholy"}
    assert len(full.items) == 2


def test_neighbors_rank_shared_descriptors(library_db: Engine) -> None:
    _add_tags(library_db, FILM_A, ["neo-noir", "melancholy"])
    _add_tags(library_db, FILM_B, ["neo-noir", "loneliness"])
    rebuild_descriptors(library_db)
    matrix = load_matrix(library_db, min_df=1)

    results = neighbors(matrix, "Fixture Film A")

    assert len(results) == 1
    assert results[0].item.title == "Fixture Film B"
    assert 0 < results[0].score < 1
    assert find_item(matrix, "fixture film b") is not None
    assert neighbors(matrix, (results[0].item.source, results[0].item.source_id))[0].item.title == (
        "Fixture Film A"
    )
    assert neighbors(matrix, "Unknown Film") == []
