// Read Google Photos' People & pets in the owner's own signed-in browser, for FrameAtlas
// `google_read_result` (kind people), which keeps each read and names people from all of them.
// Nothing here talks to FrameAtlas: an agent runs each step in a browser tab (for example with
// Claude in Chrome's javascript tool), reads the small result, and passes it on.
// Google throttles searches (after roughly 25 to 55 people, sooner for people with many photos,
// searches return "No results" for about an hour), so read a few people at a time; the read plan
// (google_read_next) keeps count. Person links carry short-lived tokens: always open a person
// from a freshly loaded People page, as step 3 does.
//
// Step 1, on https://photos.google.com/people: list the named people.
//   Result: [name, ...], to pass to google_read_register as entries.
// Step 3, on https://photos.google.com/people after a full page load: open one person (set NAME
//   first; it opens them with a full page load, so the previous person's grid is not left
//   behind), then run again on the person's page to read their photos.
//   Result: {name, items, complete, packed} where packed is one token per photo or video, its kind
//   (P or V) and its local capture time as base-36 seconds since 1970 read as UTC. Long results
//   are also written to an <article id="frameatlas-out"> for reading with get_page_text, because
//   script results are cut short.

// ---- step 1
(async () => {
  const S = ms => new Promise(r => setTimeout(r, ms));
  await S(1000);
  let el = null, best = 0;
  for (const e of document.querySelectorAll('c-wiz,div')) {
    const d = e.scrollHeight - e.clientHeight;
    if (d > best && e.clientHeight > 300 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { el = e; best = d; }
  }
  const names = new Set();
  const grab = () => { for (const a of document.querySelectorAll('a[href^="./search/"]')) { const n = (a.innerText || '').trim(); if (n) names.add(n); } };
  for (let i = 0; i < 200; i++) {
    grab();
    if (!el) break;
    const before = el.scrollTop;
    el.scrollTop += el.clientHeight * 0.9;
    await S(400);
    if (el.scrollTop === before) { grab(); break; }
  }
  return [...names];
})();

// ---- step 3 (set NAME; run on the People page; then, after the page has loaded the person, run again)
(async () => {
  const NAME = '__NAME__', MAX_ITEMS = 600;
  const S = ms => new Promise(r => setTimeout(r, ms));
  const sel = 'a[aria-label^="Photo - "],a[aria-label^="Video - "]';
  if (location.pathname.endsWith('/people')) {
    let a;
    for (let k = 0; k < 50 && !a; k++) {
      a = [...document.querySelectorAll('a[href^="./search/"]')].find(x => (x.innerText || '').trim() === NAME);
      if (!a) await S(200);
    }
    if (!a) return { error: 'This person is not in the list on screen; reload the People page' };
    location.href = a.href;  // a full page load
    return { opening: NAME };
  }
  for (let k = 0; k < 60 && !document.querySelector(sel); k++) await S(250);
  if (/No results/.test(document.body.innerText) && !document.querySelector(sel)) return { name: NAME, error: 'No results: Google is throttling; wait an hour' };
  const scroller = () => {
    let found = null, best = 0;
    for (const e of document.querySelectorAll('c-wiz,div')) {
      const d = e.scrollHeight - e.clientHeight;
      if (d > best && e.clientHeight > 300 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { found = e; best = d; }
    }
    return found;
  };
  let el = scroller();
  const seen = new Map();
  const grab = () => { for (const e of document.querySelectorAll(sel)) { const h = e.getAttribute('href') || ''; if (!seen.has(h)) seen.set(h, e.getAttribute('aria-label')); } };
  let still = 0;
  for (let i = 0; i < 3000 && seen.size < MAX_ITEMS && still < 16; i++) {
    grab();
    const before = el ? el.scrollTop : 0;
    if (el) el.scrollTop += el.clientHeight * 0.9;
    await S(300);
    still = (!el || el.scrollTop === before) ? still + 1 : 0;
    if (still > 0) el = scroller() || el;
  }
  grab();
  // "Photo - Portrait - Nov 15, 2026, 3:56:55 PM": the last part is the local capture time
  const tokens = [...seen.values()].map(label => {
    const parts = (label || '').split(' - ');
    const d = new Date(parts[parts.length - 1].replace(/\s+/g, ' '));
    if (isNaN(d)) return '';
    return (parts[0] === 'Video' ? 'V' : 'P') + Math.round((d.getTime() - d.getTimezoneOffset() * 60000) / 1000).toString(36);
  }).filter(Boolean);
  const out = { name: NAME, items: tokens.length, complete: seen.size >= MAX_ITEMS || still >= 16, packed: tokens.join(' ') };
  let art = document.getElementById('frameatlas-out');
  if (!art) { art = document.createElement('article'); art.id = 'frameatlas-out'; document.body.appendChild(art); }
  art.textContent = JSON.stringify(out);
  return out.packed.length > 900 ? { ...out, packed: `(${out.packed.length} characters: read <article id="frameatlas-out">)` } : out;
})();
