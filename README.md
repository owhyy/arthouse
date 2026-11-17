# arthouse

Recommendation system.

## Requirements

- Python 3.15
- `uv` 0.13+

## Setup

```bash
uv sync
uv run arthouse            # http://127.0.0.1:8000
```

## Library data

`/library` renders your RateYourMusic, Letterboxd, and Goodreads exports as a
card grid with one tab per category (Music / Films / Books, each sorted by
rating); tabs swap the grid in place via htmx (plain links without JS). A
search box filters the active tab by item title as you type (300 ms debounce;
the form also works as a plain GET). Every entry links to a detail page with the rating date, the original platform
rating, the platform it came from, and your review when the export has one
(Letterboxd `reviews.csv`, RYM `Review`, Goodreads `My Review`; HTML is
stripped and paragraphs preserved). Enriched items also show provider genres on
cards and the detail page. MusicBrainz genres carry a vote tally, so they are
ranked by it: the detail page marks the best-voted ones as primary and puts the
rest under "More genres", while a card keeps a single ranked line and fades
whatever does not fit. TMDB genres have no votes and render as they always have.
Films also show the TMDB synopsis and faceted descriptors (detail page only),
and the director in the same credit line where books show the author and music
the artist.
A facet-grouped descriptor filter narrows **any** tab (`?descriptor=…`);
descriptor chips on detail pages link to the same filtered view. Facets are
`mood`, `tone`, `atmosphere`, `setting`, `theme`, `style` and `creator`.
The filter accepts an alias as well (`melancholy` filters on `melancholic`).
While a title search or descriptor filter is active, the active tab's count
shows the number of matching items; the other tabs keep their library totals.
The grid shows 60 items per page and pagination keeps the active search and
descriptor filter.
Ratings are shown as a unified 0–10 score (one platform scale that maps all
three losslessly: RYM ÷1, Letterboxd stars ×2, Goodreads quarters ×2). Put the
exports in
`data/` (git-ignored — personal data never enters the repo):

```
data/
letterboxd/ratings.csv                                  # official ZIP export
rateyourmusic/<username>-music-export-with-reviews.csv  # profile "Export your data"
goodreads/goodreads_library_export.csv                  # desktop-site export
```

At startup the app parses all CSVs it finds and (re)builds `data/library.db`
(SQLite via SQLAlchemy). Ratings are normalized to a 0–5 scale internally
(RYM 1–10 ÷ 2), displayed as 0–10 scores; unrated items (RYM rating 0 /
wantlist, Goodreads unrated or to-read) are skipped. Re-export and restart to
refresh. The SQLite engine runs in WAL mode (`synchronous=NORMAL`, 5 s busy
timeout) so an enrichment pass can write while the app serves reads, and the
grid is paginated in SQL with indexes on the media/rating order and the
`(source, source_id)` identity.

### Film enrichment (TMDB)

Films are enriched with TMDB metadata — synopsis, poster path, genres,
director, and keywords — so they can feed the recommender. Set
`ARTHOUSE_TMDB_API_KEY` (v3 key or v4 read token), then run the enrichment
once:

```bash
uv run python -m arthouse.enrich_films            # all not-yet-enriched films
uv run python -m arthouse.enrich_films --limit 5  # small batch
uv run python -m arthouse.enrich_films --report   # keyword coverage + top terms
```

Matching is strict: normalized exact title (or TMDB `original_title`) with a
±1-year tolerance; very long titles retry with the part before the first comma.
Anything else is reported as unmatched rather than guessed — wrong genres are
worse than missing ones. Results live in `item_meta` plus `item_genre`, keyed
by `(source, source_id)`, so restarting the app (which rebuilds only `items`)
never discards them; re-runs skip already-enriched films.
`item_meta.tags_fetched` records the attempt, so a film TMDB has no
keywords for is not refetched every run. TV entries from the Letterboxd export
(series, miniseries) have no TMDB movie match and stay unmatched.

The enrichment schema is shared by every medium: `item_meta` stores
provider-neutral fields (`provider`, `external_id`, `summary`, `image_path`,
`credit`, `tags_fetched`), `item_miss` records lookups that found no match so
they are not retried on every run, `item_genre` stores display genres (with
`artist_level` marking the MusicBrainz artist fallback), `item_tag` stores raw
descriptor labels from any provider, and `item_descriptor` holds the
descriptors those labels map onto.

### Music enrichment (MusicBrainz)

Albums are enriched with MusicBrainz metadata — artist credit, curated genres,
and community tags. MusicBrainz requires a contact-bearing `User-Agent` and
allows one request per second; `ARTHOUSE_MUSICBRAINZ_USER_AGENT` overrides the
default, and the client throttles itself:

```bash
uv run python -m arthouse.enrich_music            # all not-yet-enriched albums
uv run python -m arthouse.enrich_music --limit 5  # small batch
```

A transient failure (429/5xx) widens that throttle by one second, up to five,
and every success eases it back towards one. Server retry hints are logged but
not acted on: a busy 503 answers with `Retry-After: 0`, and the `x-ratelimit-*`
headers describe a global per-second bucket shared with every other caller, so
the throttle alone decides the wait. Four attempts are made before the album is
given up on.

Matching is stricter than TMDB's: the normalized title must match exactly, so a
release MusicBrainz files under another title is reported as unmatched rather
than guessed. Year only breaks ties between hits — a reissue legitimately
carries a later date — and an album beats a same-name single. Genres come from
the album's curated genres, and every row keeps the community's vote tally,
which is what ranks the display and decides the primary/secondary split. When
the album has no genres the artist's are used, flagged `artist_level` in
`item_genre`. Community tags are the descriptor source; artist tags are never
borrowed, because a tag names one album's mood. A matched album is never
re-fetched, so a record that had no genres when it was enriched stays that way
until it is cleared and re-run. An album that does not match is recorded in
`item_miss`, so a later run skips it instead of searching the same query again;
delete its row to retry.

The batch logs one line per album to stderr — `fetching [12/272] Grace — Jeff
Buckley (1994)` — so stdout keeps only the summary and the unmatched list. The
summary separates what this run matched from what an earlier run had already
enriched and what an earlier run had already tried without a match.
`ARTHOUSE_DEBUG=1` raises the level to DEBUG, which adds the exact search query,
the titles and dates it returned, every throttle and retry with its status and
wait, and the body and headers of a hard failure. `httpx` and `httpcore` are
pinned to `WARNING` in `arthouse/logs.py`, so their duplicate request line and
byte-level trace never drown those entries. At one request per second, roughly
two calls per album, a full pass takes about ten minutes.

### Descriptor vocabulary

Every medium is described by one closed vocabulary in `src/arthouse/vocab.py`:
**222 descriptors** across seven facets — `mood`, `tone`, `atmosphere`,
`setting`, `theme`, `style`, and `creator` (who made the work). The list is
authored, not derived: a descriptor has to describe an album, a film and a book
equally well, so audio-only vocabulary (production, vocal technique, timbre) and
release provenance (`remake`, `imax`, `black and white`) are excluded. `creator`
is the one deliberate exception — it describes authorship rather than content,
and it exists because `woman director` is one of the most common provider tags.
It is only ever set from an explicit provider claim, never inferred from
someone's work.

Each descriptor carries two relations:

- **aliases** — the same concept in another form (`melancholy`, `sad`,
  `sorrowful` → `melancholic`). Any provider tag that normalises to an alias is
  tagged with that descriptor.
- **neighbours** — adjacent but different concepts (`sombre`, `bittersweet` for
  `melancholic`). Neighbours never tag anything; they exist so a filter can
  later widen to its surroundings.

Matching is exact after normalisation (case, punctuation, parentheticals, and
`-`/`_`/`/` all fold to spaces). There is no stemming, edit distance or model
inference: a tag the vocabulary does not know is dropped and reported, never
guessed at. That is deliberate — `poison`/`prison`, `angel`/`anger`, and
`busy`→`business` or `sad`→`sadism` are precisely what suffix rules get wrong.
A duplicate name, an alias claimed by two descriptors, an alias that lands on
another descriptor's name, or an unknown neighbour all fail at import.

Film descriptors come from TMDB **keywords** only; genres stay display-only.
Rebuild `item_descriptor` from the raw tags and inspect what mapped:

```bash
uv run python -m arthouse.tagging                    # rebuild + one-line summary
uv run python -m arthouse.tagging --report           # per-medium coverage
uv run python -m arthouse.tagging --report --media film --unmapped 40
```

The app rebuilds the table at startup and at the end of `arthouse.enrich_films`, so the
detail page and the filter never serve a stale vocabulary. The unmapped list is
the worklist: add an alias, re-run, coverage goes up. Current snapshot: 1,776 of
5,227 film keyword occurrences (**34.0%**) map onto 1,522 item-descriptor pairs
across 272 items, and 234 of 294 films get at least three descriptors.

`item_descriptor` stores only `(source, source_id, name)`; the facet lives in the
vocabulary, so there is no second copy to drift. Derived tables are regenerable,
so a schema change is handled by rebuilding rather than migrating: `create_all`
never alters an existing table, so delete `data/library.db`, let the app rebuild
it from the exports, and re-run the enrichment passes.

### Collaborative similarity (MovieLens)

`datasets/` (git-ignored) can hold the MovieLens exports (`ratings*.csv`,
`links.csv`) alongside the TMDB dump. Films join MovieLens through
`item_meta.external_id` → `links.csv` (`tmdbId` → `movieId`); nothing is written to
the database. Neighbours are adjusted-cosine item similarities (ratings centred
on each user's mean), computed only between films already in the library:

```bash
uv run python -m arthouse.collab --title "Seven Samurai"
uv run python -m arthouse.collab --ratings datasets/ratings.csv --title "In the Mood for Love"
```

The default is `ratings_small.csv` (671 users; 182 of the 215 linked library
films rated) and runs instantly. The full `ratings.csv` (26M ratings, 270,896
users) covers 215/215 films and takes about 53 s; only ratings for library
films are kept in memory. `--min-overlap` (default 5) sets the minimum number
of users who rated both films.

## Setup

```bash
uv sync
uv run arthouse            # http://127.0.0.1:8000
```

## CSS (Tailwind)

Styling is Tailwind v4, built with the standalone CLI. To download it, run:

```bash
mkdir -p .bin
curl -sL -o .bin/tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64
chmod +x .bin/tailwindcss
```

The source is `static/css/app.src.css`; it compiles to `static/css/app.css`:

```bash
.bin/tailwindcss -i static/css/app.src.css -o static/css/app.css --minify  # build
.bin/tailwindcss -i static/css/app.src.css -o static/css/app.css --watch   # develop
```

## Checks

CI runs all of the below on every push and pull request (`.github/workflows/ci.yml`).

```bash
uv run ruff check .
uv run ruff format --check .
uv run djlint templates --lint
uv run basedpyright src tests
uv run pytest
```

## Layout

```
src/arthouse/
main.py          # app factory: builds library.db, asset map, server entry point
config.py        # pydantic-settings; paths anchored to the project root
logs.py          # one logging policy for the server and every CLI entry point
deps.py          # Jinja2Templates singleton, SQLAlchemy session dependency
assets.py        # content-hashed asset URLs (`asset()` template global)
db.py            # SQLite engine/pragmas, schema and index creation
models.py        # ORM models and shared value objects (Item, GenreRow, …)
queries.py       # read queries: grid paging, detail joins, descriptor options
library.py       # platform CSV parsers, normalization, build_db
vocab.py         # closed cross-media descriptor vocabulary (7 facets, aliases)
tagging.py       # maps raw provider tags onto the vocabulary; coverage report
collab.py        # MovieLens adjusted-cosine item-item CF baseline
routes/pages.py  # HTML routes only
templates/         # Jinja2: base.html (navbar), index.html, library.html, library_detail.html
static/            # css/app.src.css (Tailwind input) → css/app.css (committed build)
static/js/         # vendored htmx 4.0.1 (tab switching; app works without it)
tests/             # httpx ASGITransport; synthetic fixtures; no sockets, no network
data/              # git-ignored: personal exports + generated library.db
```

## Configuration

Environment variables use the `ARTHOUSE_` prefix, or a `.env` at the project root:

| Variable | Default | Purpose |
|---|---|---|
| `ARTHOUSE_HOST` | `127.0.0.1` | Bind address for `uv run arthouse` |
| `ARTHOUSE_PORT` | `8000` | Bind port for `uv run arthouse` |
| `ARTHOUSE_DEBUG` | `false` | Enables reload; re-hashes assets per render; echoes SQL to stderr; raises CLI logging to DEBUG |
| `ARTHOUSE_DATA_DIR` | `<project>/data` | Exports directory; `library.db` is built here |
| `ARTHOUSE_TMDB_API_KEY` | unset | TMDB v3 key (or v4 read token) for film genre enrichment |
