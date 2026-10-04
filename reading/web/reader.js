// Reading practice — reader page logic.
//
// Extracted from index.html so the page carries no inline script (audit rule
// security/script-unsafe-inline). Loaded as <script src="reader.js">.
//
// Taps resolve from the token map already on the device; the only network calls
// are fetching the bundle and the optional DISAMBIGUATE_ENDPOINT fallback below.

/* ---------- config ----------
   Static artifacts exported by reading/tools/build_site.py. */
const DATA_BASE = '../data/site';

/* Optional disambiguation endpoint. Leave empty to run fully offline: taps then
   resolve only from the token map, and an unexplained word simply says so.

   When set, it is POSTed {sentence, selection, cefr} and should return
   {form, definition, cefr}. The pipeline's reading/pipeline/resolve.py already
   implements exactly this, so wrapping it in a route is all that is needed.
   Never put a DeepSeek key in this file. */
const DISAMBIGUATE_ENDPOINT = '';

const LONG_PRESS_MS = 380;   // hold this long to resolve a word
const CACHE_PREFIX  = 'vnread.def.';

/* ---------- state ---------- */
const S = {
  index: null,
  bundle: null,
  level: localStorage.getItem('vnread.level') || '',
  sel: null,            // {sylStart, sylEnd}
};

const $ = id => document.getElementById(id);

/* ---------- tiny helpers ---------- */
function hash(str){            // djb2 — a cache key, not a security primitive
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h * 33) ^ str.charCodeAt(i)) >>> 0;
  return h.toString(36);
}
function cacheGet(k){ try { return JSON.parse(localStorage.getItem(CACHE_PREFIX + k)); } catch { return null; } }
function cacheSet(k, v){ try { localStorage.setItem(CACHE_PREFIX + k, JSON.stringify(v)); } catch {} }

/* The sentence around an offset — the whole payload of a fallback lookup. */
function sentenceAround(text, offset){
  const stop = '.,!?…\n';
  let a = 0;
  for (let i = offset - 1; i >= 0; i--) if (stop.includes(text[i])) { a = i + 1; break; }
  let b = text.length;
  for (let i = offset; i < text.length; i++) if (stop.includes(text[i])) { b = i + 1; break; }
  return text.slice(a, b).trim();
}

/* The syllable index containing a character offset. */
function syllableAt(text, syllables, offset){
  for (let i = 0; i < syllables.length; i++)
    if (syllables[i].s <= offset && offset < syllables[i].e) return i;
  for (let i = syllables.length - 1; i >= 0; i--) if (syllables[i].e <= offset) return i;
  return 0;
}

/* ---------- loading ---------- */
async function boot(){
  try{
    const r = await fetch(DATA_BASE + '/index.json', {cache:'no-cache'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    S.index = await r.json();
  } catch (e){
    $('listHost').innerHTML = '<div class="err"><b>Không tải được dữ liệu.</b><br>' +
      'Hãy chạy <code>python -m reading.tools.build_site</code> để xuất bài đọc, ' +
      'rồi mở trang này qua một máy chủ HTTP (không dùng <code>file://</code>).<br>' +
      '<span style="color:var(--dim)">' + e.message + '</span></div>';
    return;
  }
  const levels = S.index.levels || [];
  if (!S.level || !levels.includes(S.level)) S.level = levels[0] || '';
  $('level').innerHTML = levels.map(l =>
    '<option' + (l === S.level ? ' selected' : '') + '>' + l + '</option>').join('');
  if (!levels.length){
    $('listHost').innerHTML = '<div class="err">Chưa có bài đọc nào. ' +
      'Hãy chạy <code>python -m reading.pipeline.ingest</code>.</div>';
    return;
  }
  renderList();
}

function renderList(){
  const items = (S.index.articles || []).filter(a => a.cefr_level === S.level);
  $('readHost').hidden = true;
  $('back').hidden = true;
  $('listHost').hidden = false;
  if (!items.length){ $('listHost').innerHTML = '<p style="color:var(--dim)">Không có bài ở trình độ này.</p>'; return; }
  $('listHost').innerHTML = items.map(a =>
    '<a href="#" data-file="' + a.file + '">' +
      '<div class="t">' + esc(a.title) + '</div>' +
      '<div class="m">' +
        '<span class="badge src-' + a.source + '">' + a.source.toUpperCase() + '</span>' +
        '<span>' + (a.published_at || '').slice(0, 10) + '</span>' +
        '<span>' + a.tokens + ' từ</span>' +
        (a.ambiguous ? '<span style="color:var(--warn)">' + a.ambiguous + ' chỗ chia chưa chắc</span>' : '') +
        (a.quality !== 'ok' ? '<span style="color:var(--warn)">cần xem lại</span>' : '') +
      '</div></a>').join('');
  $('listHost').querySelectorAll('a').forEach(a =>
    a.addEventListener('click', e => { e.preventDefault(); openArticle(a.dataset.file); }));
}

function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

async function openArticle(file){
  const r = await fetch(DATA_BASE + '/' + file, {cache:'no-cache'});
  S.bundle = await r.json();
  S.sel = null;
  closeSheet();
  renderReader();
}

/* ---------- rendering ---------- */
function renderReader(){
  const b = S.bundle;
  $('listHost').hidden = true;
  $('readHost').hidden = false;
  $('back').hidden = false;

  $('title').textContent = b.article.title;
  $('sub').innerHTML =
    '<span class="badge src-' + b.article.source + '">' + b.article.source.toUpperCase() + '</span> ' +
    (b.article.published_at || '').slice(0, 10) +
    ' · trình độ ' + b.version.cefr_level +
    ' · <a href="' + esc(b.article.source_url) + '" target="_blank" rel="noopener">bài gốc</a>';

  renderPreteach(b);
  renderText(b);

  // The attribution notice travels with the article; both sources require it.
  $('foot').innerHTML = esc(b.article.attribution) +
    (b.version.quality !== 'ok'
      ? '<br><span style="color:var(--warn)">Cách chia từ của bài này chưa chắc chắn — ' +
        'hãy kiểm tra lại, và nhấn giữ một từ để xem cách chia khác.</span>' +
        (b.version.notes
          ? '<br><span style="color:var(--dim)">' + esc(b.version.notes) + '</span>'
          : '')
      : '');
}

function renderPreteach(b){
  const v = b.preteach.vocab || [], g = b.preteach.grammar || [];
  if (!v.length && !g.length){ $('preBox').hidden = true; return; }
  $('preBox').hidden = false;
  $('preSummary').textContent =
    'Cần biết trước: ' + v.length + ' từ vựng, ' + g.length + ' điểm ngữ pháp';
  const block = (label, rows) => rows.length ? '<h4>' + label + '</h4>' + rows.map(r =>
      '<div class="row"><div class="term">' + esc(r.term.replace(/_/g,' ')) +
      (r.cefr ? '<span class="lv">' + esc(r.cefr) + '</span>' : '') + '</div>' +
      (r.gloss_en ? '<div class="gloss">' + esc(r.gloss_en) + '</div>' : '') +
      (r.gloss ? '<div class="gloss vi">' + esc(r.gloss) + '</div>' : '') +
      (r.example ? '<div class="eg">' + esc(r.example) + '</div>' : '') +
      '</div>').join('') : '';
  $('preIn').innerHTML = block('Từ vựng', v) + block('Ngữ pháp', g);
}

/* Render the simplified text, wrapping every token in its own span.
   Paragraph breaks are re-created from the blank lines so offsets stay exact. */
function renderText(b){
  const host = $('text');
  host.innerHTML = '';
  const text = b.text, tokens = b.tokens;

  let pStart = 0;
  const paragraphs = [];
  for (let i = 0; i <= text.length; i++){
    if (i === text.length || (text[i] === '\n' && text[i+1] === '\n')){
      if (i > pStart) paragraphs.push([pStart, i]);
      pStart = i + 2;
      i++;
    }
  }
  if (!paragraphs.length) paragraphs.push([0, text.length]);

  for (const [a, z] of paragraphs){
    const p = document.createElement('p');
    p.dataset.pStart = a;
    let cursor = a;
    for (const t of tokens){
      if (t.end <= a || t.start >= z) continue;
      if (t.start > cursor) p.appendChild(document.createTextNode(text.slice(cursor, t.start)));
      const span = document.createElement('span');
      span.className = 'w' + (t.ambiguous ? ' amb' : '');
      span.dataset.wordId = t.id;
      span.dataset.sylStart = t.syl_start;
      span.dataset.sylEnd = t.syl_end;
      span.textContent = text.slice(t.start, t.end);
      p.appendChild(span);
      cursor = t.end;
    }
    if (cursor < z) p.appendChild(document.createTextNode(text.slice(cursor, z)));
    host.appendChild(p);
  }
}

/* ---------- selection ---------- */
function tokenBySyllables(i, j){
  return S.bundle.tokens.find(t => t.syl_start === i && t.syl_end === j) || null;
}
function tokensInside(i, j){
  return S.bundle.tokens.filter(t => t.syl_start >= i && t.syl_end <= j);
}

function paint(){
  const host = $('text');
  host.querySelectorAll('.w.on').forEach(el => el.classList.remove('on'));
  if (!S.sel) return;
  for (const t of tokensInside(S.sel.sylStart, S.sel.sylEnd)){
    const el = host.querySelector('.w[data-word-id="' + t.id + '"]');
    if (el) el.classList.add('on');
  }
}

function selectSyllables(i, j){
  const n = S.bundle.syllables.length;
  i = Math.max(0, Math.min(i, n - 1));
  j = Math.max(i + 1, Math.min(j, n));
  S.sel = { sylStart: i, sylEnd: j };
  paint();
  showSelection();
}

function selectByWordId(id, reason){
  const t = S.bundle.tokens.find(x => x.id === id);
  if (!t) return;
  selectSyllables(t.syl_start, t.syl_end);
}

/* select the syllable under a raw character offset (used when the tap landed in
   a gap between tokens — exactly the case the fallback exists for) */
function selectByOffset(offset){
  const i = syllableAt(S.bundle.text, S.bundle.syllables, offset);
  selectSyllables(i, i + 1);
}

/* ---------- the definition sheet ---------- */
let lastLookup = null;

function closeSheet(){ $('sheet').classList.remove('open'); }

function selectionText(){
  const syl = S.bundle.syllables;
  const a = syl[S.sel.sylStart].s;
  const b = syl[S.sel.sylEnd - 1].e;
  return {start: a, end: b, surface: S.bundle.text.slice(a, b)};
}

/* The definition area: English meaning first, the Vietnamese gloss underneath.
   Both come from the bundle — the English from the local Việt→Anh dictionary, the
   Vietnamese from the model's pre-teach list — so this costs nothing at tap time. */
function paintDefinition(exact, fallbackText){
  const en = exact && exact.definition_en;
  const vi = exact && exact.definition_vi;
  const senses = (exact && exact.senses_en) || [];
  const primary = exact && exact.definition;

  const parts = [];
  if (en){
    parts.push('<div class="en">' + esc(en) + '</div>');
    if (senses.length)
      parts.push('<div class="alt">' + senses.map(esc).join(' · ') + '</div>');
    if (vi) parts.push('<div class="vi">' + esc(vi) + '</div>');
  } else if (vi){
    parts.push('<div class="en">' + esc(vi) + '</div>');
  } else if (primary){
    // An older bundle, or a cached fallback answer, carries one string only.
    parts.push('<div class="en">' + esc(primary) + '</div>');
  } else {
    parts.push('<div class="none">' + esc(fallbackText || '') + '</div>');
  }
  $('shDef').innerHTML = parts.join('');
}

function showSelection(){
  const {start, end, surface} = selectionText();
  const exact = tokenBySyllables(S.sel.sylStart, S.sel.sylEnd);
  const inside = tokensInside(S.sel.sylStart, S.sel.sylEnd);

  $('shForm').textContent = surface;
  $('shMeta').textContent =
    (S.sel.sylEnd - S.sel.sylStart) + ' âm tiết' +
    (exact ? '' : ' · chia lại') +
    (exact && exact.cefr ? ' · ' + exact.cefr : '');
  $('shCand').hidden = true;
  $('shNote').hidden = true;
  $('shActs').innerHTML = '';

  // 1. An exact token with a definition — no network, ever.
  if (exact && (exact.definition_en || exact.definition_vi || exact.definition)){
    paintDefinition(exact);
    showCandidates(exact);
    addHandleButtons();
    openSheet();
    return;
  }

  // 2. A token exists but carries no gloss, or the range was re-cut: try the
  //    local cache before spending anything.
  const sentence = sentenceAround(S.bundle.text, start);
  lastLookup = {sentence, selection: surface};
  const key = hash(sentence + '\u001f' + surface);
  const hit = cacheGet(key);
  if (hit){
    paintDefinition(null, hit.definition || '');
    $('shMeta').textContent += ' · đã lưu';
    showCandidates(exact);
    addHandleButtons();
    openSheet();
    return;
  }

  paintDefinition(
    null,
    (exact || inside.length)
      ? 'Chưa có nghĩa cho từ này.'
      : 'Chỗ này không nằm trong từ nào đã chia.'
  );
  showCandidates(exact);
  addHandleButtons();

  // 3. The one paid path: a single sentence, then cached locally.
  if (DISAMBIGUATE_ENDPOINT){
    askDisambiguation(sentence, surface, key);
  } else {
    $('shNote').hidden = false;
    $('shNote').textContent =
      'Chưa cấu hình endpoint tra cứu. Đặt DISAMBIGUATE_ENDPOINT trong reading/web/reader.js ' +
      'để gửi một câu ngữ cảnh đi tra nghĩa.';
  }
  openSheet();
}

function showCandidates(exact){
  if (!exact || !exact.ambiguous || !exact.candidates || exact.candidates.length < 2) return;
  $('shCand').hidden = false;
  $('shCand').textContent =
    'Cách chia khác: ' + exact.candidates
      .map(c => c.form.replace(/_/g, ' ') + ' (' + c.syllables + ' âm tiết)')
      .join(' · ');
}

function addHandleButtons(){
  const syl = S.bundle.syllables.length;
  const mk = (label, title, fn, disabled) => {
    const b = document.createElement('button');
    b.textContent = label; b.title = title;
    b.disabled = !!disabled;
    if (disabled) b.style.opacity = .4;
    b.addEventListener('click', fn);
    $('shActs').appendChild(b);
  };
  // Selection handles: the remedy when segmentation was wrong.
  mk('⇤ rộng trái',  'Thêm một âm tiết về bên trái',  () => selectSyllables(S.sel.sylStart - 1, S.sel.sylEnd), S.sel.sylStart <= 0);
  mk('⇥ rộng phải',  'Thêm một âm tiết về bên phải',  () => selectSyllables(S.sel.sylStart, S.sel.sylEnd + 1), S.sel.sylEnd >= syl);
  mk('⇥| hẹp phải',  'Bỏ âm tiết cuối',               () => selectSyllables(S.sel.sylStart, S.sel.sylEnd - 1), S.sel.sylEnd - S.sel.sylStart <= 1);
  mk('|⇤ hẹp trái',  'Bỏ âm tiết đầu',                () => selectSyllables(S.sel.sylStart + 1, S.sel.sylEnd), S.sel.sylEnd - S.sel.sylStart <= 1);
}

async function askDisambiguation(sentence, selection, key){
  $('shDef').textContent = 'Đang tra…';
  try{
    const r = await fetch(DISAMBIGUATE_ENDPOINT, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({sentence, selection, cefr: S.bundle.version.cefr_level}),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    paintDefinition(null, d.definition || 'Không tìm thấy nghĩa phù hợp.');
    if (d.form) $('shForm').textContent = d.form.replace(/_/g, ' ');
    if (d.cefr) $('shMeta').textContent += ' · ' + d.cefr;
    cacheSet(key, {definition: d.definition || '', form: d.form || '', cefr: d.cefr || ''});
  } catch (e){
    $('shDef').textContent = 'Không tra được nghĩa.';
    $('shNote').hidden = false;
    $('shNote').textContent = e.message;
  }
}

function openSheet(){ $('sheet').classList.add('open'); }

/* ---------- long-press wiring ----------
   Long-press is detected from pointer events, but the *tap* action runs on
   `click`. Click fires for every input device (touch, mouse, keyboard, and
   synthetic clicks), whereas pointerup does not — and having the two paths
   separate removes the double-fire risk entirely. */
const host = $('text');
let pressTimer = null, longFired = false;

host.addEventListener('pointerdown', e => {
  const span = e.target.closest('.w');
  if (!span) return;
  longFired = false;
  clearTimeout(pressTimer);
  pressTimer = setTimeout(() => {
    longFired = true;
    selectByWordId(+span.dataset.wordId, 'long');
  }, LONG_PRESS_MS);
});
host.addEventListener('pointerup', () => clearTimeout(pressTimer));
host.addEventListener('pointercancel', () => clearTimeout(pressTimer));
host.addEventListener('pointerleave', () => clearTimeout(pressTimer));
host.addEventListener('contextmenu', e => { if (e.target.closest('.w')) e.preventDefault(); });

host.addEventListener('click', e => {
  // A long-press already resolved this word; swallow the click that follows it.
  if (longFired){ longFired = false; return; }
  const span = e.target.closest('.w');
  if (span){ selectByWordId(+span.dataset.wordId, 'tap'); return; }
  // The tap landed between tokens: resolve by raw character offset, which is the
  // case the single-sentence fallback exists for.
  const off = offsetFromPoint(e.clientX, e.clientY);
  if (off != null) selectByOffset(off);
});

/* Absolute character offset under a point, used for taps in the gaps. */
function offsetFromPoint(x, y){
  let node = null, off = 0;
  if (document.caretPositionFromPoint){
    const p = document.caretPositionFromPoint(x, y);
    if (p){ node = p.offsetNode; off = p.offset; }
  } else if (document.caretRangeFromPoint){
    const r = document.caretRangeFromPoint(x, y);
    if (r){ node = r.startContainer; off = r.startOffset; }
  }
  if (!node || node.nodeType !== 3) return null;
  const p = node.parentElement && node.parentElement.closest('p[data-p-start]');
  if (!p) return null;
  return (+p.dataset.pStart) + off;
}

/* ---------- wiring ---------- */
$('back').addEventListener('click', renderList);
$('sheetClose').addEventListener('click', closeSheet);
$('level').addEventListener('change', e => {
  S.level = e.target.value;
  localStorage.setItem('vnread.level', S.level);
  renderList();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); });

boot();
