# Architecture

Two pipelines that barely touch: a **batch ingest** that pays for language model
calls, and a **runtime** that must not. Everything below exists to keep those two
properties — ingest does all the expensive work once, the reader does none of it.

```
                        INGEST (offline, batch, costs money)
                        ────────────────────────────────────
   VOA RSS ─┐
            ├─▶ dedupe ─▶ body ─▶ DeepSeek ─▶ segment ─▶ reconcile ─▶ token map ─▶ SQLite
   BBC RSS ─┘   (per        │     (1 call     (2 ways)   (dictionary    (offsets)      │
                level)      │      per                     decides) + guards →         │
                            │      article·level)                                      │
                            └── scrape, else RSS summary                              │
                                                                                      │
                                                                            build_site│
                                                                                      ▼
                                                                        data/site/*.json
                                                                                      │
                        RUNTIME (browser, no server, no cost)                         │
                        ─────────────────────────────────────                         │
   tap ─▶ offset ─▶ token map lookup ─▶ definition? ──yes──▶ show it     ◀────────────┘
                          │                 │
                          │                 no
                          │                 ▼
                          │          local cache ──hit──▶ show it
                          │                 │
                          │                 miss
                          │                 ▼
                          └──▶ no token? ──▶ ONE sentence + selection ─▶ DeepSeek
                                                    (optional endpoint)      │
                                                                             ▼
                                                                    cache locally, show
```

Two model calls now run at ingest, and the order matters. The simplification call
produces the article; the **practice call** comes second, takes the *simplified* text,
and returns comprehension questions and a writing task. Separate rather than more
output from the first call because (a) a truncated single response would lose the
article itself, and (b) the article is already paid for and stored, so exercises can be
added to ten stored articles later for ten small calls — `ingest --refresh-exercises` —
instead of ten rewrites.

## Why the reader is static

The listening drill is a static site (GitHub Pages + R2). Adding a reading module
that needed a server would mean introducing auth, a database host, and a deployment
story that does not exist in this repo — for a feature whose hot path is "look up a
word I already have locally".

So the expensive half is offline and the artifact is committed, exactly like
`build_data.py` → `data.json`. The page fetches one JSON bundle and resolves taps
against it. The only thing that genuinely needs a server is the rare disambiguation
fallback, and that is deliberately left behind a single configurable constant
(`DISAMBIGUATE_ENDPOINT` in `web/reader.js`) — see *Phase 2*.

## Ingest

### 1. Sources

`pipeline/sources.py` fetches the configured feeds with `feedparser`. Both VOA and BBC
require attribution; the notice is attached per article and travels with it through
storage and export.

Deduplication is on **`(article, level)`**, not on the article. An article that already
exists at another level is reused — its body is immutable, only the simplification
differs — so generating A2 and B1 from one article fetches the page once and simplifies
twice.

### 2. Body

`pipeline/fetch_full.py` strips boilerplate (`script`/`style`/`nav`/`footer`/`aside`/
`figure`), keeps the paragraphs that look like prose — long enough, and not mostly
anchor text — and then scores every **ancestor** by how much of that prose it contains.
The container it returns is the deepest one still holding ~all of it.

Scoring ancestors rather than immediate parents is load-bearing. BBC wraps each
paragraph in its own `<div>`, so every immediate parent holds exactly one paragraph, all
the scores tie, and a max() over parents returns whichever came first in the document.
The page held 8,011 characters across 48 paragraphs; the extractor stored 315 — the
first paragraph — and reported `body_source='full'`, because 315 clears the
`MIN_BODY_CHARS = 240` floor. That is how a silent 96% loss looks from the outside, and
it is why the extractor now has its own test file.

It is still a heuristic, not a readability port, and it is allowed to fail: the RSS
summary is the fallback and `articles.body_source` records which was used. Items that
are short even after extraction are dropped by the ingest gate below rather than stored
as stubs.

### 3. Simplification

One DeepSeek call per (article, level). The system prompt is a module constant and the
article is never interpolated into it, so the prefix is byte-identical on every call and
DeepSeek's context cache can hit. All variation rides in the user message, *after* it.

The model returns JSON: the rewritten article with `_` marking compound boundaries, plus
pre-teach vocabulary and grammar points.

Two properties of the call are load-bearing, and both were learned the hard way:

- **The model must reason.** `deepseek-flash` reasons before answering and that is what
  makes it rewrite at all; with reasoning disabled it returns the source verbatim.
  Reasoning is billed as completion tokens and dominates the cost, so the run summary
  prints the split.
- **The response is streamed.** A non-streamed request leaves the socket silent until
  the whole answer exists; for an 8,000-character article that is minutes, and the
  server drops the connection mid-generation. Streaming also means reasoning arrives as
  `delta.reasoning_content`, which the reader discards rather than letting it leak into
  the stored article.

The prompt states the rewriting requirement and the segmentation requirement as two
separate steps, and calls out transcription as a failure — a model that treats
"segment these words" as the whole task is the failure mode this pipeline actually hit.

`ingest_article` measures the share of words the rewrite changed and prints it, because
a transcription produces a bundle that is valid in every other respect.

### 4. Two segmentations of the same text

This is the part worth being careful about. There are two coordinate systems:

- **character offsets** into the stored text — what the reader needs, and
- **syllable indices** — what segmentation actually talks about, since Vietnamese words
  are runs of space-separated syllables.

Every grouping is therefore reduced to a list of **counts** (`[2, 1, 3]`), and both
groupings are checked to cover exactly the same syllable sequence before anything is
compared. A grouping that does not cover it is rejected outright rather than
mis-aligned.

> The pipeline deliberately does **not** segment from underthesea's `format='text'`
> output: that string re-spaces punctuation (`gia .`), which would shift every offset
> after it. It takes the token list and counts groups instead, leaving the source text
> untouched.

### 5. Reconciliation — `pipeline/reconcile.py`

A fixed, testable decision order:

| Order | Condition | Outcome |
|---|---|---|
| 1 | the dictionary knows a word starting here **and the guards allow it** | **locked**, source `dictionary` |
| 2 | the model and underthesea propose the same span | **locked**, source `agreed` |
| 3 | they disagree | underthesea's span becomes primary, token marked **ambiguous**, **both** readings kept as candidates |
| 4 | a span the guards refuse (punctuation inside it, or a run of function words nothing can gloss) | **split**, source `split` |

The dictionary wins because the word lists are a statement of record about Vietnamese
compound boundaries; the model does not get to override them. On disagreement,
underthesea is primary because it is deterministic and trained on Vietnamese, and
because biasing to the *shorter* span is the safe failure — it can never swallow a
clause. The alternative is not discarded: it is stored, shown to the reader, and the
expand/shrink handles act on it.

**The guards are the part that took measurement to get right.** The word lists are
corpus-derived, so "the dictionary knows it" is not the same as "it is a word": 183
distinct multi-syllable locked spans in the stored corpus had no gloss at all, including
`của ông`, `không phải`, `trong lúc`. Two guards narrow the lock, and both are stated
in `reconcile.py` with the measurement that produced them:

* **contiguity** — no punctuation, quote or paragraph break inside a span. The syllable
  sequence only carries `\w+` runs, so this is checked through the `allow_span`
  predicate that ingest builds from the text (`text.span_is_contiguous`). Digits keep
  their separators, so `300.000` stays one token.
* **function-word run, but only when nothing glosses it** — a run of function words the
  dictionary can explain is a word (`trước đây`, `chúng tôi`, `tháng Một`, `vì vậy`) and
  is kept; one it cannot explain is a phrase (`của ông`, `không phải`, `lúc nào`) and is
  split so each word is tappable. The first version of this guard ignored the gloss
  condition and destroyed real words — `tôi` alone leads with "slave; domestic servant"
  — and the gloss condition is what makes it safe.

Guard 1 is why `tools/resegment.py` can repair stored articles for free: the guards are
in the reconciler, not in the model, so the stored *text* does not have to change for the
token map to be rebuilt correctly.

### 6. Token map — `pipeline/tokens.py`

One row per span: `surface`, underscore `form`, `[start_char, end_char)`, syllable
count, CEFR tag, provenance, ambiguity flag and candidates — plus **two** definition
columns, because the two languages have different sources and different lifetimes:

| Column | Source | Language |
|---|---|---|
| `definition_en` | `glosses` table in the dictionary (built by `tools/build_glosses.py`) | English |
| `definition_vi` | the pre-teach glosses from the simplification call | Vietnamese |
| `senses_en` | further English senses, JSON, for the sheet's secondary line | English |
| `definition` | whichever of the two is primary — English when present | — |

Definitions are resolved cheapest-first: the local dictionary (free, offline), then the
pre-teach glosses from the simplification call (free, already paid for), then
**nothing** — a NULL that the runtime fallback exists to fill. Ingest never makes a
per-word call; that would undo the point of batching.

Keeping the columns separate rather than merging them is what lets the reader show
English first with the Vietnamese gloss underneath, and lets a dictionary rebuild change
one language without disturbing the other. It also lifted stored-definition coverage
from 17% of tokens (pre-teach only) to 88%.

### 7. Storage

`storage/migrations/` is authoritative and additive; `db.migrate` applies unapplied
files and records them in `schema_migrations`. Nothing drops or recreates a table.

| Table | Holds |
|---|---|
| `articles` | one row per source article, `UNIQUE(source, guid)`, with attribution |
| `article_versions` | one simplification per `(article, level)`, with model + simplification prompt version + exercises prompt version + quality |
| `tokens` | the token map, indexed on `(version_id, start_char, end_char)`, with both definition languages |
| `preteach` | vocabulary and grammar points, keyed by version |
| `exercises` | comprehension questions and the writing task, keyed by version — one row per item, with the options already in their content-determined order |
| `lookup_cache` | fallback answers, so one context is never paid for twice |

`article_versions.quality` is `ok` or `syllable_mismatch`. The latter means the model's
grouping did not line up with the text at all, so it was discarded in favour of
underthesea alone — a visible flag rather than a silently mangled map.

Two prompt versions live on a version row rather than one, because the two calls have
different lifetimes: `prompt_version` for the simplification and
`exercises_prompt_version` for the practice content. Changing the questions must not
make the (far more expensive) rewrite look stale, and `--refresh-exercises` reads the
second column to find exactly the rows that need one more small call.

## Runtime

```
long-press / tap / drag-select
      │
      ▼
  character offset(s)
      │
      ▼
  syllable range
      │
      ├── an exact token with a definition ─────▶ highlight it, show the gloss
      │
      ├── otherwise: the exported lookup index ─▶ is (syl_start, syl_end) a
      │                                           dictionary span? ─▶ show the gloss
      │                                           "known, no gloss" ─▶ say so
      │
      ├── a token/range with no gloss anywhere
      │        └─ sentence_around(text, offset) ─▶ lookup_cache ─hit─▶ show
      │                                                  │miss
      ▼                                                  ▼
  handles: widen/narrow by syllable        POST {sentence, selection, cefr}  (optional)
                                                           │
                                                           ▼
                                                   cache locally, then show
```

The lookup index is the piece that makes an *arbitrary* range work. The dictionary never
reaches the browser — definitions are baked in per token — so before the index a range
the segmenter had not produced was a dead end. `pipeline/lookup.py` is run at export
time, where the dictionary is free to consult: every 2–6 syllable run in the article that
the dictionary knows, with its English gloss (~450–500 spans, a few tens of kilobytes).
The reader holds it as a `Map` keyed `"sylStart:sylEnd"`, so resolution is one lookup,
offline, with no server and no per-tap cost.

Nothing on the happy path leaves the device. `resolve_offset(..., allow_llm=False)`
returns the purely-local answer, which is what the UI asks for first; only an empty
result escalates.

The fallback payload is **one sentence plus the selection** — never the article — and
the answer is keyed by a hash of `(sentence, selection)` so repeat taps are free.

## Cost model

| Work | Frequency | Cost |
|---|---|---|
| simplification + segmentation | once per (article, level) | 1 model call |
| comprehension questions + writing task | once per (article, level) | 1 shorter call (skippable: `--no-exercises`) |
| segmentation, reconciliation, token map | once per (article, level) | local |
| English glosses for the token map | once, from the local dictionary | free |
| Vietnamese glosses for the token map | once, reusing that same call's output | free |
| the offline lookup index | once, at export | free (local dictionary) |
| practice content for an article already ingested | per `--refresh-exercises` run | 1 short call |
| re-segmenting a stored article after a guard change | per `tools/resegment.py` run | **free — no call at all** |
| an item too short to be a reading exercise | once | **no call — skipped first** |
| a tap with a stored definition, or a range in the lookup index | every tap | free |
| a tap with no stored definition anywhere | rare, then cached | 1 short call |

Prompt caching is designed in: system prompts are constants, variable content follows.

The practice call is the one that changes the arithmetic: it doubles the number of calls
per article but costs far less than the rewrite, because it reads the finished simplified
text and writes no article. It is also the reason a prompt change to the questions costs
ten small calls rather than ten rewrites.

Nothing runs on a schedule. There is no cron and no timer in the app: the paid-relevant
steps — `build_glosses`, `ingest`, `build_site` — are commands a human types, and opening
the reader only fetches a committed bundle. The one button that can spend money is the
reader's **↻ Cập nhật**, which is off until `REFRESH_ENDPOINT` is set, asks for a
password, and dispatches *the same command* on GitHub Actions — where the key lives and
where the off-peak guard still refuses a peak-hour run. That keeps the property this
section is about: no page load, and no schedule, ever spends a token by itself.

## Phase 2 — one of the two is now built

`refresh-worker/` and `.github/workflows/reading-refresh.yml` are the re-ingest trigger:
the Worker holds the GitHub token and the password, the workflow holds the DeepSeek key
and runs the same CLI a human would. That is a transport layer over `run_ingest`, exactly
as planned — no second implementation.

Still unbuilt, and still deliberately so: **the HTTP wrapper for the disambiguation
fallback**. `pipeline/resolve.py` implements the one-sentence lookup and is unit-tested,
and `web/reader.js` already POSTs to a configurable `DISAMBIGUATE_ENDPOINT`. The lookup
index means it fires far less often than it used to, which is the argument for leaving it
alone until something actually needs it. The same Worker is the natural home for it.

## Phase 2 — one of the two is now built

`refresh-worker/` and `.github/workflows/reading-refresh.yml` are the re-ingest trigger:
the Worker holds the GitHub token and the password, the workflow holds the DeepSeek key
and runs the same CLI a human would. That is a transport layer over `run_ingest`, exactly
as planned — not a second implementation.

Still unbuilt, and still deliberately so: **the HTTP wrapper for the disambiguation
fallback**. `pipeline/resolve.py` implements the one-sentence lookup and is unit-tested,
and `web/reader.js` already POSTs to a configurable `DISAMBIGUATE_ENDPOINT`. The lookup
index means it fires far less often than it used to, which is the argument for leaving it
alone until something actually needs it. The same Worker is the natural home for it.

## Testing

`313 tests`. The reconciler and token-map builder are unit-tested against hand-written
segmentations, because their invariants (spans tile the syllables; offsets address the
exact substring) are what the reader depends on. One integration test drives the whole
pipeline over a fixture article with DeepSeek stubbed, so the pipeline's own logic is
under test rather than the model's.

Four files exist because a bug got through without them. `test_fetch_full.py` covers
the container-scoring shapes that actually occur (per-paragraph wrappers, navigation
furniture, link farms, pages with no prose at all); `test_glosses.py` pins the ordering
rules that decide which English sense a tap shows first, against hand-written records
rather than the 79 MB source; `test_deepseek.py` covers the streamed-response reader
against canned SSE frames — the path every article now takes, and the one where
reasoning text leaking into the article, or usage going unreported, would be invisible.
`test_text.py` covers paragraph preservation, the rewrite-change measurement and the
contiguity guard, which the reader and the quality guard depend on.

The guard tests are the ones that carry a measurement rather than an intention:
`test_reconcile.py` pins that a glossed run of function words (`trước đây`, `chúng tôi`)
is still one word while an unglossed one (`của ông`) is not, because the first version of
that rule failed exactly there and the corpus was the only thing that showed it.

The suite uses the installed `underthesea` model but no network: RSS, article and page
fixtures are saved files. That is why the module's tests still mean something on a day
the news sites change their markup.
