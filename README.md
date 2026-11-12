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
stripped and paragraphs preserved). Enriched films also show TMDB genre tags
(on cards and the detail page), the TMDB synopsis and faceted keyword
descriptors (detail page only), and the director in the same credit line where
books show the author and music the artist.
On the Films tab, a facet-grouped descriptor filter narrows the grid
(`?descriptor=…`); descriptor chips on film detail pages link to the same
filtered view. The select lists the 30 most common descriptors per facet;
rarer ones stay reachable from a film's chips.
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
refresh.

### Film enrichment (TMDB)

Films are enriched with TMDB metadata — synopsis, poster path, genres,
director, and keywords — so they can feed the recommender. Set
`ARTHOUSE_TMDB_API_KEY` (v3 key or v4 read token), then run the enrichment
once:

```bash
uv run python -m arthouse.enrich            # all not-yet-enriched films
uv run python -m arthouse.enrich --limit 5  # small batch
uv run python -m arthouse.enrich --report   # keyword coverage + top terms
```

Matching is strict: normalized exact title (or TMDB `original_title`) with a
±1-year tolerance; very long titles retry with the part before the first comma.
Anything else is reported as unmatched rather than guessed — wrong genres are
worse than missing ones. Results live in `film_meta` / `film_genre` /
`film_keyword`, keyed by `(source, source_id)`, so restarting the app (which
rebuilds only `items`) never discards them; re-runs skip already-enriched
films. `film_meta.keywords_fetched` records the attempt, so a film TMDB has no
keywords for is not refetched every run. TV entries from the Letterboxd export
(series, miniseries) have no TMDB movie match and stay unmatched.

### Content similarity (descriptors)

TMDB keywords are normalized into a shared vocabulary (`based on novel` →
`based on novel or book`, parentheticals stripped, place variants unified) and
tagged with a facet: `theme`, `mood`, `style`, `setting`, or `production`.
Production trivia (`aftercreditsstinger`, `duringcreditsstinger`) is dropped.
Rebuild the derived tables and print the most widely shared descriptors per
facet:

```bash
uv run python -m arthouse.descriptors
```

Nearest neighbours are TF-IDF cosine over binary descriptor presence
(`idf = log((1 + N) / (1 + df)) + 1`, L2-normalized); descriptors shared by
fewer than 3 films are ignored by default (`--min-df`). Ask for specific films:

```bash
uv run python -m arthouse.descriptors --title "Seven Samurai" --title "Aftersun"
```

The library filter reads the same derived tables. The app rebuilds them at
startup, and `python -m arthouse.enrich` rebuilds them after each run, so the
detail page and filter never serve a stale vocabulary.

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
deps.py          # Jinja2Templates singleton, SQLAlchemy session dependency
assets.py        # content-hashed asset URLs (`asset()` template global)
library.py       # ORM model, platform CSV parsers, SQLite build/queries
descriptors.py   # keyword normalization, facets, TF-IDF content kNN
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
| `ARTHOUSE_DEBUG` | `false` | Enables reload; re-hashes assets per render; echoes SQL to stderr |
| `ARTHOUSE_DATA_DIR` | `<project>/data` | Exports directory; `library.db` is built here |
| `ARTHOUSE_TMDB_API_KEY` | unset | TMDB v3 key (or v4 read token) for film genre enrichment |
