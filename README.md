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

`/library` renders your RateYourMusic, Letterboxd, and Goodreads exports as one
grouped page (Music / Films / Books, each sorted by rating); every entry links
to a detail page with the rating date, the original platform rating, and the
platform it came from. Ratings are shown as a unified 0–10 score (one platform
scale that maps all three losslessly: RYM ÷1, Letterboxd stars ×2, Goodreads
quarters ×2). Put the exports in
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
routes/pages.py  # HTML routes only
templates/         # Jinja2: base.html (navbar), index.html, library.html, library_detail.html
static/            # css/app.src.css (Tailwind input) → css/app.css (committed build)
tests/             # httpx ASGITransport; synthetic fixtures; no sockets, no network
data/              # git-ignored: personal exports + generated library.db
```

## Configuration

Environment variables use the `ARTHOUSE_` prefix, or a `.env` at the project root:

| Variable | Default | Purpose |
|---|---|---|
| `ARTHOUSE_HOST` | `127.0.0.1` | Bind address for `uv run arthouse` |
| `ARTHOUSE_PORT` | `8000` | Bind port for `uv run arthouse` |
| `ARTHOUSE_DEBUG` | `false` | Enables reload; re-hashes assets per render |
