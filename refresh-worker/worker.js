/**
 * Refresh endpoint for the reading page — the "↻ Cập nhật" button, behind a password.
 *
 * WHY THIS EXISTS AT ALL
 * ----------------------
 * Ingesting costs money and needs the DeepSeek API key, and the reading page is a
 * static site: there is nowhere on the page a key could live that a visitor could not
 * read with view-source.  So the page holds nothing but a password prompt, and this
 * Worker holds the token that starts the run.  The password protects *spending*, not
 * data, which is why it is rate-limited as well as checked.
 *
 * WHAT IT DOES
 * ------------
 *   POST /  {password, level?, limit?}  ->  {ok: true, run_url, run_id}
 *   GET  /                             ->  {ok: true}  (a liveness check)
 *
 * On success it dispatches the `reading-refresh` GitHub Actions workflow with
 * `mode=ingest`.  The run itself — off-peak guard, dedupe, export, commit — lives in
 * `.github/workflows/reading-refresh.yml`; this file only decides *whether* to start it.
 *
 * CONFIGURATION (wrangler secret put <NAME>)
 * -----------------------------------------
 *   REFRESH_PASSWORD  the password the reader types.  Use a long random passphrase:
 *                     it is the only thing between the public and your API spend.
 *   GITHUB_TOKEN      fine-grained PAT, repository = this repo, permission
 *                     "Actions: read and write" — and nothing else.
 *   GITHUB_REPO       "owner/name", e.g. councilgritter/nghe_expanded
 *   GITHUB_REF        optional branch, default "main"
 *   ALLOWED_ORIGIN    optional, e.g. "https://councilgritter.github.io".  Set it: it
 *                     stops other sites from using your Worker as an oracle.
 *   MAX_RUNS_PER_DAY  optional integer, default 6.
 *
 * RATE LIMITING is per-UTC-day using a KV namespace bound as RATE_LIMIT.  It is a
 * spend cap, not a security boundary: it bounds what one leaked password can cost.
 */

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

function cors(env) {
  return {
    'access-control-allow-origin': env.ALLOWED_ORIGIN || '*',
    'access-control-allow-methods': 'POST, GET, OPTIONS',
    'access-control-allow-headers': 'content-type',
  };
}

function reply(body, status, env) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...cors(env) },
  });
}

/** Constant-time string comparison, so a wrong password leaks nothing by timing. */
function sameSecret(a, b) {
  const left = new TextEncoder().encode(a || '');
  const right = new TextEncoder().encode(b || '');
  // Fold the length difference into the accumulator instead of returning early.
  let diff = left.length ^ right.length;
  const max = Math.max(left.length, right.length);
  for (let i = 0; i < max; i++) diff |= (left[i] || 0) ^ (right[i] || 0);
  return diff === 0;
}

const today = () => new Date().toISOString().slice(0, 10);

async function bumpCounter(env, day) {
  if (!env.RATE_LIMIT) return { used: 0, limit: 0 };
  const key = `runs:${day}`;
  const current = parseInt((await env.RATE_LIMIT.get(key)) || '0', 10);
  await env.RATE_LIMIT.put(key, String(current + 1), { expirationTtl: 172800 });
  return { used: current + 1, limit: parseInt(env.MAX_RUNS_PER_DAY || '6', 10) };
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: cors(env) });
    }
    if (request.method === 'GET') {
      return reply({ ok: true, service: 'reading-refresh' }, 200, env);
    }
    if (request.method !== 'POST') {
      return reply({ ok: false, error: 'method not allowed' }, 405, env);
    }

    if (!env.REFRESH_PASSWORD || !env.GITHUB_TOKEN || !env.GITHUB_REPO) {
      return reply({ ok: false, error: 'endpoint is not configured' }, 500, env);
    }

    let payload = {};
    try {
      payload = await request.json();
    } catch {
      return reply({ ok: false, error: 'expected a JSON body' }, 400, env);
    }

    if (!sameSecret(String(payload.password || ''), env.REFRESH_PASSWORD)) {
      // One message for every rejection: no hint about how close it was.
      return reply({ ok: false, error: 'wrong password' }, 401, env);
    }

    const { used, limit } = await bumpCounter(env, today());
    if (limit && used > limit) {
      return reply(
        { ok: false, error: `daily limit reached (${limit} runs per day)` },
        429,
        env,
      );
    }

    // A spend cap on the run itself, not just the number of runs.
    const limitArticles = Math.max(1, Math.min(parseInt(payload.limit, 10) || 5, 20));
    const level = String(payload.level || 'B1').toUpperCase();

    const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/reading-refresh.yml/dispatches`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: 'application/vnd.github+json',
        'x-github-api-version': '2022-11-28',
        'user-agent': 'reading-refresh-worker',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        ref: env.GITHUB_REF || 'main',
        inputs: { mode: 'ingest', level, limit: String(limitArticles), allow_peak: 'false' },
      }),
    });

    if (!response.ok) {
      // The GitHub error is reported but the token never is.
      const detail = await response.text();
      return reply(
        { ok: false, error: `github returned ${response.status}: ${detail.slice(0, 200)}` },
        502,
        env,
      );
    }

    return reply(
      {
        ok: true,
        run_url: `https://github.com/${env.GITHUB_REPO}/actions/workflows/reading-refresh.yml`,
        runs_today: used,
        limit_per_day: limit,
      },
      200,
      env,
    );
  },
};
