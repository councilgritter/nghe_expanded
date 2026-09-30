# Migration plan — move audio to Cloudflare R2

**Status: done.** The migration below shipped. Audio is served from R2, `audio/` is
gitignored, and the install Action uploads approved recordings to R2 instead of
committing them. This document is kept as the design record.

> **What this means for this repo.** Both `councilgritter/nghe` and
> `councilgritter/nghe_expanded` read the **same R2 bucket**, so they share one set of
> recordings automatically. Only `nghe` runs the installer on a schedule — it is the
> sole writer, which is what keeps the two from racing on the same clip keys. The
> Action here is `workflow_dispatch`-only for that reason.
>
> One step below is **not** done: reclaiming the ~73 MB of AI audio still in git
> history (the optional `git filter-repo` rewrite at the end).

## Why

Nghe is a static site, and GitHub Pages hosts it fine. The problem isn't the host —
it's that **~12,000 audio clips live in git**, and the long-term plan is for *every*
clip to be re-recorded by humans, with re-records over time. Git never deletes: every
replaced clip is a new permanent blob, so a continuously re-recorded library would
bloat the repo without limit.

**Object storage is the right home for audio that churns.** Cloudflare R2 keyed by
`<region>/<clip>.mp3` means each re-record is just an overwrite at the same key — zero
history cost, repo stays tiny forever.

## Cost

Free at this scale. R2 free tier ≈ 10 GB storage, ~1M writes/mo, ~10M reads/mo, and
**zero egress** (no bandwidth charges — the reason it beats S3 for audio). Nghe's audio
is ~120 MB (one take each) / ~240 MB if we keep AI + human. Caveat: **R2 asks for a
card** to enable, even on the free plan; it won't charge within the limits.

## Target architecture

```
app + data.json + scripts   →  git (tiny, rarely changes)
audio (AI now, human later)  →  R2 bucket, keyed <region>/<clip>.mp3
flag / record UI             →  posts to the Apps Script collector   (unchanged)
review + approve             →  Apps Script review page              (unchanged)
install Action               →  uploads approved takes straight to R2 (overwrite)
generation (Kaggle)          →  uploads new clips straight to R2, not git
the app                      →  reads every clip from R2 by <region>/<clip>
```

Nothing already built is wasted — the collector, review page, and accent-tagging all
carry over. Only three things get reworked: **audio → R2**, **install Action → R2
upload**, **add cache-versioning**. The app barely changes: it already fetches by
`clip_id` + `region`, so an AI clip and its human replacement share the same URL and
the swap is invisible.

Kaggle is **not** needed for the migration itself (it was only the GPU for generation).
The one-time upload runs from the PC, where the clips already sit in the local clone.

## Two decisions to make first

1. **Keep the AI originals, or overwrite in place?**
   - *Overwrite in place (simplest):* an approved human take replaces the AI clip at its
     key; the AI one is gone. If a human clip is ever bad/pulled, the app falls back to the
     device's browser voice.
   - *Keep AI as a fallback (recommended):* human takes under `<region>/<clip>.mp3`, AI
     copies under `ai/<region>/<clip>.mp3`. A pulled human take falls back to the AI
     original instead of the robotic browser voice, and you can A/B them. ~240 MB — trivial
     on R2's free tier.

2. **How re-recorded clips refresh in users' caches.** The service worker currently caches
   each clip **forever** (right for static AI clips, wrong once clips change). Pick one:
   - *Versioned URLs (most correct):* a per-clip revision so a replaced clip's URL changes
     (`…mp3?v=<rev>`); the Action bumps the rev, the app appends it. Small addition to
     `data.json`.
   - *Stale-while-revalidate (simplest):* SW serves the cached clip and fetches the fresh
     one in the background; users get the new take one play later.

## Steps (in an order that never leaves the site without audio)

**Cloudflare side (yours; needs the account + card):**
1. Create an R2 bucket, e.g. `nghe-audio`.
2. Upload `audio/` keeping the `<region>/<clip>.mp3` layout. 12,000 small files → use
   **rclone** from the PC (not the dashboard):
   - `rclone config` → new remote, type **S3**, provider **Cloudflare R2**, your account
     ID + an R2 API token (Access Key / Secret), endpoint
     `https://<accountid>.r2.cloudflarestorage.com`.
   - `rclone copy "C:\Users\ADMIN\Documents\GitHub\nghe\audio" nghe-r2:nghe-audio --transfers=32`
   - (`aws s3 sync ./audio s3://nghe-audio --endpoint-url https://<accountid>.r2.cloudflarestorage.com` also works.)
3. Make it reachable: enable the bucket's public `r2.dev` URL, or attach a **custom domain**
   like `audio.<yourdomain>` (nicer + cacheable). Result: a base URL, e.g.
   `https://audio.nghe.app`. Set a sensible `Cache-Control` on the objects.
4. Verify one clip plays in a browser, e.g. `https://audio.nghe.app/south/v06131.mp3`.

**Code side (Claude does these, once the base URL exists):**
5. Add an `AUDIO_BASE` constant and point `clipUrl()` at it (relative → absolute). One
   small change; empty `AUDIO_BASE` keeps today's behaviour, so it's a safe toggle.
6. Update `sw.js` to cache the CDN origin too (handle cross-origin/opaque responses so
   clips still work offline — no CORS config needed), and switch audio to versioned or
   stale-while-revalidate per decision 2.
7. Push and **test clips play from R2** while the git copy is still there as a safety net.
8. Once confirmed: `git rm -r audio/` so the repo stops carrying ~120 MB.
9. Rework `scripts/install_recordings.py` + the workflow to **upload approved takes to R2**
   (S3 `PUT`) instead of committing to git; add the R2 API keys as Action secrets; bump the
   clip version if using versioned URLs.
10. Update `build_audio.ipynb` / `generate_clips.py` so future generation uploads to R2
    instead of committing — AI audio then never touches git either.

**Optional later:** the AI blobs remain in git history after step 8. Reclaiming that space
is a one-time history rewrite (`git filter-repo`) — disruptive, not urgent.

## What stays the same

The whole flag → collector → review → approve chain, accent tagging, the app's drill
logic, the first-run accent picker. Migrating audio doesn't touch any of it.
