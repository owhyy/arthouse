"""SQLAlchemy models and the small value objects the UI shares."""

# SQLAlchemy declarative columns are initialized by the ORM's instrumentation,
# which pyright cannot see — the rule is false-positives on this module only.
# pyright: reportUninitializedInstanceVariable=false

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PROVIDER_LABELS = {"tmdb": "TMDB", "musicbrainz": "MusicBrainz", "openlibrary": "OpenLibrary"}


@dataclass(frozen=True)
class GenreRow:
    """One display genre and the provider's vote tally for it."""

    name: str
    count: int


class Base(DeclarativeBase):
    pass


def score_str(rating: float) -> str:
    """Display a 0-5 rating as a 0-10 score.

    x2 maps all three platforms losslessly: RYM 1-10 stays integer,
    Letterboxd half-stars become integers, Goodreads quarter steps become
    halves — so one decimal is always exact and never false precision.
    """
    return f"{rating * 2:g}"


class Item(Base):
    """One rated item from any platform."""

    __tablename__: str = "items"

    __table_args__: tuple[Index, ...] = (
        Index("ix_items_identity", "source", "source_id"),
        Index(
            "ix_items_media_rating_title",
            "media_type",
            text("rating DESC"),
            text("title COLLATE NOCASE"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[str]  # music | film | book
    source: Mapped[str]  # letterboxd | rateyourmusic | goodreads
    source_id: Mapped[str]
    title: Mapped[str]
    creator: Mapped[str]
    year: Mapped[int | None]
    rating: Mapped[float]  # normalized to 0-5
    rating_native: Mapped[str]
    rated_on: Mapped[date | None]
    review: Mapped[str | None]

    @property
    def score(self) -> str:
        return score_str(self.rating)


class ItemMeta(Base):
    """Provider enrichment for one item; keyed by platform id so it survives rebuilds."""

    __tablename__: str = "item_meta"
    __table_args__: tuple[Index, ...] = (Index("ix_item_meta_provider", "provider"),)

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str]  # tmdb | musicbrainz | openlibrary
    external_id: Mapped[str]  # TMDB id, MusicBrainz MBID, OpenLibrary work id
    summary: Mapped[str]  # overview / description
    image_path: Mapped[str | None]  # poster / cover
    # None means "not fetched yet"; "" means the provider lists no credit.
    credit: Mapped[str | None] = mapped_column(default=None)
    # False means the provider's separate tag fetch has not been attempted yet;
    # an empty tag set is a valid result and must not be refetched every run.
    tags_fetched: Mapped[bool] = mapped_column(default=False)
    matched_title: Mapped[str]
    matched_year: Mapped[int | None]
    enriched_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)


class ItemMiss(Base):
    """A provider lookup that found no match.

    Matching is strict, so a miss is a real answer and not something a rerun
    would fix. Recording it keeps the item out of the pending queue instead of
    spending requests on it every run; delete the row to try it again.
    When we implement other API sources for our metadata, these items should be refetched.
    """

    __tablename__: str = "item_miss"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ItemGenre(Base):
    """One display genre for an item.

    Genres are kept apart from descriptor tags because they are shelf data,
    not vocabulary input. `count` is the provider's vote tally, which is what
    ranks the genres and decides the primary/secondary split; providers without
    votes (TMDB) leave it at zero. `artist_level` marks the fallback: the work
    itself had no genres, so the artist's were used and the UI may warn that
    they are approximate.
    """

    __tablename__: str = "item_genre"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    genre: Mapped[str] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    artist_level: Mapped[bool] = mapped_column(default=False)


class ItemTag(Base):
    """One raw descriptor label from an enrichment provider.

    Genres live in `item_genre`; this table holds only labels that the closed
    vocabulary may map (TMDB keywords, MusicBrainz tags, OpenLibrary subjects).
    The provider's own word for them is deliberately not stored. Raw tags stay
    the source of truth for the derived descriptors.
    """

    __tablename__: str = "item_tag"

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    tag: Mapped[str] = mapped_column(primary_key=True)


class ItemDescriptor(Base):
    """One descriptor from the closed vocabulary attached to an item.

    Only the item identity and the descriptor name are stored: the facet lives
    in `arthouse.vocab`, so there is no second copy to drift out of sync.
    """

    __tablename__: str = "item_descriptor"
    # The filter and the option counts look descriptors up by name; the primary
    # key leads with the item identity, so name alone would be a scan.
    __table_args__: tuple[Index, ...] = (Index("ix_item_descriptor_name", "name"),)

    source: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
