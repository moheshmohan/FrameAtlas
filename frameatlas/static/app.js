'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number(n).toLocaleString();
const size = n => n < 1024*1024 ? (n/1024).toFixed(0)+' KB' : n < 1024**3 ? (n/1024**2).toFixed(1)+' MB' : (n/1024**3).toFixed(1)+' GiB';
const clock = n => {n=Math.max(0,Math.floor(n||0));return Math.floor(n/60)+':'+String(n%60).padStart(2,'0')};
function editTime(seconds){
 const ms=Math.round(Number(seconds)*1000);
 if(seconds==null||!Number.isSafeInteger(ms)||ms<0)return '—';
 return `${clock(Math.floor(ms/1000))}.${String(ms%1000).padStart(3,'0')}`;
}
const matchTime = m => m.source_part ? `Part ${Number(m.source_part.index)+1} · source ${editTime(m.source_part.sample_time??m.source_part.start)} · group ${editTime(m.start)}` : editTime(m.start);
const date = s => s ? String(s).slice(0,10) : 'Date unknown';
const sourceName = p => ({google_photos:'Google Photos',insta360:'Insta360',local:'Local reference'}[p]||p);
let csrf='', page='library', provider='', offset=0, results=[], total=0, selected=new Map(), currentAsset=null, stats=null;
let bounds='',captureMonth='',personFilter=null,placeFilter=null;
let searchGeneration=0, relatedResults=false, relatedPending=false, toastTimer;
async function api(action,args={},post=false,retried=false) {
  const response=await fetch('/api/'+action+(post?'':'?'+new URLSearchParams(args)),post?{method:'POST',headers:{'Content-Type':'application/json','X-FrameAtlas-Token':csrf},body:JSON.stringify(args)}:{});
  const data=await response.json();if(response.status===403&&post&&!retried&&data.error==='Reload the local app to refresh its session'){csrf=(await(await fetch('/api/session')).json()).csrf;return api(action,args,post,true)}if(!response.ok)throw Error(data.error||'Request failed');return data;
}
function toast(text,error=false){const e=$('#toast');e.textContent=text;e.classList.toggle('error',error);e.classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.classList.add('hidden'),error?12000:6000)}
function safe(fn){return async event=>{try{await fn(event)}catch(e){toast(e.message,true)}}}
function navigate(next){if(next==='ideas'){next='ideapad';setIdeaMode('footage')}page=next;$$('.page').forEach(p=>p.classList.toggle('hidden',p.id!==next));$$('.nav').forEach(n=>n.classList.toggle('active',n.dataset.page===next));$('#page-name').textContent={library:'Library',people:'People',ideapad:'Ideas',queue:'Analysis queue',sources:'Sources & storage'}[next];if(location.hash!=='#'+next)history.replaceState(null,'','#'+next);if(next==='queue')loadJobs();if(next==='sources'){loadStats();loadPickerBatches().catch(e=>toast(e.message))}if(next==='ideapad')loadIdeaPad().catch(e=>toast(e.message,true));if(next==='people')loadPeople().catch(e=>toast(e.message,true));window.scrollTo(0,0)}
async function loadStats(){stats=await api('stats');$('#nav-count').textContent=fmt(stats.total);$('#stat-total').textContent=fmt(stats.total);$('#stat-scenes').textContent=fmt(stats.scenes);$('#stat-speech').textContent=fmt(stats.transcribed);$('#stat-cache').textContent=size(stats.temporary_bytes);$('#cache-cap').textContent=size(stats.temporary_cap)+' cap · includes reservations';$('#queue-cache-cap').textContent=size(stats.temporary_cap);if(!$('#storage-limit').dataset.dirty)$('#storage-limit').value=String(stats.temporary_cap/1024**3);const categories=$('#category'),value=categories.value;categories.innerHTML='<option value="">All categories</option>'+stats.categories.map(c=>`<option>${esc(c)}</option>`).join('');categories.value=value;
 $('#storage-details').innerHTML=`<dt>Metadata database</dt><dd>${size(stats.db_bytes)}</dd><dt>Temporary storage used / reserved</dt><dd>${size(stats.temporary_bytes)} / ${size(stats.temporary_cap)}</dd><dt>Available analysis budget</dt><dd>${size(stats.available_budget)}</dd><dt>Persistent full originals</dt><dd>None in FrameAtlas</dd><dt>Inference</dt><dd>This Mac only</dd>`;
 $('#import-history').innerHTML=stats.imports.length?stats.imports.map(i=>`<div class="history-row"><div><strong>${esc(i.label)}</strong><p class="muted">${esc(i.coverage)}</p></div><span>${fmt(i.count)} items<br><span class="muted">${new Date(i.created*1000).toLocaleDateString()}</span></span></div>`).join(''):'<p class="muted">No imports yet. Start with an existing AutoCut inventory or Google Picker selection.</p>';
 if(page==='sources')await loadCoverage().catch(e=>toast(e.message,true));
}
const coverageLimit=12;
let coverageOffset=0,coverageGeneration=0;
function coverageCount(row,status,count,label,kind,speechOnly=false){
 if(!count)return '0';
 const month=row.month||'unknown';
 const media={photo:'photos',video:'videos',audio:'audio items'}[kind]||'items';
 return `<button type="button" class="text-button" data-coverage-month="${esc(month)}" data-coverage-source="${esc(row.provider)}" data-coverage-status="${status}" data-coverage-kind="${esc(kind)}" data-speech-only="${speechOnly}" aria-label="Browse ${fmt(count)} ${speechOnly?media+' with transcript text':label+' '+media} from ${esc(sourceName(row.provider))} · ${esc(month==='unknown'?'unknown capture month':month)}">${fmt(count)}</button>`;
}
async function loadCoverage(requestedOffset=coverageOffset){
 const generation=++coverageGeneration;
 const data=await api('coverage',{provider:$('#coverage-provider').value,kind:$('#coverage-kind').value,limit:coverageLimit,offset:requestedOffset});
 if(generation!==coverageGeneration)return;
 coverageOffset=data.offset;
 const kind=data.kind||'';
 $('#coverage-totals').innerHTML=data.totals.map(r=>`<div><strong>${esc(sourceName(r.provider))}</strong><span>${fmt(r.catalogued)} catalogued</span><p class="fine">${fmt(r.ready)} ready · ${fmt(r.partial)} partial · ${fmt(r.metadata_only)} metadata only</p></div>`).join('');
 $('#coverage-months').innerHTML=data.items.map(r=>`<tr><th scope="row">${esc(r.month||'Unknown')}</th><td>${esc(sourceName(r.provider))}</td><td>${coverageCount(r,'',r.catalogued,'catalogued',kind)}</td><td>${coverageCount(r,'ready',r.ready,'ready',kind)}</td><td>${coverageCount(r,'partial',r.partial,'partially analysed',kind)}</td><td>${coverageCount(r,'metadata_only',r.metadata_only,'metadata-only',kind)}</td><td>${coverageCount(r,'',r.transcribed,'',kind,true)}</td></tr>`).join('')||'<tr><td colspan="7">No records match these filters.</td></tr>';
 $('#coverage-prev').disabled=!coverageOffset;
 $('#coverage-next').disabled=!data.has_more;
 $('#coverage-page').textContent=data.items.length?`${coverageOffset+1}–${coverageOffset+data.items.length} source-month rows`:'';
}
async function importPicker(session){
 const queue_analysis=$('#picker-queue').checked;
 const result=await api('picker_import',{session,queue_analysis},true);
 toast(result.ready?`${fmt(result.seen)} selected records imported${queue_analysis?` · ${fmt(result.queued)} new analysis jobs`:''}`:'Finish choosing media and press Done in Google first');
 return result.ready;
}
function mediaMix(counts){
 return [['photo','photo','photos'],['video','video','videos'],['other','other item','other items']]
  .filter(([kind])=>counts?.[kind]>0)
  .map(([kind,singular,plural])=>`${fmt(counts[kind])} ${counts[kind]===1?singular:plural}`).join(' · ');
}
async function loadPickerBatches(){
 const data=await api('picker_sessions');
 const unknown=data.recent_google_unclassified_analysis_completions;
 const recentMix=mediaMix(data.recent_google_picker_analysis_by_kind);
 const peakMix=mediaMix(data.peak_minimum_jobs_per_hour_by_kind);
 const paceNote=peakMix?`<p class="fine">Google Photos jobs finished in the past hour. ${fmt(data.recent_google_picker_analysis_completions)} via Picker${recentMix?` (${recentMix})`:''} · ${fmt(data.recent_google_local_analysis_completions)} via local files${unknown?` · ${fmt(unknown)} without a recorded source`:''}.</p><p class="fine">Highest minimum pace per hour across active deadlines · ${peakMix}. Each media type can peak at a different deadline.</p>`:'';
 $('#picker-batches').innerHTML=paceNote+(data.items.length?data.items.map(b=>{
  const label=b.state==='closed'?'Closed':b.expired?'Expired · choose again':b.state==='pending'?'Awaiting selection':'Selected';
  const expiry=b.expires?new Date(b.expires).toLocaleString():'Unknown — access may need refreshing';
  const range=b.captured_min&&b.captured_max?`${date(b.captured_min)} – ${date(b.captured_max)}`:'';
  const months=Object.entries(b.capture_months||{});
  const monthDetails=months.length?`<details><summary>Selected capture months · UTC</summary><p class="fine">${months.map(([m,n])=>`${esc(m)} · ${fmt(n)}`).join(' | ')}</p><p class="fine">Counts describe this selection. Coverage within a month can be partial.</p></details>`:'';
  const load=b.deadline_load;
  const batchMix=mediaMix(b.cloud_queued_jobs_by_kind);
  const deadlineMix=mediaMix(load?.cloud_jobs_due_by_kind);
  const paceMix=mediaMix(load?.minimum_jobs_per_hour_by_kind);
  const deadlineDetails=b.cloud_queued_jobs?`<p class="fine">${fmt(b.cloud_queued_jobs)} queued here need Picker access${batchMix?` (${batchMix})`:''}.${load?` ${fmt(load.cloud_jobs_due)} jobs due across batches by then${deadlineMix?` (${deadlineMix})`:''} · minimum ${fmt(load.minimum_jobs_per_hour)}/hr${paceMix?` (${paceMix} per hour)`:''}.`:''}</p>`:'';
  return `<article class="picker-batch"><div class="batch-heading"><strong>${fmt(b.selected_count)} selected items</strong><span>${esc(label)}</span></div>${range?`<p>${esc(range)} <span class="fine">UTC</span></p>`:''}<p class="fine">${fmt(b.added_count)} new records · ${fmt(b.linked_assets)} currently linked · ${fmt(b.analysed_assets)} linked items analysed</p>${b.state==='closed'?'<p class="fine">Receipt retained after closing cloud access.</p>':`<p class="fine">${fmt(b.pending_jobs)} pending jobs · Expires ${esc(expiry)}</p>`}${deadlineDetails}${b.cloud_queued_jobs?`<button class="text-button" data-picker-queue="${esc(b.id)}">Show queued media</button>`:''}${monthDetails}<details><summary>Batch reference</summary><code>${esc(b.id)}</code></details><div class="batch-actions">${b.state==='pending'?`<button class="button small" data-batch-import="${esc(b.id)}">Check selection</button>`:''}${b.state!=='closed'?`<button class="text-button" data-batch-close="${esc(b.id)}" ${b.can_close?'':'disabled title="Finish the pending jobs before closing"'}>Close finished batch</button>`:''}</div></article>`;
 }).join(''):'<p class="muted">No saved selection batches yet.</p>')+(data.truncated?`<p class="fine">Showing ${fmt(data.items.length)} of ${fmt(data.total)} batches; active batches appear first.</p>`:'');
 $$('[data-batch-close]').forEach(b=>b.onclick=safe(async()=>{b.disabled=true;try{await api('picker_delete',{session:b.dataset.batchClose},true);await loadPickerBatches();toast('Selection batch closed')}catch(e){b.disabled=false;throw e}}));
 $$('[data-batch-import]').forEach(b=>b.onclick=safe(async()=>{b.disabled=true;try{await importPicker(b.dataset.batchImport);await Promise.all([loadPickerBatches(),loadStats()])}catch(e){b.disabled=false;throw e}}));
 $$('[data-picker-queue]').forEach(b=>b.onclick=safe(async()=>{
  b.disabled=true;
  try{await showPickerQueue(b.dataset.pickerQueue)}finally{b.disabled=false}
 }));
}
const pickerQueueLimit=20;
let pickerQueueGeneration=0;
async function showPickerQueue(session,offset=0){
 const generation=++pickerQueueGeneration;
 const params={offset,limit:pickerQueueLimit};
 const data=session?await api('picker_queued_items',{...params,session}):await api('picker_reselection',params);
 if(generation!==pickerQueueGeneration)return;
 const dialog=$('#picker-queue-dialog');
 if(offset&&!dialog.open)return;
 $('#picker-queue-title').textContent=session?'Queued Google Photos media':'Media waiting for Google access';
 const guidance=!session
  ?'Finish any pending selection, or select the listed files in a new Picker batch and import it. Matching items keep their existing jobs.'
  :data.needs_reselection
  ?'Choose these files again in a new Picker selection, then import it. Matching items keep their existing jobs.'
  :'These files still have queued jobs that depend on this selection. Refresh access if the batch expires before they finish.';
 const rows=data.items.map(item=>{
  const captured=Date.parse(item.captured);
  const when=Number.isFinite(captured)?new Date(captured).toISOString():'Capture time unknown';
  const reason={missing:'No selection',pending:'Selection incomplete',closed:'Selection closed',expired:'Selection expired'}[item.reason];
  return `<li><strong>${esc(item.filename)}</strong><br><span class="muted">${esc(when)} · ${esc(item.kind)} · ${fmt(item.queued_jobs)} queued${reason?' · '+reason:''}</span></li>`;
 }).join('');
 $('#picker-queue-content').innerHTML=`<p>${fmt(data.total)} items · ${fmt(data.queued_jobs)} queued jobs</p>
  <p class="fine">${guidance} Capture times are UTC.</p>
  ${rows?`<ul class="picker-queue-list">${rows}</ul>`:'<p class="muted">'+(!session&&!data.total?'No queued media is waiting for Google access.':'No queued media on this page.')+'</p>'}
  <div class="pagination"><button class="button small" id="picker-queue-prev" ${offset?'':'disabled'}>← Previous</button>
  <span aria-live="polite">${rows?`${offset+1}–${offset+data.items.length} of ${fmt(data.total)}`:''}</span>
  <button class="button small" id="picker-queue-next" ${data.has_more?'':'disabled'}>Next →</button></div>`;
 $('#picker-queue-prev').onclick=safe(()=>showPickerQueue(session,Math.max(0,offset-pickerQueueLimit)));
 $('#picker-queue-next').onclick=safe(()=>showPickerQueue(session,offset+pickerQueueLimit));
 if(!dialog.open)dialog.showModal();
}
function resetLibraryFilters(){
 provider='';bounds='';captureMonth='';personFilter=null;placeFilter=null;offset=0;
 ['query','kind','status','search-scope','category','source-access','speech-state','since','until','gps-bounds'].forEach(id=>$('#'+id).value='');
 $$('[data-source]').forEach(b=>b.classList.toggle('selected',!b.dataset.source));
}
function filters(){const text=$('#query').value;const exact=$('#search-scope').value==='filename';return{query:exact?'':text,filename:exact&&text.trim()?text:'',provider,kind:$('#kind').value,status:$('#status').value,speech_only:$('#search-scope').value==='speech',scene_only:$('#search-scope').value==='scenes',category:$('#category').value,source_access:$('#source-access').value,speech_state:$('#speech-state').value,since:$('#since').value,until:$('#until').value,bounds,capture_month:captureMonth,person:personFilter?String(personFilter.id):'',place:placeFilter?placeFilter.ids:'',limit:24,offset}}
let storySearchScope=null;
async function search(related=false){
 const generation=++searchGeneration, {limit,offset:pageOffset,...criteria}=filters();
 const button=$('#search-related'), grid=$('#media-grid'), note=$('#search-phrases');
 const exact=$('#search-scope').value==='filename';
 button.classList.toggle('hidden',exact);
 $$('.example').forEach(example=>example.classList.toggle('hidden',exact));
 $('#query').placeholder=exact?'Enter a complete filename, including the extension…':criteria.scene_only?'Search within scene captions…':'Search scenes, transcripts, filenames or tags…';
 $('#search-hint-text').textContent=exact?'Use source or date filters to distinguish files with the same name.':criteria.scene_only?'All search terms must match the same scene caption.':'Search matches indexed words.';
 $('#capture-month-filter').classList.toggle('hidden',!captureMonth);
 $('#capture-month-label').textContent=captureMonth==='unknown'?'Unknown capture month':`Capture month ${captureMonth}`;
 relatedPending=related;
 button.disabled=related;
 button.textContent=related?'Finding related wording…':'Search related wording locally';
 grid.classList.add('loading');
 $('#ideas-results-scope').disabled=true;
 try{
  const response=related?await api('search_related',criteria,true):await api('search',{...criteria,limit,offset:pageOffset});
  if(generation!==searchGeneration)return;
  relatedResults=related;
  results=response.items;
  total=related?results.length:response.total;
  renderCards();
  $('#result-count').textContent=fmt(total)+' items';
  $('#results-title').textContent=related?'Related search results':personFilter?`With ${personFilter.name}`:placeFilter?`At ${placeFilter.name}`:(criteria.query||criteria.filename)?'Search results':provider?sourceName(provider):'All media';
  note.classList.toggle('hidden',!related);
  note.textContent=response.phrases?.length?'Also searched '+response.phrases.map(phrase=>'“'+phrase+'”').join(', ')+'.':'The local model returned no additional wording.';
  $('#prev').disabled=related||pageOffset===0;
  $('#next').disabled=related||pageOffset+limit>=total;
  $('#page-info').textContent=related?'Up to 24 indexed matches':total?`${pageOffset+1}–${Math.min(pageOffset+limit,total)} of ${fmt(total)}`:'0 items';
  $('#clear-filters').classList.toggle('hidden',!Object.values(criteria).some(Boolean));
  storySearchScope=!related&&total&&Object.values(criteria).some(Boolean)?criteria:null;
  $('#ideas-results-scope').classList.toggle('hidden',!storySearchScope);
 }catch(error){
  if(generation===searchGeneration)throw error;
 }finally{
  if(generation===searchGeneration){
   relatedPending=false;
   button.disabled=false;
   button.textContent='Search related wording locally';
   grid.classList.remove('loading');
   $('#ideas-results-scope').disabled=false;
  }
 }
}
function renderMatches(asset){
 const matches=asset.matches||[];
 const types=new Set();
 const wording=asset.matched_query?`<p class="fine">Matched “${esc(asset.matched_query)}”</p>`:'';
 return wording+matches.map((m,i)=>{
  const type=m.kind||'scene';
  if(types.has(type))return '';
  types.add(type);
  const label=type==='speech'?'Speech':type==='directional'?'360° sample':'Scene';
  const excerpt=m.matched_text||m.caption.replace(/^Sampled at [\d.]+ seconds · /,'');
  return `<button class="match" data-open="${asset.id}" data-match="${i}" title="${esc(excerpt)}">${label} ${esc(matchTime(m))} · ${esc(excerpt.slice(0,65))}</button>`;
 }).join('');
}
function renderCards(){const grid=$('#media-grid');grid.innerHTML=results.length?results.map(a=>`<article class="media-card ${selected.has(a.id)?'selected':''}" data-id="${a.id}"><span class="card-source">${esc(sourceName(a.provider))}${(a.metadata.is_360||a.analysis.is_360)?' · 360°':''}</span><input class="card-check" type="checkbox" aria-label="Select ${esc(a.filename)}" data-id="${a.id}" ${selected.has(a.id)?'checked':''}><button class="card-visual" data-open="${a.id}" aria-label="Open ${esc(a.filename)}">${a.thumbnail?`<img loading="lazy" src="/thumb/${esc(a.thumbnail)}" alt="Preview of ${esc(a.filename)}">`:`<span class="media-symbol">${a.kind==='video'?'▷':a.kind==='audio'?'≋':'▧'}</span><span class="preview-label">${a.status==='inventory'?'METADATA ONLY':'PREVIEW NOT STORED'}</span>`}${a.kind!=='photo'&&a.duration?`<span class="card-duration">${clock(a.duration)}</span>`:''}</button><div class="card-body"><button class="card-title" data-open="${a.id}" title="${esc(a.filename)}">${esc(a.filename)}</button><div class="card-meta"><span>${date(a.captured)}</span><span>${a.bytes?size(a.bytes):a.kind}</span></div><p class="card-summary">${esc(a.summary||(a.status==='inventory'?'Ready to discover what’s inside.':'No visual summary. Open for transcript and details.'))}</p><div class="card-bottom"><span class="status ${esc(a.status)}">${a.status==='ready'?'Analysed · unverified':a.status==='partial'?'Partial analysis':'Metadata only'}</span><span class="card-tags">${esc(a.tags.slice(0,2).join(' · '))}</span></div>${renderMatches(a)}</div></article>`).join(''):`<div class="empty"><p class="eyebrow">NOTHING FOUND YET</p><h2>No matching media</h2><p>Try fewer words, clear a filter, or analyse more of your inventory.</p></div>`;
 $$('[data-open]').forEach(b=>b.onclick=safe(()=>{
  const match=b.dataset.match===undefined?undefined:
   results.find(a=>a.id===b.dataset.open)?.matches?.[Number(b.dataset.match)];
  return openDetail(b.dataset.open,match,$('#search-scope').value==='speech');
 }));$$('.card-check').forEach(c=>c.onchange=()=>{c.checked?selected.set(c.dataset.id,results.find(a=>a.id===c.dataset.id)?.filename||c.dataset.id):selected.delete(c.dataset.id);c.closest('.media-card').classList.toggle('selected',c.checked);updateSelection()});updateSelection();}
function updateSelection(){const n=selected.size;$('#compare-selected').classList.toggle('hidden',n!==2);$('#analyze-selected').classList.toggle('hidden',!n);$('#analyze-selected').textContent=`Analyse ${n} selected`;$('#ideas-selected').classList.toggle('hidden',!n);$('#select-page').textContent=results.length&&results.every(a=>selected.has(a.id))?'Deselect page':'Select page'}
async function compareSelectedSources(){
 const [first,second]=selected.keys();
 const data=await api('compare_sources',{first,second});
 const labels={
  same_bytes:'The analysed files had identical bytes.',
  different_bytes:'The analysed files had different bytes.',
  unknown:'FrameAtlas lacks exact file evidence for one or both records.'
 };
 $('#source-comparison-result').innerHTML=`<p>${esc(labels[data.result])}</p><p class="fine">This compares the files read during analysis. Cloud records and retrieval links are kept separately.</p><ul>${data.items.map(item=>`<li><strong>${esc(item.filename)}</strong><br><span class="fine">${esc(sourceName(item.provider))} · ${esc(date(item.captured))} · ${item.file_fingerprint?size(item.file_fingerprint.bytes):'No file fingerprint'}</span></li>`).join('')}</ul>`;
 $('#source-comparison').showModal();
}
let detailGeneration=0;
const citationTime = ref => ref.sample_time!=null
 ? `360° sample at ${Number(ref.sample_time).toFixed(3)} s`
 : `${Number(ref.start).toFixed(3)}–${Number(ref.end).toFixed(3)} s`;
async function openDetail(id,match,transcriptOnly=false){
 const generation=++detailGeneration;
 const params={id};
 const sceneReference=match?.kind==='citation'||Boolean(match?.scene_key);
 if(sceneReference){
  params.start=match.start;
  params.end=match.end;
  if(match.sample_time!=null)params.sample_time=match.sample_time;
  if(match.scene_key)params.scene_key=match.scene_key;
  if(match.source_part?.filename)params.part_filename=match.source_part.filename;
 }
 const asset=await api('asset',params);
 if(generation!==detailGeneration)return;
 if(sceneReference)match={...match,kind:'citation',label:match.kind==='citation'?'Story citation':'Search match',
                         ordinal:asset.citation_match?.ordinal};
 currentAsset=asset;
 renderDetail(match,transcriptOnly);
 const dialog=$('#detail');
 if(!dialog.open)dialog.showModal();
 dialog.scrollTop=0;
 if(!match)return;
 requestAnimationFrame(()=>{
  if(!dialog.open||generation!==detailGeneration)return;
  const target=match.kind==='speech'?dialog.querySelector('[data-speech-hit]'):
   Number.isInteger(match.ordinal)?dialog.querySelector(`[data-scene="${match.ordinal}"]`):null;
  if(target){
   target.scrollIntoView({block:'center'});
   if(match.kind==='speech'||match.kind==='citation')target.focus({preventScroll:true});
  }
 });
}
function factsLine(f){if(!f)return'';const w=f.when,bits=[];
 if(w&&w.time)bits.push(`${w.weekday} ${w.time}`+(w.basis.startsWith('camera')?' (camera clock)':''));else if(w)bits.push(w.weekday);
 if(f.camera)bits.push('📷 '+f.camera.label);else if(f.device)bits.push(`📷 ${f.device.label} (filename guess)`);
 if(f.place)bits.push('📍 '+(f.place.label||`${f.place.latitude.toFixed(4)}, ${f.place.longitude.toFixed(4)}`));
 if(f.picture&&f.picture.width)bits.push(`${f.picture.width}×${f.picture.height}${f.picture.fps?' · '+(Math.round(f.picture.fps*100)/100)+' fps':''}`);
 return `<p class="fine facts-line">${esc(bits.join(' · '))}${!f.place&&f.place_status?`<br><span title="${esc(f.place_status)}">📍 No place recorded</span>`:''}</p>`}
function renderDetail(match,transcriptOnly=false){const a=currentAsset;$('#detail-content').innerHTML=`<div class="detail-top"><div class="detail-preview">${a.thumbnail?`<img src="/thumb/${esc(a.thumbnail)}" alt="${esc(a.filename)}">`:'▧'}</div><div><p class="eyebrow">${esc(sourceName(a.provider))}${(a.metadata.is_360||a.analysis.is_360)?' · 360° RECORDING':''}</p><h2>${esc(a.filename)}</h2><p>${date(a.captured)} · ${a.kind} ${a.bytes?'· '+size(a.bytes):''}</p>${factsLine(a.facts)}${peopleLine(a.people)}${placesLine(a.places)}<p>${esc(a.summary||'This item has metadata only. Analyse it to index its scenes and spoken words.')}</p><div class="tag-list">${a.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div><div class="detail-actions"><button class="button primary" id="detail-analyze">Analyse locally</button><button class="button" id="detail-source">Open cloud source ↗</button><button class="button" id="detail-export">Export metadata</button><button class="button" id="detail-retrieve">Prepare for editor</button></div></div></div><div class="tabs"><button class="active" data-tab="scenes">Scenes · ${a.scenes.length}</button><button data-tab="speech">Transcript</button><button data-tab="source">Source & provenance</button><button data-tab="edit">Correct & organise</button></div><div class="detail-section" id="detail-tab"></div>`;
 $('#detail-retrieve').onclick=safe(()=>showRetrievalForm());showTab(match?.kind==='speech'||transcriptOnly?'speech':'scenes',match);$$('[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));$('#detail-analyze').onclick=()=>openAnalysis([a.id]);$('#detail-source').onclick=safe(async()=>{const r=await api('resolve',{id:a.id});const link=r.cloud_url||r.search_url;if(link)window.open(link,'_blank','noopener,noreferrer');else{showTab('source');toast('Open Insta360 Studio and find the recording name shown here')}});$('#detail-export').onclick=()=>{const blob=new Blob([JSON.stringify(a,null,2)],{type:'application/json'});const u=URL.createObjectURL(blob);const link=document.createElement('a');link.href=u;link.download=a.filename+'.catalogue.json';link.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};
}
function renderScene(s,match,first){
 const d=s.details,views=Array.isArray(d.views)?d.views:[],corrected=!!d.description?.editor_corrected;
 const title=views.length&&!corrected?`360° sample at ${editTime(d.sample_time)}`:s.caption;
 const cited=match?.kind==='citation'&&s.ordinal===match.ordinal;
 const directions=views.length?`<details class="directional-views" ${(s.ordinal===match?.ordinal||s.ordinal===first&&!corrected)?'open':''}><summary>${corrected?'Original local observations':views.length+' viewing directions'} · observed at ${editTime(d.sample_time)}</summary>${views.map(v=>`<div class="direction-view"><strong>${esc(v.label)}</strong><p>${esc(v.description?.caption||'No local description available.')}</p></div>`).join('')}<p class="fine">Headings refer to the camera’s orientation. These observations describe the sampled moment; events between samples may be missed.</p></details>`:'';
 return `<div class="scene-row${cited?' cited-scene':''}" data-scene="${s.ordinal}"${cited?' tabindex="-1"':''}><div class="scene-time">${editTime(s.start)}<br>— ${editTime(s.end)}</div><div>${cited?`<p class="citation-label">${esc(match.label)} · ${esc(citationTime(match))}</p>`:''}<div class="scene-caption">${esc(title)}</div>${d.retained_editor_scene?'<p class="fine">Saved correction retained from the previous scene boundaries.</p>':''}${d.source_part?`<p class="scene-origin">Part ${Number(d.source_part.index)+1} · ${esc(d.source_part.filename)} · source ${editTime(d.source_part.sample_time??d.source_part.start)}</p>`:""}${directions}<div class="scene-detail">${esc(d.description?.detail||d.description?.setting||'')}</div><button class="text-button" data-correct="${s.ordinal}">Correct caption</button> <button class="text-button" data-copy-clip="${s.ordinal}">Copy clip reference</button> <button class="text-button" data-prepare-clip="${s.ordinal}">Prepare this scene for editor</button><div class="scene-tags">${(d.description?.actions||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join(' ')}</div>${d.description?.text?.length?`<p class="scene-detail">Text guessed in frame: ${esc(d.description.text.join(' · '))}</p>`:''}</div></div>`;
}
function renderTranscript(analysis,match){
 const words=analysis.speech?.words||[];
 const first=match?.word_start,last=match?.word_end;
 const highlighted=Number.isInteger(first)&&Number.isInteger(last)&&first>=0&&last>=first&&last<words.length
  &&Number(words[first].start)===match.start&&Number(words[last].end)===match.end
  &&words.slice(first,last+1).map(w=>w.text).join(' ').slice(0,500)===match.matched_text;
 const status=$('#speech-state').querySelector(`option[value="${CSS.escape(analysis.speech_status||'pending')}"]`);
 const state=analysis.speech_status==='not_applicable'?'Not applicable':status?.textContent||'No speech pass recorded';
 const info=`<p class="muted">${[analysis.speech_model,analysis.speech?.language,state].filter(Boolean).map(esc).join(' · ')}</p>`;
 const hit=highlighted?`<p class="speech-hit-summary">${match.source_part?esc(matchTime(match))+' · Group match':'Speech match'} ${match.start.toFixed(3)}–${match.end.toFixed(3)} s${match.speech_key?` <button class="text-button" data-copy-speech="${esc(match.speech_key)}">Copy spoken clip reference</button> <button class="text-button" data-prepare-speech="${esc(match.speech_key)}">Prepare this match for editor</button>`:''}</p>`:
  match?.kind==='speech'?'<p class="fine">Run the search again to jump to the current transcript.</p>':'';
 const lines=[];
 for(let i=0;i<words.length;i+=30){
  const chunk=words.slice(i,i+30);
  const joined=list=>esc(list.map(w=>w.text).join(' '));
  let content=joined(chunk);
  if(highlighted&&first<i+chunk.length&&last>=i){
   const from=Math.max(0,first-i),to=Math.min(chunk.length,last-i+1);
   content=[joined(chunk.slice(0,from)),
    `<mark data-speech-hit tabindex="-1">${joined(chunk.slice(from,to))}</mark>`,
    joined(chunk.slice(to))].filter(Boolean).join(' ');
  }
  lines.push(`<div class="speech-line"><time>${clock(chunk[0].start)}</time>${content}</div>`);
 }
 return info+hit+(lines.join('')||`<div class="empty">${esc(analysis.speech_error||'No spoken words have been indexed for this item.')}</div>`);
}
function showTab(tab,match){$$('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));const a=currentAsset;const panel=$('#detail-tab');if(tab==='scenes'){
 const first=a.scenes.find(s=>s.details.sample_key)?.ordinal;
 const stale=match?.kind==='citation'&&!Number.isInteger(match.ordinal)
  ?`<div class="notice compact">${esc(match.label)} (${esc(citationTime(match))}) could not be confirmed in the current scene index.</div>`:'';
 panel.innerHTML=stale+`<div class="notice compact">Local model observations are unverified. Check identities, readable text and landmarks before using them in narration.${a.analysis.coverage_warning?' '+esc(a.analysis.coverage_warning):''}</div>`+(a.scenes.length?a.scenes.map(s=>renderScene(s,match,first)).join(''):'<div class="empty"><h2>No scenes indexed yet</h2><p>Queue local analysis to describe this item.</p></div>');}
 else if(tab==='speech'){
  const replace=a.analysis.speech_status==='complete';
  const multipart=Boolean(a.analysis.parts||a.locator.recording_parts);
  const recoverable=multipart?a.analysis.speech_status==='failed'&&a.analysis.parts?.some(p=>p.analysis?.speech_status==='failed')
   :replace||['failed','no_speech'].includes(a.analysis.speech_status);
  const retry=recoverable&&a.analysis.source_bytes&&a.analysis.probe?.duration&&['video','audio'].includes(a.kind);
  panel.innerHTML=(retry?`<button class="button small" id="retry-speech">${multipart?'Retry failed parts':replace?'Re-transcribe locally':'Retry transcript'}</button>`:'')+renderTranscript(a.analysis,match);
 }
 else if(tab==='source'){panel.innerHTML=`<h3>Source locator</h3>${a.position?`<p class="fine">${esc(a.position.source)} · ${esc(a.position.latitude)}, ${esc(a.position.longitude)}</p>`:''}<p class="muted">Use the stable filename and provider ID to retrieve a source again. A Picker ID is not assumed to be a Google Photos page ID.</p><pre>${esc(JSON.stringify({id:a.id,provider:a.provider,source_id:a.source_id,filename:a.filename,cloud_url:a.cloud_url,locator:a.locator,metadata:a.metadata},null,2))}</pre><h3>Analysis provenance</h3><pre>${esc(JSON.stringify(a.analysis,null,2))}</pre><button class="button" id="source-plan">Show retrieval plan</button><pre id="retrieval-plan" class="hidden"></pre>`;$('#source-plan').onclick=safe(async()=>{const r=await api('resolve',{id:a.id});$('#retrieval-plan').textContent=JSON.stringify(r,null,2);$('#retrieval-plan').classList.remove('hidden')});}
 else if(tab==='edit'){panel.innerHTML=`<form class="detail-edit" id="edit-form"><label>Correct the searchable summary</label><textarea id="edit-summary" rows="4">${esc(a.summary)}</textarea><label>Tags, separated by commas</label><input id="edit-tags" value="${esc(a.tags.join(', '))}"><button class="button primary">Save corrections</button><p class="fine">Original machine output remains in scene provenance. Corrections update your searchable summary and tags.</p></form><hr><form class="detail-edit" id="attach-form"><label>Associate an existing original (read-only)</label><input id="attach-path" placeholder="/Users/…/original.mov"><button class="button">Link original</button></form>`;$('#edit-form').onsubmit=safe(async e=>{e.preventDefault();currentAsset=await api('annotate',{id:a.id,summary:$('#edit-summary').value,tags:$('#edit-tags').value.split(',').map(s=>s.trim()).filter(Boolean)},true);toast('Catalogue corrections saved');renderDetail();search()});$('#attach-form').onsubmit=safe(async e=>{e.preventDefault();await api('attach_local',{id:a.id,path:$('#attach-path').value},true);toast('Original linked read-only')});
  if(a.provider==='google_photos'&&a.locator.picker_id===a.source_id&&!a.source_id.startsWith('takeout:')&&['photo','video'].includes(a.kind))showTakeoutLocationForm(a,panel);
 }
}
function showTakeoutLocationForm(asset,panel){
 const linked=asset.metadata.takeout_location?.asset_id;
 panel.insertAdjacentHTML('beforeend',`<hr><form class="detail-edit" id="takeout-location-form">
  <label for="takeout-location-id">Link a verified Takeout location</label>
  <p class="fine">Confirm the Takeout record describes the same media. Its catalogue ID is under Source &amp; provenance.</p>
  <input id="takeout-location-id" value="${esc(linked||'')}" required pattern="[a-f0-9]{24}">
  <button class="button" value="link">Link location</button>
  ${linked?'<button class="text-button" value="remove" formnovalidate>Remove location link</button>':''}
 </form>`);
 $('#takeout-location-form').onsubmit=safe(async e=>{
  e.preventDefault();
  const remove=e.submitter?.value==='remove';
  const takeout_id=remove?'':$('#takeout-location-id').value.trim();
  await api('link_takeout_location',{picker_id:asset.id,takeout_id},true);
  const updated=await api('asset',{id:asset.id});
  if($('#detail').open&&currentAsset?.id===asset.id){currentAsset=updated;showTab('source')}
  toast(remove?'Takeout location link removed':'Takeout location linked');
  await search();
 });
}
function studioJob(j){
 const t=j.result?.studio_download;if(!t)return '';
 if(t.state==='removed')return '<p class="fine">Temporary Studio source removed. Catalogue metadata and any requested editor copy are retained.</p>';
 if(t.state==='released'&&j.state==='running')return `<p class="fine">Analysis saved. Freed ${esc(size(t.released_bytes))} for the remaining Studio downloads.</p>`;
 const active=['running','cancelling'].includes(j.state);
 const batch=t.batch?`<strong>${t.batch.length} recordings in this batch</strong>
  <ul>${t.batch.map(item=>`<li>${esc(item.filename)}</li>`).join('')}</ul>`:'';
 const preflight=t.batch&&active&&t.state==='waiting'
  ?`<p>Before starting the batch in Studio, click the first recording by itself to clear the previous selection, then add the other listed recordings. Cancel the Download dialog and reselect if it shows a count other than ${t.batch.length}.</p>`:'';
 return `<div class="job-result"><strong>Temporary Studio source · ${esc(size(t.quota_bytes))} hard limit</strong><span>${esc(t.filename)}</span>${batch}${preflight}${t.expected_parts>1?`<p>All ${Number(t.expected_parts)} camera parts are required.</p>`:""}<code>${esc(t.destination)}</code><p>${esc(t.instructions)}</p>${active?`<button class="button small" data-stage-copy="${j.id}">Copy destination</button>${t.state==='waiting'?`<button class="button small" data-studio-confirm="${j.id}">${t.batch?'Confirm remaining batch':'Studio download finished'}</button>${t.batch?`<button class="button small" data-studio-confirm="${j.id}" data-individual>Confirm this recording</button>`:''}`:'<span>Source validated. Local processing continues.</span>'}`:`<span>Cancel any active transfer in Studio first.</span><button class="button small" data-studio-cleanup="${j.id}">Clean temporary source</button>`}</div>`;
}
function clipTiming(clip){
 const start=editTime(clip.source_start),end=editTime(clip.source_end);
 return `Source time ${start===end?start:start+'–'+end} · ${esc(clip.source_filename)}`;
}
function editorJob(job){
 const result=job.result;
 if(job.task!=='retrieve'||job.state!=='done'||!result?.path)return '';
 const clip=result.clip,properties=result.indexed_source_properties_checked;
 const partPath=clip?.source_path&&clip.source_path!==result.path?`<code>${esc(clip.source_path)}</code>`:'';
 let verification='';
 if(result.indexed_source_verification==='matched'){
  verification=result.recording_parts?.length?'Referenced camera part verified against the index.':'File bytes verified against the indexed source.';
 }else if(result.indexed_source_verification==='different_bytes'){
  verification='This file differs from the indexed copy. The catalogue source link was kept.';
 }else if(result.indexed_source_verification==='no_fingerprint'){
  verification='Exact byte verification unavailable for this source.';
 }
 return `<div class="job-result"><strong>Ready for editor import</strong>
  <code>${esc(result.path)}</code>${(result.related_files||[]).map(p=>`<code>${esc(p)}</code>`).join('')}
  ${clip?`<span>${clipTiming(clip)}</span>${partPath}`:''}
  ${verification?`<span>${esc(verification)}</span>`:''}
  ${result.indexed_source_verification==='no_fingerprint'&&properties?.length?`<span>Matched recorded properties · ${esc(properties.join(' · '))}</span>`:''}
  ${result.local_source_mismatch_recovered?'<span>Used Google Photos because the linked local file differed.</span>':''}
  <span>FrameAtlas will not auto-delete this editor source.</span>
  <div class="editor-copy-actions"><button type="button" class="button small" data-editor-job="${job.id}" data-editor-copy="path">${clip?'Copy clip source path':'Copy editor path'}</button>
  ${clip?`<button type="button" class="button small" data-editor-job="${job.id}" data-editor-copy="reference">Copy timed reference</button>`:''}</div></div>`;
}
function bindEditorCopy(jobs){
 $$('[data-editor-copy]').forEach(button=>button.onclick=safe(async()=>{
  const job=jobs.find(row=>row.id===Number(button.dataset.editorJob));
  const {path,clip,asset_id,provider,source_id,indexed_source_verification,indexed_source_properties_checked}=job.result;
  const isReference=button.dataset.editorCopy==='reference';
  const value=isReference
   ?JSON.stringify({asset_id,provider,source_id,...clip,indexed_source_verification,indexed_source_properties_checked},null,2)
   :clip?.source_path||path;
  await navigator.clipboard.writeText(value);
  toast(isReference?'Timed reference copied':'Editor source path copied');
 }));
}
async function loadJobs(){const r=await api('jobs');$('#queue-picker-reselection').classList.toggle('hidden',!r.waiting_for_selection);const active=r.items.filter(j=>['queued','running','cancelling'].includes(j.state));$('#nav-jobs').textContent=r.counts?((r.counts.queued||0)+(r.counts.running||0)+(r.counts.cancelling||0)||''):active.length||'';$('#pause-queue').textContent=r.paused?'Resume queue':'Pause queue';$('#pause-queue').dataset.paused=String(r.paused);$('#job-counts').textContent=Object.entries(r.counts||{}).filter(([state])=>state!=='failed').map(([state,n])=>fmt(n)+' '+state).join(' · ')+(r.counts?.failed?' · '+fmt(r.unresolved_failures)+' need attention · '+fmt(r.counts.failed-r.unresolved_failures)+' recovered attempts':'')+(r.waiting_for_selection?' · '+fmt(r.waiting_for_selection)+' awaiting Google Photos selection':'')+(r.paused?' · Queue paused':'')+' · Showing up to 100 jobs, with current work first.';$('#jobs-list').innerHTML=r.items.length?r.items.map(j=>`<article class="job"><div class="job-state"><span class="status ${esc(j.recovered_by?'ready':j.state)}">${esc(j.recovered_by?'recovered':j.state)}</span></div><div class="job-body"><div class="job-name">${esc(j.filename)}</div><p class="job-stage">${j.task==='retrieve'?'Editing retrieval · ':j.task==='speech'?'Transcript retry · ':''}${esc(j.recovered_by?'Recovered by job #'+j.recovered_by:j.waiting_for_selection?'Select this item in Google Photos to resume':j.stage||'Waiting for local worker')} · #${j.id}</p>${j.error?`<div class="job-error">${esc(j.error)}</div>`:''}${studioJob(j)}${editorJob(j)}</div><div class="job-actions"><button class="text-button" data-job-asset="${j.asset_id}">Details</button>${['queued','running'].includes(j.state)?`<button class="button small" data-cancel="${j.id}">Cancel</button>`:!j.recovered_by&&['failed','cancelled'].includes(j.state)?`<button class="button small" data-retry="${j.id}">Retry</button>`:''}</div></article>`).join(''):'<div class="empty"><h2>A quiet queue</h2><p>Select media in your library to start local analysis.</p></div>';bindEditorCopy(r.items);$$('[data-cancel]').forEach(b=>b.onclick=safe(async()=>{await api('cancel',{id:Number(b.dataset.cancel)},true);await loadJobs()}));$$('[data-retry]').forEach(b=>b.onclick=safe(async()=>{const j=r.items.find(j=>j.id===Number(b.dataset.retry));if(j.task==='retrieve'){const opts=JSON.parse(j.options);await api('retrieve',{id:j.asset_id,destination:opts.destination||'',...opts.reference},true)}else if(j.task==='speech')await api('retry_speech',{id:j.asset_id,...JSON.parse(j.options)},true);else await api('enqueue',{id:j.asset_id},true);await loadJobs()}));$$('[data-job-asset]').forEach(b=>b.onclick=safe(()=>openDetail(b.dataset.jobAsset)));$$('[data-stage-copy]').forEach(b=>b.onclick=safe(async()=>{const j=r.items.find(j=>j.id===Number(b.dataset.stageCopy));await navigator.clipboard.writeText(j.result.studio_download.destination);toast('Temporary destination copied')}));$$('[data-studio-confirm]').forEach(b=>b.onclick=safe(async()=>{await api('studio_confirm',{id:Number(b.dataset.studioConfirm),individual:b.hasAttribute('data-individual')},true);toast('Download confirmed; local validation follows');await loadJobs()}));$$('[data-studio-cleanup]').forEach(b=>b.onclick=safe(async()=>{await api('studio_cleanup',{id:Number(b.dataset.studioCleanup)},true);toast('Owned temporary source cleaned');await loadJobs()}));}
let ideaScope=[], ideaSearch=null;
function setIdeaScope(entries,searchScope=null){
 ideaScope=entries;
 ideaSearch=searchScope;
 const exact=entries.length>0;
 $('#idea-scope').classList.toggle('hidden',!exact&&!searchScope);
 $('#idea-scope-exact').classList.toggle('hidden',!exact);
 const description=$('#idea-search-description');
 description.classList.toggle('hidden',!searchScope);
 const query=searchScope?.query||searchScope?.filename;
 description.textContent=searchScope
  ?`Use up to 18 files with scene captions from the Library results${query?' for “'+query+'”':''}. The Library filters still apply.`
  :'';
 $('#idea-scope-count').textContent=`Use only ${fmt(entries.length)} selected files`;
 $('#idea-scope-files').innerHTML=entries.map(([,filename])=>`<li>${esc(filename)}</li>`).join('');
 const expand=$('#idea-expand');
 expand.disabled=exact||Boolean(searchScope);
 if(expand.disabled)expand.checked=false;
}

async function init(){csrf=(await (await fetch('/api/session')).json()).csrf;$$('.nav').forEach(b=>b.onclick=()=>navigate(b.dataset.page));$('.brand').onclick=e=>{e.preventDefault();navigate('library')};$('#add-media').onclick=()=>navigate('sources');$('#coverage-link').onclick=()=>{navigate('sources');$('#coverage-panel').scrollIntoView()};$('#search-form').onsubmit=safe(async e=>{e.preventDefault();offset=0;await search()});$('#search-related').onclick=safe(()=>search(true));$$('.example').forEach(b=>b.onclick=safe(async()=>{$('#query').value=b.dataset.query;offset=0;await search()}));$$('[data-source]').forEach(b=>b.onclick=safe(async()=>{provider=b.dataset.source;$$('[data-source]').forEach(x=>x.classList.toggle('selected',x===b));offset=0;await search()}));['kind','status','search-scope','category','source-access','speech-state','since','until'].forEach(id=>$('#'+id).onchange=safe(async()=>{offset=0;await search()}));$('#date-toggle').onclick=()=>$('#date-row').classList.toggle('hidden');$('#date-clear').onclick=safe(async()=>{$('#since').value=$('#until').value='';offset=0;await search()});$('#clear-filters').onclick=safe(async()=>{resetLibraryFilters();await search()});$('#prev').onclick=safe(async()=>{offset=Math.max(0,offset-24);await search()});$('#next').onclick=safe(async()=>{offset+=24;await search()});$('#select-page').onclick=()=>{const all=results.every(a=>selected.has(a.id));results.forEach(a=>all?selected.delete(a.id):selected.set(a.id,a.filename));renderCards()};$('#analyze-selected').onclick=()=>openAnalysis([...selected.keys()]);$('#detail-close').onclick=()=>$('#detail').close();$('#detail').onclick=safe(handleDetailClick);$('#refresh-jobs').onclick=safe(loadJobs);$('#pause-queue').onclick=safe(async()=>{await api('queue_control',{paused:$('#pause-queue').dataset.paused!=='true'},true);await loadJobs()});
 $('#compare-selected').onclick=safe(compareSelectedSources);
 ['show-picker-reselection','queue-picker-reselection'].forEach(id=>$('#'+id).onclick=safe(()=>showPickerQueue(null)));
 $('#coverage-provider').onchange=safe(()=>loadCoverage(0));
 $('#coverage-kind').onchange=safe(()=>loadCoverage(0));
 $('#refresh-coverage').onclick=safe(()=>loadCoverage());
 $('#coverage-prev').onclick=safe(()=>loadCoverage(Math.max(0,coverageOffset-coverageLimit)));
 $('#coverage-months').onclick=safe(async e=>{
  const count=e.target.closest('[data-coverage-month]');
  if(!count)return;
  resetLibraryFilters();
  provider=count.dataset.coverageSource;captureMonth=count.dataset.coverageMonth;
  $('#kind').value=count.dataset.coverageKind;
  $('#status').value=count.dataset.coverageStatus;
  $('#search-scope').value=count.dataset.speechOnly==='true'?'speech':'';
  $$('[data-source]').forEach(b=>b.classList.toggle('selected',b.dataset.source===provider));
  navigate('library');
  await search();
  $('#results-title').focus({preventScroll:true});
 });
 $('#capture-month-clear').onclick=safe(async()=>{captureMonth='';offset=0;await search()});
 $('#coverage-next').onclick=safe(()=>loadCoverage(coverageOffset+coverageLimit));
 $('#storage-limit').onchange=()=>{$('#storage-limit').dataset.dirty='true'};
 $('#storage-limit-form').onsubmit=safe(async e=>{e.preventDefault();const button=e.submitter;if(button)button.disabled=true;try{const r=await api('storage_limit',{gib:Number($('#storage-limit').value)},true);delete $('#storage-limit').dataset.dirty;await loadStats();toast(size(r.temporary_cap)+' temporary limit saved for new jobs')}finally{if(button)button.disabled=false}});
 $('#native-import').onclick=safe(async e=>{e.target.disabled=true;try{const data=await api('import_native',{},true);toast(`Metadata refreshed: ${fmt(data.google_photos)} Google Photos · ${fmt(data.insta360)} Insta360${data.metadata_only?` · ${fmt(data.metadata_only)} saved metadata-only records`:""}`);await loadStats();await search()}finally{e.target.disabled=false}});
 $('#manifest-file').onchange=safe(async e=>{const f=e.target.files[0];if(!f)return;if(f.size>8*1024**2)throw Error('Manifest exceeds 8 MiB');const data=await api('import_manifest',JSON.parse(await f.text()),true);toast(`${data.added} records added; no originals copied`);await loadStats();await search();e.target.value=''});
 $('#takeout-import').onclick=safe(async e=>{
  e.target.disabled=true;
  const report=$('#takeout-result');
  report.classList.add('hidden');
  try{
   const data=await api('import_takeout',{path:$('#takeout-path').value},true);
   const failed=data.failed_archives||0;
   if(data.archives){
    report.textContent=data.archives.map(a=>a.filename+' · '+(a.error?'Could not import: '+a.error:fmt(a.added)+' added')).join('\n');
    report.classList.remove('hidden');
   }
   toast(`${fmt(data.seen)} sidecars read · ${fmt(data.added)} records added${failed?' · '+fmt(failed)+' archives could not be imported':''}`,!!failed);
   await loadStats();await search();
  }finally{e.target.disabled=false}
 });
 $('#picker-create').onclick=safe(async e=>{e.target.disabled=true;try{const r=await api('picker_create',{},true);$('#picker-session').classList.remove('hidden');$('#picker-id').value=r.id;$('#picker-link').href=r.picker_uri;await loadPickerBatches();sessionStorage.setItem('picker',JSON.stringify(r));toast('Picker ready. Open the link below and choose media.')}finally{e.target.disabled=false}});
 $('#refresh-picker-batches').onclick=safe(loadPickerBatches);
 const saved=sessionStorage.getItem('picker');if(saved){try{const r=JSON.parse(saved);$('#picker-session').classList.remove('hidden');$('#picker-id').value=r.id;$('#picker-link').href=r.picker_uri}catch{sessionStorage.removeItem('picker')}}
 $('#picker-import').onclick=safe(async()=>{if(!await importPicker($('#picker-id').value))return;await Promise.all([loadPickerBatches(),loadStats()]);await search()});$('#picker-close').onclick=safe(async()=>{await api('picker_delete',{session:$('#picker-id').value},true);sessionStorage.removeItem('picker');$('#picker-session').classList.add('hidden');await loadPickerBatches();toast('Picker session closed')});
 $('#check-models').onclick=safe(async e=>{e.target.disabled=true;try{const m=await api('models');$('#model-output').textContent=JSON.stringify({scene_models:m.scenes.models?.filter(x=>x.installed).map(x=>({id:x.id,installed:x.installed})),speech_models:m.speech.models?.filter(x=>x.installed).map(x=>x.id),writing:m.writing},null,2);$('#model-output').classList.remove('hidden')}finally{e.target.disabled=false}});
 $('#ideas-selected').onclick=()=>{
  if(selected.size>18){toast('Choose up to 18 files for one story',true);return}
  setIdeaScope([...selected]);
  navigate('ideas');
  $('#idea-prompt').focus();
 };
 $('#ideas-results-scope').onclick=()=>{
  if(!storySearchScope)return;
  setIdeaScope([],storySearchScope);
  navigate('ideas');
  $('#idea-prompt').focus();
 };
 $('#idea-scope-clear').onclick=()=>setIdeaScope([]);
 $('#idea-form').onsubmit=safe(async e=>{e.preventDefault();$('#idea-submit').disabled=true;$('#idea-submit').textContent='Building from your footage…';try{const data=await api('ideas',{prompt:$('#idea-prompt').value,model:$('#idea-model').value,expand_search:$('#idea-expand').checked,...(ideaScope.length?{asset_ids:ideaScope.map(([id])=>id)}:ideaSearch?{search_scope:ideaSearch}:{})},true);toast(`${data.saved_ids.length} stor${data.saved_ids.length===1?'y':'ies'} added to your ideas${data.related_search_phrases?.length?' · also searched '+data.related_search_phrases.join(', '):''}`);await showFreshIdeas(data.saved_ids)}finally{$('#idea-submit').disabled=false;$('#idea-submit').textContent='✧ Build a story'}});
 $('#gps-toggle').onclick=()=>{const visible=!$('#gps-filter').classList.toggle('hidden');$('#gps-toggle').setAttribute('aria-expanded',String(visible))};
 $('#gps-filter').onsubmit=safe(async e=>{e.preventDefault();const previous=bounds;bounds=$('#gps-bounds').value.trim();offset=0;try{await search()}catch(err){bounds=previous;throw err}});
 $('#gps-clear').onclick=safe(async()=>{$('#gps-bounds').value='';bounds='';offset=0;await search()});
 setupAnalysis();setupStudioInventory();setupIdeaPad();setupPeople();await Promise.all([loadStats(),search(),loadJobs()]);const initial=location.hash.slice(1);if(['library','people','ideapad','ideas','queue','sources'].includes(initial))navigate(initial);
 setInterval(async()=>{try{await loadJobs();await loadStats();if(page==='library'&&stats.statuses.ready!==undefined&&!$('#detail').open&&!relatedResults&&!relatedPending)await search()}catch{}},10000);
}
async function showRetrievalForm(reference={}){
 const id=currentAsset.id;
 showTab('source');
 const panel=$('#detail-tab'), loading=document.createElement('p');
 loading.className='muted';loading.textContent='Checking source availability…';panel.prepend(loading);
 let plan;
 try{plan=await api('resolve',{id,...reference});if(!loading.isConnected)return}finally{loading.remove()}
 if(!$('#detail').open||currentAsset.id!==id||!$('[data-tab="source"]').classList.contains('active'))return;
 if(panel.querySelector('.retrieval-form'))return;
 const form=document.createElement('form');form.className='detail-edit';
 form.classList.add('retrieval-form');
 const state=plan.google_selection?.state;
 const canUseCloudCopy=plan.clip&&plan.provider==='google_photos'&&plan.existing_local_available&&['selected','unknown'].includes(state);
 const status={expired:'The saved Google selection has expired.',closed:'The saved Google selection was closed.',pending:'The Google selection is still pending.',missing:'This item needs a Google selection.'}[state];
 form.innerHTML=`<h3>Prepare this source for editing</h3>${plan.clip?`<p>${clipTiming(plan.clip)}. The full source will be available with this time reference.</p>`:''}<p class="muted">${esc(plan.needs_selection&&status?status+' ':'')}${esc(plan.download_method||(plan.existing_local_available?'Reuse the existing original read-only.':'Link an existing original first.'))}</p>`;
 if(plan.needs_selection){
  form.innerHTML+='<button type="button" class="button">Open Sources & storage</button>';
  form.querySelector('button').onclick=()=>{$('#detail').close();navigate('sources')};
 }else if(plan.existing_local_available||plan.provider!=='local'){
  if(!plan.existing_local_available)form.innerHTML+='<label for="retrieval-destination">Editing folder</label><input required id="retrieval-destination" placeholder="/Users/…/Movies/My project/media">';
  else if(canUseCloudCopy)form.innerHTML+='<label for="retrieval-destination">Editing folder (optional)</label><input id="retrieval-destination" placeholder="/Users/…/Movies/My project/media"><p class="muted">If the linked file differs from the index, FrameAtlas can check and save the selected Google Photos copy here.</p>';
  form.innerHTML+='<button class="button primary">Queue priority retrieval</button>';
  form.onsubmit=safe(async e=>{e.preventDefault();const r=await api('retrieve',{id,...reference,destination:form.querySelector('input')?.value||''},true);$('#detail').close();toast('Priority retrieval queued · #'+r.id);navigate('queue')});
 }
 panel.prepend(form);
}
let studioPreview=null;
function setupStudioInventory(){
 $('#studio-screenshot').onchange=safe(async e=>{
  const file=e.target.files[0];if(!file)return;if(file.size>6*1024**2)throw Error('Screenshot exceeds 6 MiB');
  const engine=$('#studio-engine').value;e.target.disabled=true;$('#studio-preview').classList.remove('hidden');$('#studio-preview').textContent=engine==='apple_vision'?'Reading text locally on the CPU…':'Reading locally with Qwen2.5-VL… It may wait for the current GPU task.';$('#studio-add').classList.add('hidden');
  try{const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('Cannot read screenshot'));reader.readAsDataURL(file)});
   studioPreview=await api('studio_ocr',{image_base64:encoded,engine},true);$('#studio-preview').textContent=studioPreview.records.map(r=>r.filename+'  ·  '+r.created_label+(r.display_offset_minutes?'  ·  display differs '+r.display_offset_minutes+' minutes':'')).join('\n')+'\n\nCheck that Cloud Media is selected and review these names against Studio. Visible rows only; OCR remains unverified.'+(studioPreview.rejected.length?'\nRejected rows: '+studioPreview.rejected.join('; '):'');$('#studio-add').classList.toggle('hidden',!studioPreview.records.length);
  }catch(error){studioPreview=null;$('#studio-preview').textContent=error.message;throw error}finally{e.target.disabled=false;e.target.value=''}
 });
 $('#studio-add').onclick=safe(async()=>{if(!studioPreview)return;const p=studioPreview;const r=await api('studio_import',{cloud_media_selected:true,list_view:true,records:p.records,evidence_sha256:p.evidence_sha256,model:p.model},true);$('#studio-add').classList.add('hidden');toast(r.added+' cloud inventory records added; no footage downloaded');await loadStats();await search()});
}
let pendingAnalysis=[];
let pendingSpeech=null;
const interactiveAnalysisLimit=8;
function openAnalysis(ids,speech=null){pendingAnalysis=ids;pendingSpeech=speech;$('#analysis-count').textContent=speech?(speech.replace?'Re-transcribe locally':'Retry transcript'):`Analyse ${ids.length} item${ids.length===1?'':'s'}`;const studioBatch=ids.length>=2&&ids.length<=8&&ids.every(id=>results.some(a=>a.id===id&&a.provider==='insta360'&&!a.local_path&&['photo','video'].includes(a.kind)));$('#analysis-studio-group').classList.toggle('hidden',!studioBatch);$('#analysis-studio-batch').checked=false;
 for(const id of ['analysis-detail-label','analysis-detail','analysis-priority-group'])$('#'+id).classList.toggle('hidden',Boolean(speech));
 $('#analysis-submit').textContent=speech?'Queue transcript only':'Queue local analysis';
 if(speech){
  const language=$('#analysis-language'),model=$('#analysis-model');
  language.value=[...language.options].some(o=>o.value===speech.language)?speech.language:'auto';
  model.value=speech.replace&&language.value==='ml'?'whisper-medium-ml':[...model.options].some(o=>o.value===speech.model)?speech.model:'whisper-base';
 }
 $('#analysis-priority-label').textContent=ids.length<=interactiveAnalysisLimit?'Process after active work and higher-priority transfers':'Process before the background backlog';
 $('#analysis-options').showModal();
}
function setupAnalysis(){
 $('#analysis-close').onclick=()=>$('#analysis-options').close();
 $('#analysis-language').onchange=()=>{if($('#analysis-language').value==='ml')$('#analysis-model').value='whisper-medium-ml';else if($('#analysis-language').value==='en')$('#analysis-model').value='whisper-large-v3-turbo'};
 $('#analysis-form').onsubmit=safe(async e=>{
  e.preventDefault();
  const studio=$('#analysis-studio-batch').checked,priority=$('#analysis-priority').checked;
  const options={language:$('#analysis-language').value,model:$('#analysis-model').value,detail:$('#analysis-detail').value,priority};
  if(pendingSpeech){
   const {language,model}=options;
   const r=await api('retry_speech',{id:pendingAnalysis[0],language,model,replace:pendingSpeech.replace},true);
   $('#analysis-options').close();$('#detail').close();toast('Transcript job queued · #'+r.id);navigate('queue');return;
  }
  if(priority&&!studio&&pendingAnalysis.length<=interactiveAnalysisLimit)options.interactive=true;
  await api(studio?'studio_batch':'enqueue',{ids:pendingAnalysis,options},true);
  $('#analysis-options').close();if($('#detail').open)$('#detail').close();selected.clear();renderCards();toast('Queued for local analysis');navigate('queue');
 });
}
async function handleDetailClick(e){
 if(e.target===$('#detail')){$('#detail').close();return}
 const chip=e.target.closest('[data-person-open]');if(chip){$('#detail').close();await openPerson(Number(chip.dataset.personOpen));return}
 const spot=e.target.closest('[data-place-open]');if(spot){$('#detail').close();await showPlaceMedia(spot.dataset.placeOpen,spot.dataset.placeName);return}
 const unlinked=e.target.closest('[data-face-link]');if(unlinked){await linkDetailFace(Number(unlinked.dataset.faceLink));return}
 if(e.target.closest('#retry-speech')){const a=currentAsset;openAnalysis([a.id],{replace:a.analysis.speech_status==='complete',language:a.analysis.speech?.language,model:a.analysis.speech_model});return}
 const copy=e.target.closest('[data-copy-clip],[data-copy-speech],[data-prepare-clip],[data-prepare-speech]');
 if(copy){
  const asset=currentAsset,params={};
  const speechKey=copy.dataset.copySpeech??copy.dataset.prepareSpeech;
  if(speechKey){
   params.speech_key=speechKey;
  }else{
   const scene=asset.scenes.find(s=>s.ordinal===Number(copy.dataset.copyClip??copy.dataset.prepareClip));
   if(!scene?.scene_key)throw Error('Reopen this item to see its current scenes.');
   params.scene_key=scene.scene_key;
   if(scene.details.source_part?.filename)params.part_filename=scene.details.source_part.filename;
  }
  if(copy.hasAttribute('data-prepare-clip')||copy.hasAttribute('data-prepare-speech')){await showRetrievalForm(params);return}
  const plan=await api('resolve',{id:asset.id,...params});
  const {asset_id,provider,source_id,clip}=plan;
  await navigator.clipboard.writeText(JSON.stringify({asset_id,provider,source_id,...clip},null,2));
  toast('Clip reference copied');
  return;
 }
 const correct=e.target.closest('[data-correct]');
 if(correct){const ordinal=Number(correct.dataset.correct);const scene=currentAsset.scenes.find(s=>s.ordinal===ordinal);const row=correct.closest('.scene-row');if(row.querySelector('textarea'))return;const form=document.createElement('div');form.className='detail-edit';form.innerHTML=`<label>Correct this scene’s description</label><textarea rows="3">${esc(scene.caption)}</textarea><button class="button small" data-scene-save="${ordinal}">Save scene correction</button><p class="fine">Original model output is retained separately. Your correction becomes searchable.</p>`;correct.insertAdjacentElement('afterend',form);return}
 const save=e.target.closest('[data-scene-save]');
 if(save){
  const asset=currentAsset,ordinal=Number(save.dataset.sceneSave);
  const scene=asset.scenes.find(s=>s.ordinal===ordinal);
  if(!scene?.scene_key)throw Error('Reopen this item to see its current scenes.');
  const caption=save.parentElement.querySelector('textarea').value;
  save.disabled=true;
  try{
   const updated=await api('correct_scene',{id:asset.id,ordinal,scene_key:scene.scene_key,caption},true);
   if(currentAsset===asset&&$('#detail').open){currentAsset=updated;showTab('scenes')}
   toast('Scene correction saved');await search();
  }finally{save.disabled=false}
 }
}
init().catch(e=>toast('Could not start the local catalogue: '+e.message,true));
