'use strict';
// Idea Pad: saved video ideas, written by hand or generated locally from the catalogue's metadata.
const IDEA_STATUSES=['idea','planned','shooting','editing','published','dropped'];
const IDEA_FORMATS=['short','reel','vlog','story','montage','explainer','anniversary','other'];
let ideaFilter='', ideaItems=[], ideaShown=100;
const pct=(n,t)=>t?(n/t*100<1&&n?'<1':Math.round(n/t*100))+'%':'—';
function ideaMoment(m){
 if(!m)return'';
 const c=m.counts||{};const parts=[m.start===m.end?m.start:`${m.start} → ${m.end}`,
  [c.photos?`${fmt(c.photos)} photos`:'',c.videos?`${fmt(c.videos)} videos`:'',c['360']?`${fmt(c['360'])} 360°`:''].filter(Boolean).join(', ')];
 if(m.places&&m.places.length)parts.push(m.places.join(' · '));
 if(m.devices&&m.devices.length)parts.push(m.devices.slice(0,2).join(' · '));
 return `<p class="idea-moment">${m.anniversary?`<span class="idea-chip warm">${esc(m.anniversary)}</span>`:''}${esc(parts.filter(Boolean).join(' · '))}</p>`;
}
function ideaCard(i){
 const linked=(i.linked||[]).map(a=>`<button class="idea-thumb" data-open-asset="${esc(a.id)}" title="${esc(a.filename)}">${a.thumbnail?`<img loading="lazy" src="/thumb/${esc(a.thumbnail)}" alt="${esc(a.filename)}">`:`<span>${a.kind==='video'?'▷':'▧'}</span>`}</button>`).join('');
 return `<article class="idea-card" data-idea-id="${i.id}">
  <div class="idea-card-top"><span class="idea-chip">${esc(i.format)}</span><span class="idea-chip ${i.origin==='generated'?'gen':''}">${i.origin==='generated'?'✧ Generated · '+esc(i.model):'✎ Yours'}</span>
   <select class="idea-status" aria-label="Status">${IDEA_STATUSES.map(s=>`<option ${s===i.status?'selected':''}>${s}</option>`).join('')}</select></div>
  <h3 class="idea-title" contenteditable="plaintext-only" spellcheck="true" title="Click to rename. Enter saves, Esc cancels.">${esc(i.title)}</h3>
  ${i.logline?`<p class="idea-logline">${esc(i.logline)}</p>`:''}
  ${i.basis&&i.basis.why?`<p class="fine"><strong>Why now:</strong> ${esc(i.basis.why)}</p>`:''}
  ${ideaMoment(i.basis&&i.basis.moment)}
  ${linked?`<div class="idea-thumbs">${linked}</div>`:''}
  ${i.tags.length?`<div class="tag-list">${i.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>`:''}
  <label class="fine idea-notes-label">Notes<textarea class="idea-notes" aria-label="Notes for ${esc(i.title)}" rows="2" placeholder="Shots to get, music, people to ask…">${esc(i.notes)}</textarea></label>
  <div class="idea-card-bottom"><span class="fine">Updated ${new Date(i.updated*1000).toLocaleDateString()}</span>${i.basis&&i.basis.moment?`<button class="text-button" data-idea-dates="${esc(i.basis.moment.start)}|${esc(i.basis.moment.end)}">Footage from these days →</button>`:''}<button class="text-button danger" data-idea-delete="${i.id}">Delete</button></div>
 </article>`;
}
async function loadIdeaPad(){
 const args=ideaFilter?{status:ideaFilter}:{};
 const [data,cov]=await Promise.all([api('idea_pad',{...args,limit:Math.min(ideaShown,500)}),api('metadata_coverage')]);
 // Beyond 500, read further pages with an offset.
 while(data.items.length<Math.min(ideaShown,data.total)){const more=await api('idea_pad',{...args,limit:500,offset:data.items.length});if(!more.items.length)break;data.items.push(...more.items)}
 ideaItems=data.items;
 const t=cov.total;
 $('#idea-coverage').innerHTML=`<div><span>CAPTURE DATE</span><strong>${pct(t.date,t.total)}</strong></div><div><span>PLACE</span><strong>${pct(t.location,t.total)}</strong></div><div><span>CAMERA (RECORDED)</span><strong>${pct(t.camera,t.total)}</strong></div><div><span>DEVICE (FILENAME GUESS)</span><strong>${pct(t.device_hint,t.total)}</strong></div><div><span>ANALYSED</span><strong>${pct(t.analysed,t.total)}</strong></div>`;
 $('#idea-coverage-notes').innerHTML=cov.notes.map(n=>`<li>${esc(n)}</li>`).join('');
 $('#idea-filters').innerHTML=['',...IDEA_STATUSES].map(s=>`<button class="${s===ideaFilter?'selected':''}" data-idea-filter="${s}">${s?s[0].toUpperCase()+s.slice(1):'All'} <b>${s?(data.counts[s]||0):Object.values(data.counts).reduce((a,b)=>a+b,0)}</b></button>`).join('');
 $('#idea-list').innerHTML=ideaItems.length?ideaItems.map(ideaCard).join(''):`<div class="empty"><p class="eyebrow">NOTHING HERE YET</p><h2>No ideas ${ideaFilter?'with this status':'yet'}</h2><p>Write one down, or generate a few from your library.</p></div>`;
 $('#idea-more').classList.toggle('hidden',data.total<=ideaItems.length);
 $('#idea-more').textContent=`Show more (${fmt(data.total-ideaItems.length)} not shown)`;
 $$('[data-idea-filter]').forEach(b=>b.onclick=safe(async()=>{ideaFilter=b.dataset.ideaFilter;ideaShown=100;await loadIdeaPad()}));
 $$('.idea-card').forEach(card=>{
  const id=Number(card.dataset.ideaId);const item=ideaItems.find(x=>x.id===id);
  card.querySelector('.idea-status').onchange=safe(async e=>{await api('idea_pad_update',{id,status:e.target.value},true);toast('Status saved');await loadIdeaPad()});
  const title=card.querySelector('.idea-title');
  title.onblur=safe(async()=>{const v=title.textContent.trim();if(!v){title.textContent=item.title;return}if(v!==item.title){await api('idea_pad_update',{id,title:v},true);item.title=v;toast('Title saved')}});
  title.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();title.blur()}else if(e.key==='Escape'){title.textContent=item.title;title.blur()}};
  const notes=card.querySelector('.idea-notes');
  notes.onblur=safe(async()=>{if(notes.value!==item.notes){await api('idea_pad_update',{id,notes:notes.value},true);item.notes=notes.value;toast('Notes saved')}});
 });
 $$('[data-idea-delete]').forEach(b=>b.onclick=safe(async()=>{const item=ideaItems.find(x=>x.id===Number(b.dataset.ideaDelete));if(!confirm(`Delete “${item.title}”? This cannot be undone.`))return;await api('idea_pad_delete',{id:item.id},true);toast('Idea deleted');await loadIdeaPad()}));
 $$('[data-open-asset]').forEach(b=>b.onclick=safe(()=>openDetail(b.dataset.openAsset)));
 $$('[data-idea-dates]').forEach(b=>b.onclick=safe(async()=>{const [s,e]=b.dataset.ideaDates.split('|');resetLibraryFilters();$('#since').value=s;$('#until').value=e;$('#date-row').classList.remove('hidden');navigate('library');offset=0;await search()}));
}
function setupIdeaPad(){
 $('#idea-more').onclick=safe(async()=>{ideaShown+=100;await loadIdeaPad()});
 $('#idea-pad-format').innerHTML=IDEA_FORMATS.map(f=>`<option ${f==='other'?'selected':''}>${f}</option>`).join('');
 $('#idea-pad-form').onsubmit=safe(async e=>{e.preventDefault();
  const tags=$('#idea-pad-tags').value.split(',').map(t=>t.trim()).filter(Boolean);
  await api('idea_pad_add',{title:$('#idea-pad-title').value,logline:$('#idea-pad-logline').value,notes:$('#idea-pad-notes').value,format:$('#idea-pad-format').value,tags},true);
  e.target.reset();$('#idea-pad-format').value='other';toast('Idea saved');ideaFilter='';await loadIdeaPad()});
 $('#idea-generate-form').onsubmit=safe(async e=>{e.preventDefault();const b=$('#idea-generate');b.disabled=true;b.textContent='Reading your library…';
  try{const r=await api('idea_pad_generate',{count:Number($('#idea-generate-count').value),since:$('#idea-generate-since').value,until:$('#idea-generate-until').value,focus:$('#idea-generate-focus').value,model:$('#idea-generate-model').value,include_used:$('#idea-generate-used').checked},true);
   toast(`${r.items.length} new idea${r.items.length===1?'':'s'} from ${r.moments_considered} moments`);ideaFilter='';await loadIdeaPad()}
  finally{b.disabled=false;b.textContent='✧ Generate ideas'}});
}
