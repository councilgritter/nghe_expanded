# Đọc — the reading module

A sibling to the listening drill: read real Vietnamese news, simplified to a CEFR
level you choose, with every word tappable. Long-press a word and its full compound
and meaning come up **from data already on the device** — no request, no waiting.

Articles come from **VOA Tiếng Việt** and **BBC News Tiếng Việt**, are rewritten at a
target level by DeepSeek, then split into words by a reconciler that treats a local
Vietnamese compound dictionary as authoritative. The result is exported as a static
bundle, so the reader is a plain page and the app stays a static site — the same
shape as the listening drill, which has no server either.

> **Attribution is not optional.** VOA and BBC both require credit wherever their
> text appears. Each article carries its notice through ingest, storage and export,
> and `web/index.html` always renders it. Do not strip it.

---

## Pipeline at a glance

```
RSS (VOA, BBC)
   │  dedupe on (source, guid) ── already have this article?  skip
   ▼
full article body  ── scrape + strip boilerplate, else fall back to the RSS summary
   │
   ▼
DeepSeek: "rewrite this at CEFR B1, keep the facts, mark compound words with _"
   │  fixed system prompt first, variable text after (context caching)
   ▼
segmentation ──┬─ the LLM's underscores
               └─ underthesea, run over the same text
                       │
                       ▼
              reconcile  (dictionary wins → agreement locks → disagreement keeps both)
                       │
                       ▼
              token map  (character offsets + definition + CEFR tag)
                       │
                       ▼
              SQLite  ──export──▶  data/site/*.json  ──▶  web/index.html
```

Full detail, including the runtime path, is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Why the dictionary decides

`underthesea` ships four Vietnamese word lists (`Viet11K` → `Viet74K`, 73,901
headwords, 64,668 of them multi-syllable). Those lists state which syllable runs form
one word, so they are used as a **hard constraint**: when the dictionary knows
`đại_học`, neither the model nor the segmenter may split it.

This matters more than it sounds. Measured over live articles, the dictionary locks
**33–50%** of all spans by itself, and even in the worst case — a model that marks
every syllable as its own word — only **1.5–6.2%** of spans end up ambiguous, because
the dictionary resolves the rest. The simplification prompt does not have to be
perfect for the segmentation to be good.

The word lists carry no glosses, so the dictionary decides *boundaries* only.
Definitions come from DeepSeek; `entries.definition` exists so a curated dictionary
can be dropped in later without a schema change.

## Setup

```bash
cd reading
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt

cp .env.example .env          # then put your DEEPSEEK_API_KEY in it
```

Python 3.12+ (CI uses 3.12; developed against 3.14). `underthesea` bundles its
segmentation model, so nothing downloads on first use.

## Running it

**1. Build the compound dictionary** (once — it reads the word lists out of the
installed `underthesea` package):

```bash
python -m reading.tools.build_dictionary
# 73,414 headwords (64,541 multi-syllable, longest 17 syllables)
```

**2. Ingest articles:**

```bash
python -m reading.pipeline.ingest --cefr B1 --limit 5
```

Useful flags:

| Flag | Effect |
|---|---|
| `--cefr A1..C2` | target level (default `B1`) |
| `--limit N` | cap new (article, level) pairs this run; `0` = unlimited |
| `--source voa\|bbc` | restrict to one source |
| `--refresh` | re-generate pairs that already exist (after a prompt change) |
| `--no-fetch-full` | RSS summaries only, no article pages |
| `--no-dictionary` | skip the dictionary constraint (debugging only) |
| `--db PATH` | override the database path |
| `--list` | show what is stored |

Ingest is **idempotent per (article, level)**: a second run costs nothing. One article
can carry several levels, and re-ingesting a level replaces that version's text, token
map and pre-teach rows in place.

**3. Export and read:**

```bash
python -m reading.tools.build_site
python -m http.server 8000    # from the repo root, then open /index.html
```

The app's entry screen asks whether you want **Nghe / Nói** or **Đọc / Viết**; this
module is the Đọc half. `/reading/web/index.html` also works directly, and its header
links back to the entry screen so the two halves are never a one-way trip.

`data/site/` is the deployable artifact — the reading equivalent of `data.json`, and
deliberately not gitignored so it can be published alongside the page. Regenerate it
from a real ingest before deploying; an export made with a stubbed model contains no
genuine definitions.

The page needs HTTP; `file://` blocks the `fetch` of the bundle.

**4. Tests:**

```bash
python -m pytest              # from reading/
# 105 passed
```

## Environment

Everything is read from the environment or `reading/.env`. No key is ever hardcoded,
and nothing secret reaches the browser.

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | **required for ingest** |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API base |
| `DEEPSEEK_MODEL` | `deepseek-chat` | model |
| `DEEPSEEK_TIMEOUT_S` / `DEEPSEEK_MAX_RETRIES` | `120` / `3` | request budget |
| `READING_DB_PATH` | `data/reading.sqlite3` | articles, token maps, cache |
| `READING_DICT_PATH` | `data/dictionary.sqlite3` | compound dictionary |
| `READING_SITE_DIR` | `data/site` | exported bundles |
| `VOA_RSS_URLS` | Tin tức + Việt Nam feeds | comma-separated |
| `BBC_RSS_URLS` | `feeds.bbci.co.uk/vietnamese/rss.xml` | comma-separated |
| `READING_FETCH_FULL` | `true` | scrape article pages |
| `READING_HTTP_TIMEOUT_S` | `30` | fetch timeout |
| `READING_FETCH_DELAY_S` | `1.0` | politeness delay between fetches |
| `READING_USER_AGENT` | `nghe-reading/0.1 …` | sent to the news sites |
| `READING_MAX_COMPOUND_SYLLABLES` | `4` | longest dictionary-enforced compound |
| `READING_DEFAULT_CEFR` | `B1` | default level |
| `READING_INGEST_LIMIT` | `0` | cap per run, `0` = unlimited |
| `READING_ALLOW_PEAK` | `false` | approve running during DeepSeek's peak hours |
| `READING_PEAK_WINDOWS` | `01:00-04:00,06:00-10:00` | peak windows, UTC hours |
| `READING_PEAK_WEEKDAYS` | `Mon,Tue,Wed,Thu,Fri` | days those windows apply |
| `READING_OFFPEAK_DATES` | — | ISO dates known to be Chinese holidays (off-peak all day) |

## Off-peak guard

**Ingest will not start during DeepSeek's peak hours without explicit approval.**
Peak costs double, and a run both fetches and translates, so the whole run is gated
rather than just the model calls.

DeepSeek's published window is **01:00–04:00 and 06:00–10:00 UTC, Monday through
Friday, excluding Chinese public holidays**; every other hour is off-peak at half
price, including all of Saturday and Sunday
([pricing docs](https://api-docs.deepseek.com/quick_start/pricing/)).

Outside the window the CLI refuses and says when it opens:

```
Pricing: PEAK now (2026-10-05 02:14 UTC) — off-peak resumes 2026-10-05 04:00 UTC (in 2h)

Refusing to run during peak hours: DeepSeek bills double, and this run would both
fetch and translate.
  peak = 01:00-04:00, 06:00-10:00 UTC on Mon, Tue, Wed, Thu, Fri (all other hours off-peak)
  Off-peak resumes 2026-10-05 04:00 UTC.
Re-run then, or pass --allow-peak (or set READING_ALLOW_PEAK=true) to approve this one explicitly.
```

Approval is per-run (`--allow-peak`) or blanket (`READING_ALLOW_PEAK=true`). The guard
takes `now` as an argument rather than reading the clock, because it is the one place
that authorises spending money and so is tested directly rather than through the
system time.

**Holidays are the conservative case.** Knowing whether today is a Chinese public
holiday needs a calendar this module does not carry, so an unlisted holiday is treated
as **peak** and the run waits rather than paying double by mistake. List the dates you
know in `READING_OFFPEAK_DATES` and those days become off-peak all day.

## Cost

Kept low by design:

- **Off-peak only, by default.** A run is refused during DeepSeek's peak hours, which
  cost double — see the guard above. That halves the bill for no code change.
- **Actual usage is printed, not estimated.** Every article logs the tokens DeepSeek
  reported, and the run ends with a total, so the real cost is visible per run.
- **One DeepSeek call per (article, level)** at ingest — never per word. Segmentation,
  reconciliation and the token map all run locally.
- **Definitions are mostly free**: the pre-teach glosses from that same call are reused
  for the token map, so ingest makes no per-word requests.
- **Zero cost per tap.** Roughly 70% of tokens end up with a stored definition; the rest
  resolve from the local cache. A tap only reaches the API when the offset has no token
  or the token has no gloss, and then it sends **one sentence**, not the article — and
  caches the answer, so the same context is never paid for twice.
- **Prompt caching**: system prompts are module constants and the article is never
  interpolated into them, so the prefix is byte-identical across calls.

## Layout

```
settings.py                 environment-only config (no secrets in code)
pipeline/
  sources.py                RSS fetch + parse, dedupe, attribution notices
  fetch_full.py             article page → prose, boilerplate stripped
  deepseek.py               the two API calls; JSON parsing and retries
  prompts.py                FIXED system prompts (cache-friendly) + version tag
  segment.py                underthesea word segmentation
  reconcile.py              dictionary ▸ agreement ▸ keep-both, with candidates
  tokens.py                 token-map builder (offsets ↔ syllables)
  vocab.py                  pre-teach rows + definition map
  extract.py                assemble the reading bundle
  resolve.py                tap-time resolution + the one-sentence fallback
  ingest.py                 the orchestrator and its CLI
storage/
  migrations/0001_init.sql  the schema (additive; numbered files)
  db.py                     connection, migration runner, queries
  dictionary.py             the compound dictionary and its lookups
tools/
  build_dictionary.py       underthesea word lists → SQLite
  build_site.py             stored articles → static bundles
web/index.html              the reader page (markup + styles)
web/reader.js               its logic, loaded as a separate file
fixtures/                   RSS, article and page fixtures for the tests
tests/                      105 tests
```

## Known limits

**The segmenter and the model will disagree, and the disagreement is stored rather
than hidden.** underthesea's reading becomes the primary span, the token is flagged
ambiguous, and both readings are kept. The reader sees the alternative and can widen
or narrow the selection by syllable. That is the remedy — not a silent guess.

**Pre-teach quality tracks the prompt.** The model picks the vocabulary and grammar
points; nothing downstream verifies that a chosen item is genuinely new at the target
level. It is a prompt-quality problem, and `prompts.PROMPT_VERSION` is recorded on
every version so a prompt change is visible in the data.

**Full-text extraction is a heuristic.** It scores `<p>` tags and keeps the densest
container. That is enough for VOA and BBC article pages, but a redesign on either site
could make it pick the wrong block. When a page cannot be parsed the RSS summary is
used instead and `articles.body_source` records which one you got.

**A mis-tap on a word with no stored definition needs the optional endpoint.** With
`DISAMBIGUATE_ENDPOINT` empty in `web/reader.js` the reader says it has no meaning
rather than calling out. Wiring that endpoint is `reading/pipeline/resolve.py` behind a
route — the logic already exists and is tested; the HTTP wrapper is phase 2.
