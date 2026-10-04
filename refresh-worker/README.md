# Refresh endpoint — the "↻ Cập nhật" button, behind a password

The reading page is a static site, and ingesting costs money: it calls DeepSeek and
needs the API key. **No key can live on that page** — anything the page holds, a
visitor can read with view-source. So the page holds nothing but a password prompt,
and the run is started by something that does hold the key: a GitHub Actions
workflow, with the key in a repository secret.

This directory is the small piece in between. It exists so that pressing a button is
possible without opening GitHub.

```
browser (reading page)                Cloudflare Worker            GitHub Actions
  │  POST {password, level, limit} ──▶ │  check password            │
  │                                   │  count today's runs        │
  │                                   │  dispatch workflow ──────▶ │  ingest
  │  ◀──── {ok, run_url}              │                            │  build_site
  │                                   │      (GITHUB_TOKEN)        │  commit + push
  ▼                                                                ▼
password prompt only                                  Pages redeploys a few minutes later
```

## What is protected, and by what

| Thing | Where it lives | Who can see it |
|---|---|---|
| `DEEPSEEK_API_KEY` | GitHub Actions secret | GitHub, and the run logs' environment |
| `GITHUB_TOKEN` (Actions: write) | Worker secret | the Worker only |
| `REFRESH_PASSWORD` | Worker secret | you — it is typed into the page, never stored by it |
| the dictionary + database | Cloudflare R2 | private bucket, same credentials as the audio |

The password protects **spending**, not data. Treat it as a secret, and rely on the
rate limit for the day you leak it.

## Deploy

```bash
cd refresh-worker
npm install -g wrangler          # or use npx

npx wrangler kv namespace create RATE_LIMIT     # paste the id into wrangler.toml

npx wrangler secret put REFRESH_PASSWORD        # a long random passphrase
npx wrangler secret put GITHUB_TOKEN            # fine-grained PAT: Actions: read+write

npx wrangler deploy
```

Then give the page the endpoint it should POST to — `REFRESH_ENDPOINT` in
`reading/web/reader.js`:

```js
const REFRESH_ENDPOINT = 'https://reading-refresh.<your-subdomain>.workers.dev';
```

Until that constant is set the button is hidden and nothing on the page is capable of
spending anything. Reload the page after changing it.

### The GitHub token

Fine-grained, **only this repository**, and only **Actions: read and write**. Nothing
else — the workflow itself has `contents: write` for the commit, using the run's own
token rather than this one.

### Make it stronger, for free

Cloudflare Access (Zero Trust) can sit in front of this Worker and require an email
one-time code before the password is even asked for. That replaces "a shared secret on
the open internet" with a real login, at no cost for a single user, and the Worker
needs no change.

## What the workflow needs, once

The `reading-refresh` workflow restores two files from R2 before it runs and saves them
back afterwards:

* `reading/data/reading.sqlite3` — **ingest's dedupe lives here.** With an empty
  database every run re-simplifies the whole feed and pays for all of it again.
* `reading/data/dictionary.sqlite3` — the compound dictionary and English glosses.
  Without it segmentation loses its hard constraint and quality drops silently; the
  workflow rebuilds it (`build_dictionary`, `build_glosses`) if the bucket has none.

Both use the same `R2_ENDPOINT` / `R2_BUCKET` / `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY` secrets the audio installer already uses. Upload them once from
a machine that has them:

```bash
python -m reading.tools.r2_sync put      # needs boto3 + the R2 env vars
```

## Limits worth knowing

* **The off-peak guard still rules.** A run started during DeepSeek's peak hours
  (01:00–04:00 and 06:00–10:00 UTC, Mon–Fri) is refused by the pipeline, costs nothing,
  and says when off-peak resumes. The workflow surfaces that in its log.
* **A run takes minutes**, because the model reasons before it answers. The Worker
  returns as soon as the run is *dispatched*; the page tells you to reload later.
* **A public repository means Actions minutes are free**, so a refresh costs only
  DeepSeek tokens. `limit` caps how many articles one press may add.
* **You do not need this Worker to refresh.** The Actions tab → *Refresh reading
  articles* → *Run workflow* is the same thing with GitHub's own authentication. The
  Worker exists to make it a button inside the app.
