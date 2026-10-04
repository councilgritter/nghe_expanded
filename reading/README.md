# Đọc — the reading module

A sibling to the listening drill: read real Vietnamese news, simplified to a CEFR
level you choose, with every word tappable. Tap a word and its meaning comes up
**from data already on the device** — no request, no waiting. The meaning is the
**English** one, from a bundled Việt→Anh dictionary; the model's Vietnamese gloss
sits underneath it.

Articles come from **VOA Tiếng Việt** and **BBC News Tiếng Việt**, are rewritten at a
target level by DeepSeek, then split into words by a reconciler that treats a local
Vietnamese compound dictionary as authoritative. The result is exported as a static
bundle, so the reader is a plain page and the app stays a static site — the same
shape as the listening drill, which has no server either.

On top of the article:

- **any run of words can be highlighted** and looked up offline — the bundle carries
  every dictionary span in the article, not just the tokens the segmenter produced;
- **the device reads it aloud** in Vietnamese (no key, no server, works offline);
- **comprehension questions and a writing task** are generated per article and graded
  locally where that is possible;
- **a light/dark theme**, and an optional **password-gated refresh button** that starts
  an ingest run without ever putting the API key in the page.

> **Attribution is not optional.** VOA and BBC both require credit wherever their
> text appears, and the English glosses are derived from Wiktionary under CC BY-SA
> 4.0. Each article carries its notice through ingest, storage and export, and
> `web/index.html` always renders it. Do not strip it.

> **Nothing here ever runs by itself.** Ingest, gloss import and export are three
> commands you run by hand. There is no schedule, no cron, no workflow, and no code
> path that reaches DeepSeek as a side effect of opening the app — opening the
> reader only fetches a committed JSON bundle. Every token you spend, you spend by
> typing one of the commands below, or by pressing the refresh button, which is
> itself a password-gated dispatch of one of those commands. See *Refreshing from the
> app*.

---

## Pipeline at a glance

```
RSS (VOA, BBC)
   │  dedupe on (source, guid) ── already have this article?  skip
   ▼
full article body  ── scrape + strip boilerplate, else fall back to the RSS summary
   │                   (items too short to read are skipped before any paid call)
   │
   ▼
DeepSeek: "rewrite this at CEFR B1, keep the facts, mark compound words with _"
   │  fixed system prompt first, variable text after (context caching)
   ▼
segmentation ──┬─ the LLM's underscores
               └─ underthesea, run over the same text
                       │
                       ▼
              reconcile  (dictionary wins, under two guards → agreement locks →
                          disagreement keeps both)
                       │
                       ▼
              token map  (character offsets + English and Vietnamese definitions + CEFR tag)
                       │
                       ├── DeepSeek (second, cheaper call): comprehension questions + writing task
                       ▼
              SQLite  ──export──▶  data/site/*.json  ──▶  web/index.html
                          (+ the offline lookup index for every dictionary span)
```

Full detail, including the runtime path, is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Why the dictionary decides — and where it doesn't

`underthesea` ships four Vietnamese word lists (`Viet11K` → `Viet74K`, 73,901
headwords, 64,668 of them multi-syllable). Those lists state which syllable runs form
one word, so they are used as a **hard constraint**: when the dictionary knows
`đại_học`, neither the model nor the segmenter may split it.

This matters more than it sounds. Measured over live articles, the dictionary locks
**33–50%** of all spans by itself, and even in the worst case — a model that marks
every syllable as its own word — only **1.5–6.2%** of spans end up ambiguous, because
the dictionary resolves the rest. The simplification prompt does not have to be
perfect for the segmentation to be good.

**But the word lists are corpus-derived, so a lock has to be earned.** Measured over
the stored articles, 183 distinct multi-syllable locked spans had no English gloss at
all, and the list contained `của ông`, `không phải`, `trong lúc`, `các vị` — sequences
that are phrases, not words. Locking those hides two words that each have a meaning
behind one span that has none. Two guards now narrow the lock:

| Guard | What it refuses | Why it is safe |
|---|---|---|
| **Contiguity** | a span with punctuation, a quote or a paragraph break inside it | the syllable sequence carries no punctuation, so `BBC.\n\nÔng` used to be emitted as one tappable "word". Digits keep their separators, so `300.000` survives |
| **Function-word run** | a span whose *every* syllable is a function word, **when nothing glosses it** | `của ông`, `không phải`, `lúc nào` are now two tappable words each. `trước đây`, `chúng tôi`, `tháng Một`, `vì vậy` are glossed and therefore kept — see below |

That second condition is not decoration. The first version of the guard split every
run of function words, and it destroyed real words: `chúng tôi` (we) became `chúng` +
`tôi`, where `tôi` leads with "slave; domestic servant", and `tháng Một` (January)
became `tháng` + `Một`, "month" + "one". A guard that trades a wrong-ish compound for
two wrong words is worse than no guard. It now only fires when the dictionary has no
gloss for the span, which is exactly the case the reader complained about: a merged
token that shows no meaning at all.

The word lists carry no glosses, so that layer decides *boundaries* only.

## Two dictionaries in one file

`data/dictionary.sqlite3` holds two independent layers, built by two commands:

| Table | Built by | What it is |
|---|---|---|
| `entries` | `tools/build_dictionary.py` | compound boundaries from underthesea's word lists, **no glosses** |
| `glosses` | `tools/build_glosses.py` | English senses from the Wiktionary-derived Vietnamese dictionary (CC BY-SA 4.0) |

They are separate because they answer different questions and come from different
sources: the boundary layer decides where words start and stop, the gloss layer
supplies the meaning shown on a tap. The gloss layer is deliberately **not** restricted
to the boundary headwords, so a word the segmenter produced is still glossed when the
underthesea lists do not contain it.

That is what makes the English coverage high: **88% of tokens** in the current bundle
carry an English definition, against **17%** when definitions came only from the
model's pre-teach list. The reader never sees the dictionary — definitions are baked
into the exported bundle — so swapping in a different gloss dataset is a rebuild and
a re-export, not a client change.

## Highlighting a string of words

A tap resolves one token. The dictionary itself never reaches the browser, so a range
the segmenter did not produce used to be a dead end: the sheet said "no meaning for
this", and the only escape was a paid lookup.

`tools/build_site.py` now exports a **lookup index** with each article: every run of
2–6 syllables in the text that the dictionary knows, with its English gloss. Measured
on the live articles, a 1,750-syllable article yields **~500 spans**, costing about
45 KB on a bundle that is already 200–570 KB (**105 KB gzipped**). In exchange:

- dragging across words (or shift-clicking) selects the whole range;
- if that range is a dictionary word — even one the segmenter split — the meaning
  appears instantly, offline, with no server and no per-tap cost. `Thay vào đó`
  arrives as three separate tokens and now resolves to "instead";
- if the dictionary knows the span but has no meaning for it, the sheet says so,
  which is a different answer from "not a word";
- the expand/shrink handles still work, and each narrowing is looked up the same way.

## Practice: comprehension and writing

Comprehension questions and a writing task come from a **second, cheaper call**
(`prompts.EXERCISES_SYSTEM_PROMPT`) on the *simplified* text — not from the
simplification call itself. Splitting them is deliberate:

- **the article is never at risk.** Asking one call for the article, the pre-teach
  list and the exercises risks a truncated response that loses the article;
- **exercises can be added later.** `--refresh-exercises` gives practice content to
  articles that are already simplified and paid for, without re-simplifying anything:
  ten stored articles cost ten small calls instead of ten rewrites;
- **they version separately.** `prompts.EXERCISES_PROMPT_VERSION` is stored per
  version, so a change to the questions re-pays only for the versions whose questions
  are stale.

Each set is four multiple-choice questions (one of them requiring a relation, not a
fact lookup), 1–3 open questions with a model answer and a key-point checklist, and one
writing task with the facts a good answer has to use.

**Grading is local and honest about its limits.** The multiple-choice questions are
marked exactly. The writing task checks length and which key facts appear (a
case-folded content-word overlap, with function words dropped), and then shows the
model answer — a checklist, not a grade: it can see that a fact is missing, never that
a sentence is good. Grammar feedback would need a model call per submission, which is
what `DISAMBIGUATE_ENDPOINT`-style infrastructure is for; it is not built.

## Reading it aloud

The speaker buttons use the **device's own Vietnamese voice** (`SpeechSynthesis`):
no key, no server, no cost, and it works offline. The drill's clips on R2 are
per-syllable, so they are the wrong asset for prose — that is why this is the browser
voice and not a re-use of the audio pipeline.

- ▶ **Đọc bài** reads the whole article, paragraph by paragraph, highlighting the word
  being spoken where the browser reports boundaries (Chrome does; Safari does not);
- each paragraph has its own ▶ for repetition;
- the definition sheet has 🔊 for the selected word;
- speed is 0.7× / 0.85× / 1×, and the voice picker lists the Vietnamese voices the
  device has.

**Desktop browsers often ship no Vietnamese voice at all.** When there is none the
controls disable themselves and the button title says to install one in the system
settings, rather than playing silence. iOS and Android both ship one.

## Theme

Dark by default, light on request. `theme.js` is loaded synchronously in `<head>`, so
the attribute is on `<html>` before the first paint — deferring it would flash the
wrong palette on every page load. The default follows `prefers-color-scheme` until the
reader makes an explicit choice, which is then stored in `localStorage`. Both pages
(the drill and the reading half) share the file and the same stored preference.

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

Run the commands below **from the repository root**, so that `reading` is importable
as a package.

## Running it

**1. Build the two dictionary layers** (once — boundaries first, then English glosses):

```bash
python -m reading.tools.build_dictionary
# 73,414 headwords (64,541 multi-syllable, longest 17 syllables)

python -m reading.tools.build_glosses
# downloads the ~80 MB kaikki.org extract to data/_dict_src/ (gitignored), then
# imports it: ~39,000 headwords, ~56,000 senses
```

`build_glosses` caches its download, so re-running it is cheap. `--source <url|path>`
points it at a different gloss dataset; `--force-download` re-fetches.

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
| `--refresh-outdated` | re-generate **only** the pairs whose stored prompt version is older than the current one |
| `--refresh-exercises` | practice content for stored articles whose questions are missing or stale — **no re-simplification, nothing is fetched**; this is the whole run |
| `--force-exercises` | with the above: regenerate even the current ones |
| `--no-exercises` | skip the practice call, so an ingest run costs one call per pair instead of two |
| `--refetch-body` | re-scrape the article page for stored articles (after an extraction fix) |
| `--no-fetch-full` | RSS summaries only, no article pages |
| `--no-dictionary` | skip the dictionary constraint (debugging only) |
| `--db PATH` | override the database path |
| `--list` | show what is stored |

Ingest is **idempotent per (article, level)**: a second run costs nothing. One article
can carry several levels, and re-ingesting a level replaces that version's text, token
map and pre-teach rows in place.

Adding practice content to everything already stored, without touching the text:

```bash
python -m reading.pipeline.ingest --refresh-exercises
```

**3. Re-apply the segmentation guards to stored articles** (free — no model call):

```bash
python -m reading.tools.resegment --dry-run    # report only
python -m reading.tools.resegment              # write
```

The guards live in the *reconciler*, not in the model, and the stored text does not
change — so this repairs the token map of articles already in the database without
paying DeepSeek again. It splits only what the guards refuse (spans crossing
punctuation, unglossed function-word runs) and leaves everything else byte-identical,
including each token's ambiguity flag and its alternative readings. On the live corpus
it repaired 8 of 10 versions, 8–21 spans each, and moved tokens without a definition
from 1,019 to 948.

**4. Export and read:**

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

**5. Tests:**

```bash
python -m pytest              # from the repository root
# 303 tests
```

## Environment

Everything is read from the environment or `reading/.env`. No key is ever hardcoded,
and nothing secret reaches the browser.

| Variable | Default | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | **required for ingest** |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API base |
| `DEEPSEEK_MODEL` | `deepseek-chat` | model |
| `DEEPSEEK_TIMEOUT_S` / `DEEPSEEK_MAX_RETRIES` | `600` / `3` | request budget; generous on purpose, see *Model choice* |
| `READING_DB_PATH` | `data/reading.sqlite3` | articles, token maps, cache |
| `READING_DICT_PATH` | `data/dictionary.sqlite3` | compound dictionary **and** English glosses |
| `READING_SITE_DIR` | `data/site` | exported bundles |
| `VOA_RSS_URLS` | Tin tức + Việt Nam feeds | comma-separated |
| `BBC_RSS_URLS` | `feeds.bbci.co.uk/vietnamese/rss.xml` | comma-separated |
| `READING_FETCH_FULL` | `true` | scrape article pages |
| `READING_HTTP_TIMEOUT_S` | `30` | fetch timeout |
| `READING_FETCH_DELAY_S` | `1.0` | politeness delay between fetches |
| `READING_USER_AGENT` | `nghe-reading/0.1 …` | sent to the news sites |
| `READING_MIN_BODY_CHARS` | `400` | shortest body worth a reading exercise; shorter items are skipped **before** the paid call |
| `READING_MAX_COMPOUND_SYLLABLES` | `4` | longest dictionary-enforced compound |
| `READING_DEFAULT_CEFR` | `B1` | default level |
| `READING_INGEST_LIMIT` | `0` | cap per run, `0` = unlimited |
| `READING_ALLOW_PEAK` | `false` | approve running during DeepSeek's peak hours |
| `READING_PEAK_WINDOWS` | `01:00-04:00,06:00-10:00` | peak windows, UTC hours |
| `READING_PEAK_WEEKDAYS` | `Mon,Tue,Wed,Thu,Fri` | days those windows apply |
| `READING_OFFPEAK_DATES` | — | ISO dates known to be Chinese holidays (off-peak all day) |
| `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | — | only for `tools/r2_sync.py`, which carries the database and dictionary to and from object storage for a CI run |
| `READING_R2_PREFIX` | `reading` | object key prefix inside that bucket |

## Refreshing from the app

The reader's **↻ Cập nhật** button is optional and off by default. It exists because
the site is static: ingesting needs Python and the DeepSeek key, and a key on a page is
a key anyone can read. So the button only asks for a **password** and posts it to an
endpoint you deploy; the endpoint holds the GitHub token and dispatches a workflow, and
the workflow holds the key.

```
browser ──password──▶ Cloudflare Worker ──dispatch──▶ GitHub Actions
                       (refresh-worker/)              (reading-refresh.yml)
                                                         ingest → export → commit
                                                                     │
                                                        GitHub Pages redeploys
```

Nothing is enabled until you set `REFRESH_ENDPOINT` in `web/reader.js`; until then the
button is hidden and the page cannot spend anything. Deployment is documented in
[`../refresh-worker/README.md`](../refresh-worker/README.md), including the two files
the workflow restores from object storage first.

**That restore is the part that matters.** Ingest's idempotency — "already ingested at
B1" — is a row in `data/reading.sqlite3`, which is gitignored and therefore **absent
from a fresh checkout**. A run without it re-simplifies every feed article and pays for
all of them again. `tools/r2_sync.py` moves that database (and the 15 MB dictionary,
without which segmentation silently loses its constraint) into the same R2 bucket the
audio already uses:

```bash
python -m reading.tools.r2_sync get   # before an ingest run (needs boto3 + R2 env)
python -m reading.tools.r2_sync put   # after one
```

You do not need the Worker to refresh: the Actions tab → *Refresh reading articles* →
*Run workflow* is the same run, with GitHub's own authentication and no new
infrastructure. And the off-peak guard still rules — a run started at peak is refused,
costs nothing, and says when off-peak resumes.

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
- **One simplification call per (article, level)**, plus one cheaper practice-content
  call on the simplified text. Never per word: segmentation, reconciliation and the
  token map all run locally. `--no-exercises` drops the second call if you do not want
  the questions.
- **Definitions are mostly free**: the English glosses come from the local dictionary
  and the Vietnamese ones from the same call's pre-teach list. Ingest makes **no**
  per-word requests, so a bigger article costs one call, not one call per word.
- **Zero cost per tap.** 88% of tokens end up with a stored English definition, and the
  exported lookup index answers for any highlighted range the dictionary knows; the
  rest resolve from the local cache. A tap only reaches the API when the offset has no
  token or the range has no gloss anywhere, and then it sends **one sentence**, not the
  article — and caches the answer, so the same context is never paid for twice.
- **Practice content can be added later, cheaply.** `--refresh-exercises` calls only
  the practice prompt, for the stored articles that need it: ten articles cost ten
  small calls instead of ten rewrites.
- **Re-segmentation is free.** `tools/resegment.py` re-applies the reconciler's guards
  to stored articles with no model call at all.
- **Items too short to read are skipped before the call**, so a video clip's two-sentence
  lede never costs a simplification.
- **Prompt caching**: system prompts are module constants and the article is never
  interpolated into them, so the prefix is byte-identical across calls.

## Layout

```
settings.py                 environment-only config (no secrets in code)
pipeline/
  sources.py                RSS fetch + parse, dedupe, attribution notices
  fetch_full.py             article page → prose, boilerplate stripped
  deepseek.py               the API calls (simplify, exercises, define); parsing, retries
  prompts.py                FIXED system prompts (cache-friendly) + version tags
  segment.py                underthesea word segmentation
  reconcile.py              dictionary ▸ agreement ▸ keep-both, under two guards
  tokens.py                 token-map builder (offsets ↔ syllables, both glosses)
  lookup.py                 offline lookup index: every dictionary span in an article
  vocab.py                  pre-teach rows + definition map
  exercises.py              practice rows (questions + writing task)
  extract.py                assemble the reading bundle
  resolve.py                tap-time resolution + the one-sentence fallback
  ingest.py                 the orchestrator and its CLI
  text.py                   syllable offsets, paragraph-aware whitespace, contiguity
  offpeak.py                the peak-hours guard on paid runs
storage/
  migrations/0001_init.sql  the schema (additive; numbered files)
  migrations/0002_english_glosses.sql
                            definition_en / definition_vi / senses_en / gloss_en
  migrations/0003_exercises.sql
                            the `exercises` table + exercises_prompt_version
  db.py                     connection, migration runner, queries
  dictionary.py             boundaries *and* the English gloss lookup
tools/
  build_dictionary.py       underthesea word lists → boundaries in SQLite
  build_glosses.py          kaikki.org Wiktionary extract → English glosses
  build_site.py             stored articles → static bundles (with the lookup index)
  resegment.py              re-apply the guards to stored articles, free
  r2_sync.py                carry the database + dictionary to/from object storage
web/index.html              the reader page (markup + styles, dark and light)
web/reader.js               its logic, loaded as a separate file
fixtures/                   RSS, article and page fixtures for the tests
tests/                      303 tests
```

Two files it shares with the drill rather than duplicating: `../theme.js` (dark/light,
loaded before paint so there is no flash) and `../refresh-worker/` (the optional
password-gated refresh endpoint).

## Known limits

**Simplification depends on the model *reasoning*, and that is not obvious from the
outside.** This cost two rounds of debugging, so it is written down here.

`deepseek-chat` is a retired alias that is no longer returned by `/models` (only
`deepseek-flash` and `deepseek-v4-pro` are). Pinned in `.env`, it returns the article
**verbatim** with underscores added: measured across the stored articles, 0.0–0.3% of
words differed from the source and A2 came back byte-identical to B1. Every downstream
artefact looked perfect — the bundle was valid, the token map was sound, the
segmentation was right — so nothing failed; an A2 learner simply read unmodified B2
prose.

`deepseek-flash` does rewrite (15–21% of words changed, sentences genuinely split and
simplified). It is a **reasoning** model: it thinks before answering, and the thinking
is what makes it comply. Disabling it — `reasoning_effort: "none"` or
`thinking: {"type": "disabled"}` — reproduces the transcription exactly. So do not
turn thinking off to save tokens; it is the feature.

Two consequences:

- Reasoning is billed as completion tokens and dominates the bill (one 8,000-character
  article reported ~9,800 reasoning tokens on top of a ~5,000-token answer). The
  closing summary prints the split.
- A single call can run for minutes, and the response is **streamed** so the socket is
  never silent long enough for the server to drop it. The default timeout is 600 s for
  the same reason. A non-streamed request at 120 s failed outright on the two longest
  articles with `incomplete chunked read`.

`ingest_article` now measures how much of the article the rewrite actually changed and
prints it per article (`rewrite: 18% of words changed`). Below 5% at A1–B2 it also
records a warning in the version's `notes`. That is the guard that makes this class of
failure visible instead of silent — it is a warning and not a hard failure because
C1/C2 legitimately stay close to the source.

**The segmenter and the model will disagree, and the disagreement is stored rather
than hidden.** underthesea's reading becomes the primary span, the token is flagged
ambiguous, and both readings are kept. The reader sees the alternative and can widen
or narrow the selection by syllable. That is the remedy — not a silent guess.

Note that the `syllable_mismatch` quality flag covers *either* side failing to line up,
and in the current data it is underthesea that gets rejected (its token count does not
cover the syllable sequence), not the model. The reader therefore states that the
segmentation is uncertain and shows the recorded reason rather than blaming the model.

**Pre-teach quality tracks the prompt.** The model picks the vocabulary and grammar
points; nothing downstream verifies that a chosen item is genuinely new at the target
level. It is a prompt-quality problem, and `prompts.PROMPT_VERSION` is recorded on
every version so a prompt change is visible in the data.

**The practice content is unchecked by anything but its prompt.** The questions have to
be answerable from the article, the distractors have to be plausible and the writing
task has to be about the right facts — and only the model's compliance makes that true.
`prompts.EXERCISES_PROMPT_VERSION` is recorded per version for the same reason as the
pre-teach tag: so a prompt change is visible in the data rather than guessed at.

**Full-text extraction is a heuristic.** It scores paragraph containers and keeps the
deepest one that still holds ~all of the page's prose, after dropping navigation,
related-links and link-farm paragraphs. That is enough for VOA and BBC, but a redesign
on either site could still make it pick the wrong block. When a page cannot be parsed
the RSS summary is used instead and `articles.body_source` records which one you got.

**VOA is half multimedia.** Some VOA feed items are video pieces whose page carries
only a two-sentence lede and no article body at all — it is not a scraping failure, and
no amount of retrying finds text that the page does not contain. Those items fall below
`READING_MIN_BODY_CHARS` and are skipped. BBC and VOA's text articles give full bodies
(2.7k–8.2k characters in the current bundle).

**English glosses are Wiktionary, so they are occasionally verbose or off-register, and
the sense shown is not always the sense in play.** A tap shows the first sense the
dictionary lists, which may include a parenthetical explanation — `vào` leads with "to
enter (to go or come into a space that is on the inside…)". The remaining senses are
shown underneath. This is the honest reason the function-word guard keeps a glossed
compound: `tôi` alone leads with "slave; domestic servant" and `là` with "fine silk", so
splitting `chúng tôi` into its syllables produced two worse readings than "we". Where a
single-syllable gloss looks wrong, that is this, not the segmentation — a sense-ranking
fix in `tools/build_glosses.py` is the place to improve it.

**Writing feedback is a checklist, not a grade.** Word count, key facts present (a
content-word overlap) and the model answer — enough to tell you that something is
missing, never that a sentence is good. Real grammar feedback needs a model call per
submission, which is the same server the disambiguation fallback needs.

**A mis-tap on a word with no stored definition needs the optional endpoint.** The
lookup index covers every span the dictionary knows, so this is now rare — but a range
with no dictionary entry anywhere still cannot be defined without a call. With
`DISAMBIGUATE_ENDPOINT` empty in `web/reader.js` the reader says so rather than calling
out. Wiring that endpoint is `reading/pipeline/resolve.py` behind a route — the logic
already exists and is tested; the HTTP wrapper is the one piece of *Phase 2* still
unbuilt (the refresh trigger is now `refresh-worker/` + `.github/workflows/reading-refresh.yml`).
