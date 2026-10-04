// Reading practice — reader page logic.
//
// Extracted from index.html so the page carries no inline script (audit rule
// security/script-unsafe-inline). Loaded as <script src="reader.js">.
//
// Taps resolve from the token map already on the device; the only network calls are
// fetching the bundle, the optional DISAMBIGUATE_ENDPOINT fallback, and the optional
// password-gated refresh below.

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

/* Optional refresh endpoint — the "get new articles" button, behind a password.

   Leave empty to hide the button: ingesting costs money and needs the DeepSeek key,
   neither of which can live in a static page. When set, it is POSTed
   {password, level, limit} and should start a run somewhere that holds the key
   (a Cloudflare Worker dispatching the reading-refresh GitHub Action, for example).
   The password is what stands between the public and your API spend: it protects
   money, not data, so treat it as a secret and let the endpoint rate-limit.

   See refresh-worker/README.md for the deployment that pairs with this. */
const REFRESH_ENDPOINT = '';

const LONG_PRESS_MS = 380;   // hold this long to resolve a word
const DRAG_PX = 10;          // move this far and the gesture is a selection drag
const CACHE_PREFIX  = 'vnread.def.';
const WRITE_PREFIX  = 'vnread.write.';
const QUIZ_PREFIX   = 'vnread.quiz.';

/* ---------- state ---------- */
const S = {
  index: null,
  bundle: null,
  level: localStorage.getItem('vnread.level') || '',
  sel: null,            // {sylStart, sylEnd}
  lookup: new Map(),    // "sylStart:sylEnd" -> dictionary span from the bundle
  quiz: {},             // choice per question, so the score survives a reload
  writing: '',
  quizKey: '',
  writeKey: '',
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

function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

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

/* Absolute character offset under a point, used for taps in the gaps and for drags.

   The caret API reports an offset *within one text node*, and a paragraph holds many
   of them — the tokens are spans, and each span is its own text node. So the offset
   is converted to an absolute one by summing the text nodes that come before it in
   the paragraph. (Summing only the node's own offset, as this did at first, resolves
   a drag to the wrong syllable: it lands wherever the paragraph's *first* text node
   happens to be.) Nodes inside the paragraph's own play button are skipped, because
   the button's "▶" is not part of the article text the offsets refer to. */
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

  const base = +p.dataset.pStart;
  const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
  let seen = 0, current;
  while ((current = walker.nextNode())){
    if (current.parentElement && current.parentElement.closest('button')) continue;
    if (current === node) return base + seen + off;
    seen += current.data.length;
  }
  return base + off;
}

/* ---------- theme ---------- */
function paintThemeLabel(){
  if (window.NgheTheme) $('themeLabel').textContent = window.NgheTheme.label();
}
document.addEventListener('nghe:theme', paintThemeLabel);

/* ---------- text to speech ----------
   The device's own Vietnamese voice: no key, no server, works offline. Desktop
   browsers often ship no Vietnamese voice at all, so the controls say so rather
   than playing silence. */
const TTS = { voices: [], voice: null, rate: 1, reading: false };

function ttsSupported(){ return typeof window.speechSynthesis !== 'undefined'; }

function viVoices(){
  if (!ttsSupported()) return [];
  return (window.speechSynthesis.getVoices() || []).filter(v =>
    (v.lang || '').toLowerCase().replace('_','-').startsWith('vi'));
}

function loadVoices(){
  if (!ttsSupported()) return;
  TTS.voices = viVoices();
  const sel = $('voice');
  const remembered = localStorage.getItem('vnread.voice') || '';
  sel.innerHTML = TTS.voices.length
    ? TTS.voices.map(v => '<option' + (v.name === remembered ? ' selected' : '') + '>' +
        esc(v.name) + '</option>').join('')
    : '<option value="">— không có giọng tiếng Việt —</option>';
  TTS.voice = TTS.voices.find(v => v.name === sel.value) || TTS.voices[0] || null;
  const usable = !!TTS.voice;
  $('playAll').disabled = !usable;
  $('stopSpeak').disabled = !usable || !TTS.reading;
  $('playAll').title = usable
    ? 'Đọc toàn bài'
    : 'Máy này chưa cài giọng tiếng Việt (Cài đặt hệ thống → Giọng nói)';
}

function speak(text, opts){
  if (!ttsSupported() || !TTS.voice || !text) return null;
  const u = new SpeechSynthesisUtterance(text);
  u.voice = TTS.voice;
  u.lang = TTS.voice.lang || 'vi-VN';
  u.rate = TTS.rate;
  if (opts && opts.onBoundary) u.onboundary = opts.onBoundary;
  if (opts && opts.onEnd) u.onend = opts.onEnd;
  window.speechSynthesis.speak(u);
  return u;
}

function stopSpeaking(){
  if (!ttsSupported()) return;
  window.speechSynthesis.cancel();
  TTS.reading = false;
  clearSpeakingHighlight();
  $('stopSpeak').disabled = true;
}

function clearSpeakingHighlight(){
  document.querySelectorAll('.w.speaking').forEach(el => el.classList.remove('speaking'));
}

/* Read the whole article, paragraph by paragraph, highlighting the word being
   spoken where the browser reports boundaries (Chrome does; Safari does not). */
function readArticle(){
  if (!S.bundle || !TTS.voice) return;
  stopSpeaking();
  TTS.reading = true;
  $('stopSpeak').disabled = false;
  const paragraphs = [...$('text').querySelectorAll('p')];
  let index = 0;
  const next = () => {
    if (!TTS.reading || index >= paragraphs.length){
      TTS.reading = false;
      $('stopSpeak').disabled = true;
      clearSpeakingHighlight();
      return;
    }
    const p = paragraphs[index++];
    const start = +p.dataset.pStart;
    const end = +p.dataset.pEnd;
    // The raw slice, not p.textContent: the button label is not article text, and
    // trimming would shift every boundary offset the highlight maps from.
    const text = S.bundle.text.slice(start, end);
    if (!text.trim()){ next(); return; }
    speak(text, {
      onBoundary: e => highlightSpoken(start + e.charIndex),
      onEnd: next,
    });
  };
  next();
}

function highlightSpoken(offset){
  if (!S.bundle) return;
  const t = S.bundle.tokens.find(x => x.start <= offset && offset < x.end);
  clearSpeakingHighlight();
  if (!t) return;
  const el = $('text').querySelector('.w[data-word-id="' + t.id + '"]');
  if (el) el.classList.add('speaking');
}

/* ---------- loading ---------- */
async function boot(){
  paintThemeLabel();
  loadVoices();
  if (ttsSupported() && window.speechSynthesis.addEventListener)
    window.speechSynthesis.addEventListener('voiceschanged', loadVoices);
  if (REFRESH_ENDPOINT) $('refresh').hidden = false;

  try{
    const r = await fetch(DATA_BASE + '/index.json', {cache:'no-cache'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    S.index = await r.json();
  } catch (e){
    $('listHost').innerHTML = '<div class="err"><b>Không tải được dữ liệu.</b><br>' +
      'Hãy chạy <code>python -m reading.tools.build_site</code> để xuất bài đọc, ' +
      'rồi mở trang này qua một máy chủ HTTP (không dùng <code>file://</code>).<br>' +
      '<span style="color:var(--dim)">' + esc(e.message) + '</span></div>';
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
  stopSpeaking();
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
        (a.questions ? '<span>' + a.questions + ' câu hỏi</span>' : '') +
        (a.writing ? '<span>bài viết</span>' : '') +
        (a.ambiguous ? '<span style="color:var(--warn)">' + a.ambiguous + ' chỗ chia chưa chắc</span>' : '') +
        (a.quality !== 'ok' ? '<span style="color:var(--warn)">cần xem lại</span>' : '') +
      '</div></a>').join('');
  $('listHost').querySelectorAll('a').forEach(a =>
    a.addEventListener('click', e => { e.preventDefault(); openArticle(a.dataset.file); }));
}

async function openArticle(file){
  stopSpeaking();
  const r = await fetch(DATA_BASE + '/' + file, {cache:'no-cache'});
  S.bundle = await r.json();
  S.sel = null;
  S.lookup = new Map((S.bundle.lookup || []).map(e => [e.s + ':' + e.e, e]));
  // The stored answer is only meaningful against the options it was given, and the
  // options are permuted at export. The key carries a fingerprint of the question set
  // so a changed set starts fresh instead of marking the wrong option as chosen.
  S.quizKey = QUIZ_PREFIX + S.bundle.version.id + '.' + exercisesFingerprint(S.bundle.exercises);
  S.writeKey = WRITE_PREFIX + S.bundle.version.id;
  try { S.quiz = JSON.parse(localStorage.getItem(S.quizKey) || '{}'); } catch { S.quiz = {}; }
  try { S.writing = localStorage.getItem(S.writeKey) || ''; } catch { S.writing = ''; }
  closeSheet();
  renderReader();
}

/* A short fingerprint of the questions: positions and options both matter, because
   both are what a stored answer index refers to. */
function exercisesFingerprint(exercises){
  const mcq = (exercises && exercises.mcq) || [];
  if (!mcq.length) return 'none';
  return hash(mcq.map(q => q.q + '|' + (q.options || []).join('|')).join('\n'));
}

/* ---------- rendering ---------- */
function renderReader(){
  const b = S.bundle;
  $('listHost').hidden = true;
  $('readHost').hidden = false;
  $('back').hidden = false;
  $('tools').hidden = !ttsSupported();
  loadVoices();

  $('title').textContent = b.article.title;
  $('sub').innerHTML =
    '<span class="badge src-' + b.article.source + '">' + b.article.source.toUpperCase() + '</span> ' +
    (b.article.published_at || '').slice(0, 10) +
    ' · trình độ ' + b.version.cefr_level +
    ' · <a href="' + esc(b.article.source_url) + '" target="_blank" rel="noopener">bài gốc</a>';

  renderPreteach(b);
  renderText(b);
  renderExercises(b);

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
    p.dataset.pEnd = z;
    // One button per paragraph: reading a whole article aloud is a lot of speech,
    // and this is how a learner repeats a single paragraph.  Only offered when the
    // device actually has a Vietnamese voice.
    if (ttsSupported() && TTS.voice){
      const play = document.createElement('button');
      play.className = 'para-play';
      play.type = 'button';
      play.textContent = '▶';
      play.title = 'Đọc đoạn này';
      play.addEventListener('click', e => {
        e.stopPropagation();
        stopSpeaking();
        speak(text.slice(a, z));
      });
      p.appendChild(play);
    }

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
function lookupBySyllables(i, j){
  return S.lookup.get(i + ':' + j) || null;
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

function selectByWordId(id){
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

/* A drag across words, or a shift-click, selects the whole range. */
function selectByOffsets(from, to){
  const syl = S.bundle.syllables;
  const a = syllableAt(S.bundle.text, syl, Math.min(from, to));
  const b = syllableAt(S.bundle.text, syl, Math.max(from, to));
  selectSyllables(a, b + 1);
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

/* One dictionary span, as exported in the bundle's lookup index. */
function paintLookup(hit){
  const parts = [];
  if (hit.en){
    parts.push('<div class="en">' + esc(hit.en) + '</div>');
    if (hit.senses && hit.senses.length)
      parts.push('<div class="alt">' + hit.senses.map(esc).join(' · ') + '</div>');
  } else {
    parts.push('<div class="none">Từ điển biết đây là một từ, nhưng chưa có nghĩa ' +
      'tiếng Anh cho nó.</div>');
  }
  $('shDef').innerHTML = parts.join('');
}

function showSelection(){
  const {start, surface} = selectionText();
  const exact = tokenBySyllables(S.sel.sylStart, S.sel.sylEnd);
  const inside = tokensInside(S.sel.sylStart, S.sel.sylEnd);
  const dict = lookupBySyllables(S.sel.sylStart, S.sel.sylEnd);

  $('shForm').textContent = surface;
  $('shMeta').textContent =
    (S.sel.sylEnd - S.sel.sylStart) + ' âm tiết' +
    (exact ? '' : ' · chia lại') +
    (dict ? ' · từ điển' : '') +
    (exact && exact.cefr ? ' · ' + exact.cefr : '');
  $('shCand').hidden = true;
  $('shNote').hidden = true;
  $('shActs').innerHTML = '';
  addSpeakButton(surface);

  // 1. An exact token with a definition — no network, ever.
  if (exact && (exact.definition_en || exact.definition_vi || exact.definition)){
    paintDefinition(exact);
    showCandidates(exact);
    addHandleButtons();
    openSheet();
    return;
  }

  // 2. The dictionary index: this is what makes an arbitrary highlighted range
  //    resolve offline. It covers spans the segmenter never produced a token for,
  //    including the sub-spans of a token that was wrongly merged.
  if (dict){
    paintLookup(dict);
    showCandidates(exact);
    addHandleButtons();
    openSheet();
    return;
  }

  // 3. A token exists but carries no gloss, or the range was re-cut: try the
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

  // 4. The one paid path: a single sentence, then cached locally.
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

function addSpeakButton(text){
  const b = document.createElement('button');
  b.textContent = '🔊 Nghe';
  b.title = 'Đọc từ này';
  if (!ttsSupported() || !TTS.voice){
    b.disabled = true;
    b.title = 'Máy này chưa cài giọng tiếng Việt';
  } else {
    b.addEventListener('click', () => {
      stopSpeaking();
      speak(text);
    });
  }
  $('shActs').appendChild(b);
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
  // Selection handles: the remedy when segmentation was wrong, and the way to
  // narrow a merged token onto the word actually being read.
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

/* ---------- practice: comprehension and writing ---------- */
function renderExercises(b){
  const ex = b.exercises || {};
  const mcq = ex.mcq || [], short = ex.short || [], writing = ex.writing;
  if (!mcq.length && !short.length && !writing){ $('exBox').hidden = true; return; }
  $('exBox').hidden = false;
  $('exSummary').textContent = 'Bài tập: ' + mcq.length + ' câu trắc nghiệm' +
    (short.length ? ', ' + short.length + ' câu trả lời ngắn' : '') +
    (writing ? ', 1 bài viết' : '');

  const parts = [];
  if (mcq.length) parts.push('<h4>Đọc hiểu</h4>');
  mcq.forEach((q, i) => parts.push(renderQuestion(q, i)));
  if (short.length) parts.push('<h4>Trả lời ngắn</h4>');
  short.forEach((q, i) => parts.push(renderShort(q, i)));
  if (writing) parts.push(renderWriting(writing));

  $('exIn').innerHTML = parts.join('');
  wireExercises();
  updateScore();
}

function renderQuestion(q, ordinal){
  const chosen = S.quiz['q' + ordinal];
  const options = (q.options || []).map((opt, i) => {
    let cls = 'opt';
    if (chosen !== undefined){
      if (i === q.answer) cls += ' right';
      else if (i === chosen) cls += ' wrong';
    }
    return '<button class="' + cls + '" data-q="' + ordinal + '" data-i="' + i + '"' +
      (chosen !== undefined ? ' disabled' : '') + '>' +
      String.fromCharCode(65 + i) + '. ' + esc(opt) + '</button>';
  }).join('');
  const why = (chosen !== undefined && q.why)
    ? '<div class="why">' + esc(q.why) + '</div>' : '';
  return '<div class="q"><div class="ask">' + (ordinal + 1) + '. ' + esc(q.q) + '</div>' +
    options + why + '</div>';
}

function renderShort(q, ordinal){
  const key = 's' + ordinal;
  const saved = S.quiz[key] || '';
  return '<div class="q">' +
    '<div class="ask">' + esc(q.q) + '</div>' +
    '<textarea data-short="' + ordinal + '" placeholder="Viết câu trả lời của bạn…">' +
      esc(saved) + '</textarea>' +
    '<div class="acts"><button data-reveal="' + ordinal + '">Xem gợi ý</button></div>' +
    '<div class="hint" data-hint="' + ordinal + '" hidden>' +
      (q.sample ? '<b>Trả lời mẫu:</b> ' + esc(q.sample) + '<br>' : '') +
      renderKeyPoints(q.key_points, saved) +
    '</div></div>';
}

function renderWriting(w){
  return '<h4>Bài viết</h4><div class="q">' +
    '<div class="ask">' + esc(w.prompt) + '</div>' +
    '<textarea data-writing="1" placeholder="Viết bài của bạn ở đây…"></textarea>' +
    '<div class="count" data-count="1"></div>' +
    '<div class="acts"><button data-reveal-writing="1">Xem gợi ý</button>' +
    '<button data-check-writing="1">Tự kiểm tra</button></div>' +
    '<div class="hint" data-writing-hint="1" hidden></div>' +
    '</div>';
}

function renderKeyPoints(points, answer){
  if (!points || !points.length) return '';
  const text = (answer || '').toLowerCase();
  return '<b>Ý cần có:</b><ul class="kp">' + points.map(p => {
    const words = contentWords(p);
    const hits = words.filter(w => text.includes(w)).length;
    const ok = words.length > 0 && hits >= Math.ceil(words.length / 2);
    return '<li><span class="' + (ok ? 'hit' : 'miss') + '">' + (ok ? '✓' : '○') + '</span> ' +
      esc(p) + '</li>';
  }).join('') + '</ul>';
}

/* Content words of a Vietnamese phrase, for the offline "did you say this" check.
   Diacritics are kept (they are what makes the word the word), stopwords dropped so
   a match means something.  This is a checklist, not a grade: it can see that a fact
   is missing, never that a sentence is good. */
const STOPWORDS = new Set(('và với cho các những một này đó kia ấy nào ai gì thì mà nên ' +
  'nhưng hoặc nếu vì do bởi để đã đang sẽ vừa mới cũng đều chỉ còn rất quá lắm hơn không ' +
  'chẳng chưa phải được bị có là ở tại từ đến tới về ra vào lên xuống rồi trước sau trong ' +
  'ngoài trên dưới giữa khi lúc của ông bà anh chị em nó họ ta tôi mình người').split(' '));

function contentWords(phrase){
  return String(phrase || '')
    .toLowerCase()
    .replace(/[.,;:!?()"“”'’…\-–/]/g, ' ')
    .split(/\s+/)
    .filter(w => w.length > 1 && !STOPWORDS.has(w));
}

function wireExercises(){
  $('exIn').querySelectorAll('button[data-q]').forEach(btn => {
    btn.addEventListener('click', () => {
      const ordinal = btn.dataset.q;
      if (S.quiz['q' + ordinal] !== undefined) return;
      S.quiz['q' + ordinal] = +btn.dataset.i;
      saveQuiz();
      renderExercises(S.bundle);
    });
  });

  $('exIn').querySelectorAll('button[data-reveal]').forEach(btn => {
    btn.addEventListener('click', () => {
      const ordinal = btn.dataset.reveal;
      const box = $('exIn').querySelector('[data-hint="' + ordinal + '"]');
      box.hidden = !box.hidden;
    });
  });

  $('exIn').querySelectorAll('textarea[data-short]').forEach(area => {
    area.addEventListener('input', () => {
      S.quiz['s' + area.dataset.short] = area.value;
      saveQuiz();
    });
  });

  const write = $('exIn').querySelector('textarea[data-writing]');
  if (write){
    write.value = S.writing;
    countWords(write);
    write.addEventListener('input', () => {
      S.writing = write.value;
      try { localStorage.setItem(S.writeKey, S.writing); } catch {}
      countWords(write);
    });
  }

  const revealWriting = $('exIn').querySelector('button[data-reveal-writing]');
  if (revealWriting) revealWriting.addEventListener('click', () => {
    const w = (S.bundle.exercises || {}).writing || {};
    const box = $('exIn').querySelector('[data-writing-hint]');
    box.innerHTML = renderKeyPoints(w.key_points, S.writing) +
      (w.model_answer ? '<br><b>Bài mẫu:</b> ' + esc(w.model_answer) : '');
    box.hidden = !box.hidden;
  });

  const checkWriting = $('exIn').querySelector('button[data-check-writing]');
  if (checkWriting) checkWriting.addEventListener('click', () => {
    const w = (S.bundle.exercises || {}).writing || {};
    const box = $('exIn').querySelector('[data-writing-hint]');
    box.innerHTML = renderKeyPoints(w.key_points, S.writing) +
      '<br><span style="color:var(--dim)">Đây chỉ là danh sách kiểm tra: nó thấy được ' +
      'thiếu ý, không đánh giá được câu văn.</span>';
    box.hidden = false;
  });
}

function countWords(area){
  const target = $('exIn').querySelector('[data-count]');
  if (!target) return;
  const w = (S.bundle.exercises || {}).writing || {};
  const n = area.value.trim() ? area.value.trim().split(/\s+/).length : 0;
  const min = w.min_words || 0;
  target.textContent = n + ' từ' + (min ? (n >= min ? ' ✓ (tối thiểu ' + min + ')' : ' / tối thiểu ' + min) : '');
}

function saveQuiz(){
  try { localStorage.setItem(S.quizKey, JSON.stringify(S.quiz)); } catch {}
  // A short answer lives in the same store, so the key-point ticks survive a reload.
  $('exIn').querySelectorAll('textarea[data-short]').forEach(area => {
    S.quiz['s' + area.dataset.short] = area.value;
  });
}

function updateScore(){
  const mcq = (S.bundle.exercises || {}).mcq || [];
  if (!mcq.length) return;
  let answered = 0, right = 0;
  mcq.forEach((q, i) => {
    const chosen = S.quiz['q' + i];
    if (chosen === undefined) return;
    answered++;
    if (chosen === q.answer) right++;
  });
  if (!answered) return;
  $('exIn').insertAdjacentHTML('beforeend',
    '<div class="score">Trắc nghiệm: ' + right + '/' + answered + ' câu đúng' +
    (answered < mcq.length ? ' (còn ' + (mcq.length - answered) + ' câu)' : '') + '.</div>');
}

/* ---------- password-gated refresh ---------- */
async function refresh(){
  if (!REFRESH_ENDPOINT) return;
  const password = window.prompt('Mật khẩu để lấy bài mới:');
  if (!password) return;
  const button = $('refresh');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '… đang chạy';
  try{
    const r = await fetch(REFRESH_ENDPOINT, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password, level: S.level, limit: 5}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
    window.alert('Đã bắt đầu lấy bài mới.' + (d.run_url ? '\n\nTheo dõi: ' + d.run_url : '') +
      '\nBài mới sẽ xuất hiện sau vài phút — tải lại trang sau.');
  } catch (e){
    window.alert('Không chạy được: ' + e.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ---------- pointer wiring ----------
   Long-press is detected from pointer events, but the *tap* action runs on
   `click`. Click fires for every input device (touch, mouse, keyboard, and
   synthetic clicks), whereas pointerup does not — and having the two paths
   separate removes the double-fire risk entirely.

   A pointer that moves more than DRAG_PX is a drag, not a tap: it selects the whole
   range between press and release, which is how a reader highlights "a string of
   words" and asks the dictionary about it. */
const host = $('text');
let pressTimer = null, longFired = false, dragFrom = null, dragging = false;

host.addEventListener('pointerdown', e => {
  const span = e.target.closest('.w');
  if (e.target.closest('.para-play')) return;
  dragging = false;
  dragFrom = {x: e.clientX, y: e.clientY};
  if (!span) return;
  longFired = false;
  clearTimeout(pressTimer);
  pressTimer = setTimeout(() => {
    longFired = true;
    dragging = false;
    selectByWordId(+span.dataset.wordId);
  }, LONG_PRESS_MS);
});

host.addEventListener('pointermove', e => {
  if (!dragFrom) return;
  if (Math.abs(e.clientX - dragFrom.x) + Math.abs(e.clientY - dragFrom.y) > DRAG_PX){
    dragging = true;
    clearTimeout(pressTimer);
  }
});

host.addEventListener('pointerup', e => {
  clearTimeout(pressTimer);
  const from = dragFrom;
  dragFrom = null;
  if (!dragging || !from) return;
  dragging = false;
  longFired = true;                 // swallow the click this pointerup generates
  const a = offsetFromPoint(from.x, from.y);
  const b = offsetFromPoint(e.clientX, e.clientY);
  if (a != null && b != null) selectByOffsets(a, b);
});

host.addEventListener('pointercancel', () => { clearTimeout(pressTimer); dragFrom = null; });
host.addEventListener('pointerleave', () => { clearTimeout(pressTimer); dragFrom = null; });
host.addEventListener('contextmenu', e => { if (e.target.closest('.w')) e.preventDefault(); });

host.addEventListener('click', e => {
  // A long-press or a drag already resolved this word; swallow the click after it.
  if (longFired){ longFired = false; return; }
  const span = e.target.closest('.w');
  if (span){
    // Shift-click extends the current selection, which is the desktop equivalent
    // of dragging and works with a keyboard too.
    if (e.shiftKey && S.sel){
      const t = S.bundle.tokens.find(x => x.id === +span.dataset.wordId);
      if (t){
        selectSyllables(Math.min(S.sel.sylStart, t.syl_start),
                        Math.max(S.sel.sylEnd, t.syl_end));
        return;
      }
    }
    selectByWordId(+span.dataset.wordId);
    return;
  }
  // The tap landed between tokens: resolve by raw character offset, which is the
  // case the single-sentence fallback exists for.
  const off = offsetFromPoint(e.clientX, e.clientY);
  if (off != null) selectByOffset(off);
});

/* ---------- wiring ---------- */
$('back').addEventListener('click', renderList);
$('sheetClose').addEventListener('click', closeSheet);
$('level').addEventListener('change', e => {
  S.level = e.target.value;
  localStorage.setItem('vnread.level', S.level);
  renderList();
});
$('themeToggle').addEventListener('click', () => {
  if (window.NgheTheme) window.NgheTheme.toggle();
});
$('refresh').addEventListener('click', refresh);
$('playAll').addEventListener('click', readArticle);
$('stopSpeak').addEventListener('click', stopSpeaking);
$('rate').addEventListener('change', e => { TTS.rate = +e.target.value; });
$('voice').addEventListener('change', e => {
  TTS.voice = TTS.voices.find(v => v.name === e.target.value) || null;
  localStorage.setItem('vnread.voice', e.target.value);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); });

boot();
