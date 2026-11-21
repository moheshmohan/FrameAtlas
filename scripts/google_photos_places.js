// Read Google Photos' Places in the owner's own signed-in browser, for FrameAtlas `places_import`.
// Nothing here talks to FrameAtlas: an agent runs each step in a browser tab (for example with
// Claude in Chrome's javascript tool), reads the small result, and passes it to places_import.
// Google throttles long reads (searches start returning "No results" after roughly 25,000 photos
// scrolled), so read a few places at a time and spread a whole library over a few hours.
//
// Step 1, on https://photos.google.com/places: list the places with Google's place key.
//   Result: [[name, key], ...], also kept in sessionStorage.frameatlasPlaces.
// Step 2, on any https://www.google.com/maps page: each place's position and address.
//   Result: window.frameatlasMaps, [latitude, longitude, address, plus code] for each key.
// Step 3, on https://photos.google.com/places again: open one place (a full page load per place,
//   so the previous place's grid is not left behind) and read its days. Set NAME first.
//   Result: {name, key, items, packed} where packed is one token per day,
//   "YYYYMMDD.<P|V>HHMMSS.<P|V>HHMMSS.N": the day, the kind and local time of its first and last
//   item, and how many items. Long results are also written to an <article id="frameatlas-out">
//   for reading with get_page_text, because script results are cut short.

// ---- step 1
(async () => {
  const S = ms => new Promise(r => setTimeout(r, ms));
  let el = null, best = 0;
  for (const e of document.querySelectorAll('c-wiz,div')) {
    const d = e.scrollHeight - e.clientHeight;
    if (d > best && e.clientHeight > 300 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { el = e; best = d; }
  }
  const found = new Map();
  const grab = () => {
    for (const a of document.querySelectorAll('a[href^="./search/"]')) {
      const name = (a.innerText || '').trim();
      if (!name || /^(Favorites|Videos|Recently added)$/.test(name) || found.has(name)) continue;
      let raw = '';
      try { raw = atob(decodeURIComponent(a.getAttribute('href').slice(9)).replace(/-/g, '+').replace(/_/g, '/')); } catch (e) { }
      const key = (raw.match(/0x[0-9a-f]+/) || [''])[0].slice(2);
      if (key) found.set(name, key);
    }
  };
  if (el) el.scrollTop = 0;
  await S(800);
  for (let i = 0, still = 0; el && i < 400 && still < 5; i++) {
    grab();
    const before = el.scrollTop;
    el.scrollTop += el.clientHeight * 0.5;
    await S(350);
    still = el.scrollTop === before ? still + 1 : 0;
  }
  grab();
  const list = [...found.entries()];
  sessionStorage.frameatlasPlaces = JSON.stringify(list);
  return list.length;
})();

// ---- step 2, on any https://www.google.com/maps page: every place's position and address.
// Opens each place in a hidden same-site frame (five at a time) and reads the position Maps
// writes into the frame's address (!3d<lat>!4d<lng>) and the place's full name. Set KEYS to
// step 1's keys; it runs in the background: read window.frameatlasMaps when frameatlasDone
// reaches KEYS.length. Result per key: [latitude, longitude, address, plus code].
(() => {
  const KEYS = ['__KEY__'];
  const S = ms => new Promise(r => setTimeout(r, ms));
  async function one(key) {
    const f = document.createElement('iframe');
    f.style.cssText = 'position:fixed;left:-3000px;width:1024px;height:768px';
    f.src = '/maps?cid=' + BigInt('0x' + key).toString();
    document.body.appendChild(f);
    let path = '';
    for (let i = 0; i < 60; i++) {
      await S(250);
      try { path = f.contentWindow.location.pathname; if (/!3d-?[\d.]+!4d-?[\d.]+/.test(path)) break; } catch (e) { }
    }
    f.remove();
    const c = path.match(/!3d(-?[\d.]+)!4d(-?[\d.]+)/), m = path.match(/place\/([^/]+)/), pc = path.match(/!20s([^!/]+)/);
    // without a position Maps still gives a plus code, which places_import reads as plus_code
    return [c ? +c[1] : null, c ? +c[2] : null, m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : '', pc ? decodeURIComponent(pc[1]) : ''];
  }
  window.frameatlasMaps = {}; window.frameatlasDone = 0;
  const queue = KEYS.slice();
  const worker = async () => { while (queue.length) { const k = queue.shift(); try { frameatlasMaps[k] = await one(k); } catch (e) { frameatlasMaps[k] = [null, null, '']; } frameatlasDone++; } };
  Promise.all([worker(), worker(), worker(), worker(), worker()]);
  return KEYS.length;
})();

// ---- step 3 (set NAME; run on the Places page; then, after the page has loaded the place, run again)
(async () => {
  const NAME = '__NAME__', MAX_ITEMS = 30000;
  const S = ms => new Promise(r => setTimeout(r, ms));
  const sel = 'a[aria-label^="Photo - "],a[aria-label^="Video - "]';
  if (location.pathname.endsWith('/places')) {
    const list = new Map(JSON.parse(sessionStorage.frameatlasPlaces || '[]'));
    let a;
    for (let k = 0; k < 50 && !a; k++) {
      a = [...document.querySelectorAll('a[href^="./search/"]')].find(x => (x.innerText || '').trim() === NAME);
      if (!a) await S(200);
    }
    if (!a) return { error: 'This place is not in the list on screen; scroll it into view or reload' };
    sessionStorage.frameatlasKey = list.get(NAME) || '';
    location.href = a.href;  // a full page load
    return { opening: NAME };
  }
  for (let k = 0; k < 60 && !document.querySelector(sel); k++) await S(250);
  if (/No results/.test(document.body.innerText) && !document.querySelector(sel)) return { name: NAME, error: 'No results: Google is throttling; wait an hour' };
  let el = null, best = 0;
  for (const e of document.querySelectorAll('c-wiz,div')) {
    const d = e.scrollHeight - e.clientHeight;
    if (d > best && e.clientHeight > 300 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { el = e; best = d; }
  }
  const seen = new Map();
  const grab = () => { for (const e of document.querySelectorAll(sel)) { const h = e.getAttribute('href') || ''; if (!seen.has(h)) seen.set(h, e.getAttribute('aria-label')); } };
  for (let i = 0, still = 0; i < 20000 && seen.size < MAX_ITEMS && still < 15; i++) {
    grab();
    const before = el ? el.scrollTop : 0;
    if (el) el.scrollTop += el.clientHeight * 0.9;
    await S(300);
    still = (!el || el.scrollTop === before) ? still + 1 : 0;
  }
  grab();
  const days = new Map();
  const two = n => String(n).padStart(2, '0');
  for (const label of seen.values()) {
    const parts = label.split(' - ');
    const t = new Date(parts[parts.length - 1].replace(/\s+/g, ' '));
    if (isNaN(t)) continue;
    const day = `${t.getFullYear()}${two(t.getMonth() + 1)}${two(t.getDate())}`;
    const time = `${two(t.getHours())}${two(t.getMinutes())}${two(t.getSeconds())}`;
    const kind = parts[0] === 'Video' ? 'V' : 'P';
    const d = days.get(day) || { first: null, last: null, n: 0 };
    if (!d.first || time < d.first.slice(1)) d.first = kind + time;
    if (!d.last || time > d.last.slice(1)) d.last = kind + time;
    d.n += 1;
    days.set(day, d);
  }
  const packed = [...days.entries()].sort().map(([day, d]) => `${day}.${d.first}.${d.last}.${d.n}`).join(' ');
  const out = { name: NAME, key: sessionStorage.frameatlasKey || '', items: seen.size, days: days.size, packed };
  let art = document.getElementById('frameatlas-out');
  if (!art) { art = document.createElement('article'); art.id = 'frameatlas-out'; document.body.prepend(art); }
  art.textContent = JSON.stringify(out);
  return packed.length > 900 ? { ...out, packed: `${packed.length} characters: read #frameatlas-out with get_page_text` } : out;
})();
