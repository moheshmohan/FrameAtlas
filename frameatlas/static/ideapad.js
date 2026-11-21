'use strict';
// Ideas: one pad for every video idea — written by hand, found in moments of the catalogue's
// metadata, or built as a story from chosen footage — each able to grow a cited edit sequence.
const IDEA_STATUSES=['idea','planned','shooting','editing','published','dropped'];
const IDEA_FORMATS=['short','reel','vlog','story','montage','explainer','anniversary','other'];
const IDEA_ORIGINS={'':'All sources',manual:'✎ Yours',generated:'✧ Moments',story:'▷ Footage'};
let ideaFilter='', ideaOrigin='', ideaItems=[], ideaShown=100, ideaFresh=new Set(), ideaMode='write';
const pct=(n,t)=>t?(n/t*100<1&&n?'<1':Math.round(n/t*100))+'%':'—';
function setIdeaMode(mode){
 if(!['write','moments','footage'].includes(mode))mode='write';
 ideaMode=mode;
 $$('[data-idea-mode]').forEach(b=>{const on=b.dataset.ideaMode===mode;b.classList.toggle('selected',on);b.setAttribute('aria-selected',String(on))});
 $$('[data-idea-form]').forEach(f=>f.classList.toggle('hidden',f.dataset.ideaForm!==mode));
 try{localStorage.setItem('frameatlas.ideaMode',mode)}catch{}
}
const MOMENT_KINDS={trip:'✈ Trip',day_out:'⌂ Day out',return:'↺ Return visit'};
function tripLine(m){
 const t=m.trip;if(!t||!MOMENT_KINDS[m.kind])return'';
 const bits=[m.kind==='return'?(t.years||[]).join(', '):`${m.days} day${m.days===1?'':'s'}`];
 if(t.distance_km)bits.push(`${fmt(t.distance_km)} km from ${t.home}`);
 if(t.abroad&&t.abroad.length)bits.push(t.abroad.join(', '));
 return `<span class="idea-chip trip">${esc(MOMENT_KINDS[m.kind])} · ${esc(bits.join(' · '))}</span>`;
}
function ideaMoment(m){
 if(!m)return'';
 const c=m.counts||{};const parts=[m.start===m.end?m.start:`${m.start} → ${m.end}`,
  [c.photos?`${fmt(c.photos)} photos`:'',c.videos?`${fmt(c.videos)} videos`:'',c['360']?`${fmt(c['360'])} 360°`:''].filter(Boolean).join(', ')];
 if(m.people&&m.people.length)parts.push('with '+m.people.map(p=>p.replace(/ ×\d+$/,'')).join(', '));
 if(m.places&&m.places.length)parts.push(m.places.join(' · '));
 if(m.devices&&m.devices.length)parts.push(m.devices.slice(0,2).join(' · '));
 return `<p class="idea-moment">${tripLine(m)}${m.anniversary?`<span class="idea-chip warm">${esc(m.anniversary)}</span>`:''}${esc(parts.filter(Boolean).join(' · '))}</p>`;
}
function shotRef(s,attrs){
 return `<button class="source-ref" ${attrs}>↳ ${esc(s.filename||s.asset_id)} · ${esc(citationTime(s))}${s.source_part?`<br>Part ${Number(s.source_part.index)+1}: ${esc(s.source_part.filename)} · source ${clock(s.source_part.start)}–${clock(s.source_part.end)}`:''}<br>${esc(s.reason||'')}</button>`;
}
function ideaSequence(i){
 const q=i.basis&&i.basis.sequence;if(!q)return'';
 const refs=(q.sources||[]).map((s,j)=>shotRef(s,`data-seq-idea="${i.id}" data-seq-ref="${j}"`)).join('');
 const outline=q.outline?`<ol>${q.outline.map(o=>`<li>${esc(o)}</li>`).join('')}</ol>`:'';
 const pickups=q.pickups&&q.pickups.length?`<p class="eyebrow">PICKUP SHOTS STILL NEEDED</p><ul>${q.pickups.map(p=>`<li>${esc(p)}</li>`).join('')}</ul>`:'';
 const alts=(q.alternatives||[]).map((a,k)=>`<div class="idea-alt"><strong>${esc(a.title)}</strong><p class="fine">${esc(a.hook)}</p>${(a.sources||[]).map((s,j)=>shotRef(s,`data-seq-idea="${i.id}" data-seq-alt="${k}" data-seq-ref="${j}"`)).join('')}</div>`).join('');
 const n=(q.sources||[]).length;
 return `<details class="idea-sequence" ${ideaFresh.has(i.id)?'open':''}><summary>Edit sequence · ${n} shot${n===1?'':'s'}${q.pickups&&q.pickups.length?` · ${q.pickups.length} pickup${q.pickups.length===1?'':'s'}`:''}</summary>
  ${outline}${refs}${pickups}${q.related_search_phrases?`<p class="fine">Also searched: ${esc(q.related_search_phrases.join(' · '))}</p>`:''}
  ${alts?`<details class="idea-alts"><summary>Other takes from the same run</summary>${alts}</details>`:''}
  <p class="fine">Drafted by ${esc(q.model||'a local model')}. Check the cited shots before using its claims in narration.</p></details>`;
}
function originChip(i){
 if(i.origin==='generated')return `<span class="idea-chip gen">✧ From a moment · ${esc(i.model)}</span>`;
 if(i.origin==='story')return `<span class="idea-chip gen">▷ From footage · ${esc(i.model)}</span>`;
 return '<span class="idea-chip">✎ Yours</span>';
}
function ideaCard(i){
 const linked=(i.linked||[]).map(a=>`<button class="idea-thumb" data-open-asset="${esc(a.id)}" title="${esc(a.filename)}">${a.thumbnail?`<img loading="lazy" src="/thumb/${esc(a.thumbnail)}" alt="${esc(a.filename)}">`:`<span>${a.kind==='video'?'▷':'▧'}</span>`}</button>`).join('');
 const hasSeq=Boolean(i.basis&&i.basis.sequence);
 return `<article class="idea-card ${ideaFresh.has(i.id)?'fresh':''}" data-idea-id="${i.id}">
  <div class="idea-card-top"><select class="idea-format" aria-label="Format of ${esc(i.title)}">${IDEA_FORMATS.map(f=>`<option ${f===i.format?'selected':''}>${f}</option>`).join('')}</select>${originChip(i)}
   <select class="idea-status" aria-label="Status of ${esc(i.title)}">${IDEA_STATUSES.map(s=>`<option ${s===i.status?'selected':''}>${s}</option>`).join('')}</select></div>
  <h3 class="idea-title" contenteditable="plaintext-only" spellcheck="true" title="Click to rename. Enter saves, Esc cancels.">${esc(i.title)}</h3>
  ${i.logline?`<p class="idea-logline">${esc(i.logline)}</p>`:''}
  ${i.basis&&i.basis.why?`<p class="fine"><strong>Why now:</strong> ${esc(i.basis.why)}</p>`:''}
  ${i.origin==='story'&&i.basis&&i.basis.prompt?`<p class="fine idea-asked" title="${esc(i.basis.prompt)}"><strong>Asked for:</strong> ${esc(i.basis.prompt)}</p>`:''}
  ${ideaMoment(i.basis&&i.basis.moment)}
  ${linked?`<div class="idea-thumbs">${linked}</div>`:''}
  ${ideaSequence(i)}
  ${i.tags.length?`<div class="tag-list">${i.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>`:''}
  <label class="fine idea-notes-label">Notes<textarea class="idea-notes" aria-label="Notes for ${esc(i.title)}" rows="2" placeholder="Shots to get, music, people to ask…">${esc(i.notes)}</textarea></label>
  <div class="idea-card-bottom"><span class="fine">Updated ${new Date(i.updated*1000).toLocaleDateString()}</span>
   <button class="text-button" data-idea-develop="${i.id}">${hasSeq?'↻ Rebuild sequence':'▷ Build edit sequence'}</button>
   ${i.basis&&i.basis.moment?`<button class="text-button" data-idea-dates="${esc(i.basis.moment.start)}|${esc(i.basis.moment.end)}">Footage from these days →</button>`:''}
   <button class="text-button" data-idea-copy="${i.id}">Copy</button>
   <button class="text-button danger" data-idea-delete="${i.id}">Delete</button></div>
 </article>`;
}
function ideaText(i){
 const q=(i.basis&&i.basis.sequence)||{};
 const lines=[i.title];if(i.logline)lines.push(i.logline);lines.push(`Format: ${i.format} · Status: ${i.status}`);
 (q.sources||[]).forEach((s,k)=>lines.push(`${k+1}. ${s.filename||s.asset_id} · ${citationTime(s)} — ${s.reason||''}`));
 (q.pickups||[]).forEach(p=>lines.push('Pickup: '+p));
 if(i.notes)lines.push('Notes: '+i.notes);
 return lines.join('\n');
}
async function loadIdeaPad(){
 const args={...(ideaFilter?{status:ideaFilter}:{}),...(ideaOrigin?{origin:ideaOrigin}:{})};
 const [data,cov]=await Promise.all([api('idea_pad',{...args,limit:Math.min(ideaShown,500)}),api('metadata_coverage')]);
 // Beyond 500, read further pages with an offset.
 while(data.items.length<Math.min(ideaShown,data.total)){const more=await api('idea_pad',{...args,limit:500,offset:data.items.length});if(!more.items.length)break;data.items.push(...more.items)}
 ideaItems=data.items;
 const t=cov.total;
 $('#idea-coverage').innerHTML=`<div><span>CAPTURE DATE</span><strong>${pct(t.date,t.total)}</strong></div><div><span>PLACE</span><strong>${pct(t.location,t.total)}</strong></div><div><span>CAMERA (RECORDED)</span><strong>${pct(t.camera,t.total)}</strong></div><div><span>DEVICE (FILENAME GUESS)</span><strong>${pct(t.device_hint,t.total)}</strong></div><div><span>ANALYSED</span><strong>${pct(t.analysed,t.total)}</strong></div>`;
 $('#idea-coverage-notes').innerHTML=cov.notes.map(n=>`<li>${esc(n)}</li>`).join('');
 const all=o=>Object.values(o||{}).reduce((a,b)=>a+b,0);
 $('#idea-filters').innerHTML=['',...IDEA_STATUSES].map(s=>`<button class="${s===ideaFilter?'selected':''}" data-idea-filter="${s}">${s?s[0].toUpperCase()+s.slice(1):'All'} <b>${s?(data.counts[s]||0):all(data.counts)}</b></button>`).join('');
 $('#idea-origins').innerHTML=Object.entries(IDEA_ORIGINS).map(([o,label])=>`<button class="${o===ideaOrigin?'selected':''}" data-idea-origin="${o}">${label} <b>${o?((data.origins||{})[o]||0):all(data.origins)}</b></button>`).join('');
 $('#idea-list').innerHTML=ideaItems.length?ideaItems.map(ideaCard).join(''):`<div class="empty"><p class="eyebrow">NOTHING HERE YET</p><h2>No ideas ${ideaFilter||ideaOrigin?'that match':'yet'}</h2><p>Write one down, find some in your library's moments, or build a story from footage.</p></div>`;
 $('#idea-more').classList.toggle('hidden',data.total<=ideaItems.length);
 $('#idea-more').textContent=`Show more (${fmt(data.total-ideaItems.length)} not shown)`;
 $$('[data-idea-filter]').forEach(b=>b.onclick=safe(async()=>{ideaFilter=b.dataset.ideaFilter;ideaShown=100;await loadIdeaPad()}));
 $$('[data-idea-origin]').forEach(b=>b.onclick=safe(async()=>{ideaOrigin=b.dataset.ideaOrigin;ideaShown=100;await loadIdeaPad()}));
 $$('.idea-card').forEach(card=>{
  const id=Number(card.dataset.ideaId);const item=ideaItems.find(x=>x.id===id);
  card.querySelector('.idea-status').onchange=safe(async e=>{await api('idea_pad_update',{id,status:e.target.value},true);toast('Status saved');await loadIdeaPad()});
  card.querySelector('.idea-format').onchange=safe(async e=>{await api('idea_pad_update',{id,format:e.target.value},true);item.format=e.target.value;toast('Format saved')});
  const title=card.querySelector('.idea-title');
  title.onblur=safe(async()=>{const v=title.textContent.trim();if(!v){title.textContent=item.title;return}if(v!==item.title){await api('idea_pad_update',{id,title:v},true);item.title=v;toast('Title saved')}});
  title.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();title.blur()}else if(e.key==='Escape'){title.textContent=item.title;title.blur()}};
  const notes=card.querySelector('.idea-notes');
  notes.onblur=safe(async()=>{if(notes.value!==item.notes){await api('idea_pad_update',{id,notes:notes.value},true);item.notes=notes.value;toast('Notes saved')}});
 });
 $$('[data-idea-delete]').forEach(b=>b.onclick=safe(async()=>{const item=ideaItems.find(x=>x.id===Number(b.dataset.ideaDelete));if(!confirm(`Delete “${item.title}”? This cannot be undone.`))return;await api('idea_pad_delete',{id:item.id},true);toast('Idea deleted');await loadIdeaPad()}));
 $$('[data-idea-copy]').forEach(b=>b.onclick=safe(async()=>{await navigator.clipboard.writeText(ideaText(ideaItems.find(x=>x.id===Number(b.dataset.ideaCopy))));toast('Idea copied')}));
 $$('[data-idea-develop]').forEach(b=>b.onclick=safe(async()=>{
  const id=Number(b.dataset.ideaDevelop);
  $$('[data-idea-develop]').forEach(x=>x.disabled=true);b.textContent='Finding shots in your footage…';
  try{await api('idea_pad_develop',{id,model:$('#idea-model').value},true);toast('Edit sequence built from indexed footage');ideaFresh=new Set([id]);await loadIdeaPad()}
  finally{$$('[data-idea-develop]').forEach(x=>x.disabled=false)}
 }));
 $$('#idea-list [data-open-asset]').forEach(b=>b.onclick=safe(()=>openDetail(b.dataset.openAsset)));
 $$('#idea-list [data-seq-ref]').forEach(b=>b.onclick=safe(()=>{
  const q=ideaItems.find(x=>x.id===Number(b.dataset.seqIdea)).basis.sequence;
  const list=b.dataset.seqAlt!==undefined?q.alternatives[Number(b.dataset.seqAlt)].sources:q.sources;
  const ref=list[Number(b.dataset.seqRef)];
  return openDetail(ref.asset_id,{...ref,kind:'citation'});
 }));
 $$('[data-idea-dates]').forEach(b=>b.onclick=safe(async()=>{const [s,e]=b.dataset.ideaDates.split('|');resetLibraryFilters();$('#since').value=s;$('#until').value=e;$('#date-row').classList.remove('hidden');navigate('library');offset=0;await search()}));
}
// ---- trips, days out and return visits read from imported places
function placesLine(list){
 if(!list||!list.length)return'';
 return `<div class="people-line">${list.map(p=>`<button class="person-chip place-chip" data-place-open="${p.id}" data-place-name="${esc(p.name)}" title="${esc(p.address||p.name)}"><span>⌖ ${esc(p.name)}</span></button>`).join('')}</div>`;
}
async function showPlaceMedia(ids,name){resetLibraryFilters();placeFilter={ids:String(ids),name:name||'this place'};navigate('library');offset=0;await search()}
let ideaTrips=[];
function tripRow(t,k){
 const when=t.kind==='return'?(t.years||[]).join(', '):(t.start===t.end?t.start:`${t.start} → ${t.end}`);
 const where=t.places.slice(0,4).map(p=>p.replace(/ ×\d+$/,'')).join(', ');
 const far=t.distance_km?`${fmt(t.distance_km)} km from ${t.home}`:'';
 const span=t.kind==='return'?`${t.visits.length} visits`:`${t.days} day${t.days===1?'':'s'}`;
 return `<li class="idea-trip"><div><span class="idea-chip trip">${esc(MOMENT_KINDS[t.kind])}</span> <strong>${esc(t.area)}</strong>${t.abroad&&t.abroad.length?` · ${esc(t.abroad.join(', '))}`:''}
  <p class="fine">${esc([when,span,far,where].filter(Boolean).join(' · '))}</p></div>
  <div class="idea-trip-actions"><button class="text-button" data-trip-ideas="${k}">✧ Ideas from this</button><button class="text-button" data-trip-footage="${k}">Footage →</button></div></li>`;
}
async function loadIdeaTrips(){
 const kind=$('#idea-trips-kind').value;
 const r=await api('places_trips',{limit:300,...(kind?{kind}:{})});
 ideaTrips=r.trips;
 $('#idea-trips-count').textContent=r.located_days?`${fmt(r.total)} found in ${fmt(r.located_days)} days with a place`:'';
 $('#idea-trips-list').innerHTML=ideaTrips.length?ideaTrips.map(tripRow).join(''):`<li class="fine">${r.located_days?'None of this kind yet.':'No places imported yet. An agent can read Google Photos’ Places in your browser and import them with places_import (see the README).'}</li>`;
 $$('[data-trip-ideas]').forEach(b=>b.onclick=()=>{
  const t=ideaTrips[Number(b.dataset.tripIdeas)];
  setIdeaMode('moments');
  $('#idea-generate-since').value=t.kind==='return'?'':t.start;$('#idea-generate-until').value=t.kind==='return'?'':t.end;
  $('#idea-generate-kind').value=t.kind;$('#idea-generate-focus').value=t.kind==='return'?t.area:'';
  $('#idea-generate-form').scrollIntoView({behavior:'smooth',block:'center'});$('#idea-generate').focus({preventScroll:true});
  toast('Dates and kind filled in. Press Generate ideas.');
 });
 $$('[data-trip-footage]').forEach(b=>b.onclick=safe(async()=>{
  const t=ideaTrips[Number(b.dataset.tripFootage)];
  if(t.kind==='return'){await showPlaceMedia(t.place_ids.join(','),t.area);return}
  resetLibraryFilters();$('#since').value=t.start;$('#until').value=t.end;$('#date-row').classList.remove('hidden');navigate('library');offset=0;await search();
 }));
}
async function showFreshIdeas(ids){
 ideaFresh=new Set(ids||[]);ideaFilter='';ideaOrigin='';ideaShown=Math.max(ideaShown,100);
 await loadIdeaPad();
 const first=$('.idea-card.fresh');if(first)first.scrollIntoView({behavior:'smooth',block:'center'});
}
function setupIdeaPad(){
 $('#idea-trips').addEventListener('toggle',safe(async()=>{if($('#idea-trips').open)await loadIdeaTrips()}));
 $('#idea-trips-kind').onchange=safe(loadIdeaTrips);
 let saved='write';try{saved=localStorage.getItem('frameatlas.ideaMode')||'write'}catch{}
 setIdeaMode(saved);
 $$('[data-idea-mode]').forEach(b=>b.onclick=()=>setIdeaMode(b.dataset.ideaMode));
 $('#idea-more').onclick=safe(async()=>{ideaShown+=100;await loadIdeaPad()});
 $('#idea-pad-format').innerHTML=IDEA_FORMATS.map(f=>`<option ${f==='other'?'selected':''}>${f}</option>`).join('');
 $('#idea-pad-form').onsubmit=safe(async e=>{e.preventDefault();
  const tags=$('#idea-pad-tags').value.split(',').map(t=>t.trim()).filter(Boolean);
  const idea=await api('idea_pad_add',{title:$('#idea-pad-title').value,logline:$('#idea-pad-logline').value,notes:$('#idea-pad-notes').value,format:$('#idea-pad-format').value,tags},true);
  e.target.reset();$('#idea-pad-format').value='other';toast('Idea saved. Build an edit sequence for it when your footage is analysed.');await showFreshIdeas([idea.id])});
 $('#idea-generate-form').onsubmit=safe(async e=>{e.preventDefault();const b=$('#idea-generate');b.disabled=true;b.textContent='Reading your library…';
  try{const r=await api('idea_pad_generate',{count:Number($('#idea-generate-count').value),since:$('#idea-generate-since').value,until:$('#idea-generate-until').value,focus:$('#idea-generate-focus').value,model:$('#idea-generate-model').value,include_used:$('#idea-generate-used').checked,kind:$('#idea-generate-kind').value},true);
   toast(`${r.items.length} new idea${r.items.length===1?'':'s'} from ${r.moments_considered} moments`);await showFreshIdeas(r.items.map(i=>i.id))}
  finally{b.disabled=false;b.textContent='✧ Generate ideas'}});
}
