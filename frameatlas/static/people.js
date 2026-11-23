'use strict';
// People: faces found and grouped on this Mac; name them, merge, hide, link and unlink faces, and find their media.
let peopleItems=[], peopleShown=60, peopleHidden=false, peopleSelected=new Set(), peoplePoll=null, personOpen=null;
let peopleView='people', unsortedItems=[], unsortedSelected=new Set(), unsortedTotal=0;
let personFaces=[], personSelected=new Set();
let reviewItems=[], reviewTotal=0;
let pickerResolve=null, pickerExclude=new Set(), pickerPeople=new Map(), pickerTimer=null;
const FACE_PAGE=120;
const personName=p=>p&&p.name?p.name:'Unnamed person';
const personLabel=p=>p&&p.name?p.name:`Unnamed person #${p.id}`;
const faceImg=(crop,alt)=>crop?`<img loading="lazy" src="/face/${esc(crop)}" alt="${esc(alt)}">`:'<span class="media-symbol">☺</span>';
const plural=(n,word)=>`${fmt(n)} ${word}${n===1?'':'s'}`;
function years(a,b){const x=(a||'').slice(0,4),y=(b||'').slice(0,4);return x&&y?(x===y?x:`${x}–${y}`):''}
function peopleLine(list){
 if(!list||!list.length)return'';
 const seen=new Map();list.forEach(f=>{const k=f.person_id||('f'+f.face_id);if(!seen.has(k))seen.set(k,f)});
 return `<div class="people-line">${[...seen.values()].filter(f=>!f.hidden).map(f=>f.person_id
  ?`<button class="person-chip" data-person-open="${f.person_id}" title="${esc(f.name||'Unnamed person')}">${f.crop?`<img src="/face/${esc(f.crop)}" alt="">`:''}<span>${esc(f.name||'Unnamed')}</span></button>`
  :`<button class="person-chip unlinked" data-face-link="${f.face_id}" title="Link this face to a person">${f.crop?`<img src="/face/${esc(f.crop)}" alt="">`:''}<span>Who is this?</span></button>`).join('')}</div>`;
}

// ---- linking faces: the person picker
function pickerRow(p){
 pickerPeople.set(p.id,p);
 const alike=p.likeness!=null?`${Math.round(p.likeness*100)}% alike · `:'';
 return `<button class="picker-row" type="button" data-pick="${p.id}"><span class="picker-face">${faceImg(p.cover,'')}</span><span><strong>${esc(personName(p))}</strong><small>#${p.id} · ${alike}${plural(p.faces,'face')}</small></span></button>`;
}
async function pickerSearch(){
 const data=await api('people',{limit:'40',query:$('#picker-search').value.trim()});
 const items=data.items.filter(p=>!pickerExclude.has(p.id));
 $('#picker-people').innerHTML=items.length?items.map(pickerRow).join(''):'<p class="muted">No one by that name.</p>';
}
// Ask who some faces are (or whom to merge with). Resolves {person, name}, {new_person, name} or null.
function pickPerson({title,eyebrow='LINK FACES',faces=[],exclude=[],allowNew=true}){
 if(pickerResolve)pickerDone(null);
 const d=$('#person-picker');
 pickerExclude=new Set(exclude);pickerPeople=new Map();
 $('#picker-title').textContent=title;$('#picker-eyebrow').textContent=eyebrow;
 $('#picker-faces').innerHTML=faces.slice(0,12).map(f=>`<span class="picker-face">${faceImg(f.crop,'')}</span>`).join('')+(faces.length>12?`<span class="muted">+${fmt(faces.length-12)}</span>`:'');
 $('.picker-new').classList.toggle('hidden',!allowNew);$('#picker-new-name').value='';$('#picker-search').value='';
 $('#picker-suggested-wrap').classList.add('hidden');$('#picker-people').innerHTML='<p class="muted">Loading…</p>';
 const ids=faces.map(f=>f.id).filter(Boolean).slice(0,64);
 if(ids.length)api('people_suggest',{faces:ids.join(','),limit:'6'}).then(r=>{
  const items=r.items.filter(p=>!pickerExclude.has(p.id));
  if(items.length&&d.open){$('#picker-suggested').innerHTML=items.map(pickerRow).join('');$('#picker-suggested-wrap').classList.remove('hidden')}
 }).catch(()=>{});
 pickerSearch().catch(e=>toast(e.message,true));
 if(!d.open)d.showModal();
 $('#picker-search').focus();
 return new Promise(resolve=>{pickerResolve=resolve});
}
function pickerDone(value){const resolve=pickerResolve;pickerResolve=null;if($('#person-picker').open)$('#person-picker').close();if(resolve)resolve(value)}
async function linkFaces(ids,choice){
 const args=choice.person?{faces:ids,person:choice.person}:{faces:ids,new_person:true,name:choice.name||''};
 const r=await api('faces_link',args,true);
 toast(`${plural(ids.length,'face')} linked to ${choice.person?personLabel({id:choice.person,name:choice.name}):(choice.name||'a new person')}`);
 return r;
}

// ---- the People grid
async function loadPeopleStatus(){
 const s=await api('people_status');const st=s.scan||{};
 const pct=s.previews?Math.round(s.scanned/s.previews*100):0;
 $('#people-status').innerHTML=`<div><span>PREVIEWS SCANNED</span><strong>${fmt(s.scanned)}</strong><small>of ${fmt(s.previews)} (${pct}%)</small></div><div><span>FACES</span><strong>${fmt(s.faces)}</strong><small>${fmt(s.grouped)} grouped into people</small></div><div><span>PEOPLE</span><strong>${fmt(s.people)}</strong><small>${fmt(s.named)} named</small></div><div><span>SCAN</span><strong>${s.running?(st.state==='grouping'?'Grouping…':st.state==='sorting'?'Sorting…':'Scanning…'):st.state==='failed'?'Stopped':st.state==='done'?'Done':'—'}</strong><small>${s.running&&st.rate?`${st.rate} previews/s`:st.error?esc(st.error):''}</small></div>`;
 $('#people-scan').disabled=s.running;$('#people-regroup').disabled=s.running||!s.faces;$('#review-find').disabled=s.running||!s.faces;$('#review-sort').disabled=s.running||!s.faces;
 $('#people-scan').textContent=s.scanned&&s.scanned<s.previews?'Scan the remaining previews':s.scanned?'Scan new previews':'Find faces in my library';
 clearTimeout(peoplePoll);
 if(s.running&&page==='people')peoplePoll=setTimeout(()=>loadPeople(false).catch(()=>{}),4000);
 return s;
}
function personCard(p){
 return `<article class="person-card ${peopleSelected.has(p.id)?'selected':''} ${p.hidden?'is-hidden':''}" data-person-id="${p.id}">
  <input class="card-check" type="checkbox" aria-label="Select ${esc(personName(p))}" ${peopleSelected.has(p.id)?'checked':''}>
  <button class="person-face" data-person-open="${p.id}" aria-label="Open ${esc(personName(p))}">${faceImg(p.cover,personName(p))}</button>
  <input class="person-name" value="${esc(p.name)}" placeholder="Who is this?" aria-label="Name of person ${p.id}" maxlength="120">
  <p class="fine">${plural(p.assets,'item')} · ${plural(p.faces,'face')}${years(p.first_seen,p.last_seen)?' · '+years(p.first_seen,p.last_seen):''}</p>
  <div class="person-actions"><button class="text-button" data-person-media="${p.id}">Media →</button><button class="text-button" data-person-hide="${p.id}">${p.hidden?'Show':'Hide'}</button></div>
 </article>`;
}
function showMergeButton(){$('#people-merge').classList.toggle('hidden',peopleSelected.size<2);$('#people-merge').textContent=`Merge ${peopleSelected.size} people`}
async function loadPeople(withList=true){
 const s=await loadPeopleStatus();
 if(!withList&&!s.running)withList=true;
 if(!withList)return;
 if(peopleView==='unsorted')return loadUnsorted();
 if(peopleView==='review')return loadReview();
 const data=await api('people',{limit:String(Math.min(peopleShown,500)),include_hidden:String(peopleHidden)});
 peopleItems=data.items;
 for(const id of [...peopleSelected])if(!peopleItems.some(p=>p.id===id))peopleSelected.delete(id);
 $('#people-grid').innerHTML=peopleItems.length?peopleItems.map(personCard).join(''):`<div class="empty"><p class="eyebrow">NO PEOPLE YET</p><h2>${s.faces?'No groups of three or more faces yet':'Find the faces in your library first'}</h2><p>Faces are found in the stored previews and grouped on this Mac. Nothing is uploaded.</p></div>`;
 $('#people-more').classList.toggle('hidden',data.total<=peopleItems.length||peopleShown>=500);
 showMergeButton();
 $$('.person-card').forEach(card=>{
  const id=Number(card.dataset.personId);const p=peopleItems.find(x=>x.id===id);
  const name=card.querySelector('.person-name');
  name.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();name.blur()}else if(e.key==='Escape'){name.value=p.name;name.blur()}};
  name.onblur=safe(async()=>{const v=name.value.trim();if(v!==p.name){await api('person_update',{id,name:v},true);p.name=v;toast(v?`Named ${v}`:'Name removed')}});
  card.querySelector('.card-check').onchange=e=>{e.target.checked?peopleSelected.add(id):peopleSelected.delete(id);card.classList.toggle('selected',e.target.checked);showMergeButton()};
 });
 $$('#people-grid [data-person-open]').forEach(b=>b.onclick=safe(()=>openPerson(Number(b.dataset.personOpen))));
 $$('[data-person-media]').forEach(b=>b.onclick=safe(()=>showPersonMedia(peopleItems.find(x=>x.id===Number(b.dataset.personMedia)))));
 $$('[data-person-hide]').forEach(b=>b.onclick=safe(async()=>{const p=peopleItems.find(x=>x.id===Number(b.dataset.personHide));await api('person_update',{id:p.id,hidden:!p.hidden},true);toast(p.hidden?'Shown again':'Hidden from People');await loadPeople()}));
}
async function showPersonMedia(p){resetLibraryFilters();personFilter={id:p.id,name:personName(p)};navigate('library');offset=0;await search()}

// ---- unsorted faces (no person yet)
function faceTile(f,selectedSet,extra=''){
 return `<figure class="face-tile ${selectedSet.has(f.id)?'selected':''}" data-face="${f.id}">
  <input class="card-check" type="checkbox" aria-label="Select face ${f.id}" ${selectedSet.has(f.id)?'checked':''}>
  <button class="person-face" type="button" data-face-toggle="${f.id}" title="${esc(f.filename)}">${faceImg(f.crop,f.filename)}</button>
  <figcaption>${esc(date(f.captured))}${f.confirmed?' · ✓':''} <button class="text-button" type="button" data-open-asset="${esc(f.asset_id)}">Item</button></figcaption>${extra}</figure>`;
}
function wireFaceTiles(root,selectedSet,changed){
 $$(`${root} .face-tile`).forEach(tile=>{
  const id=Number(tile.dataset.face),box=tile.querySelector('.card-check');
  const set=on=>{on?selectedSet.add(id):selectedSet.delete(id);box.checked=on;tile.classList.toggle('selected',on);changed()};
  box.onchange=()=>set(box.checked);
  tile.querySelector('[data-face-toggle]').onclick=()=>set(!selectedSet.has(id));
 });
}
function unsortedChanged(){
 const n=unsortedSelected.size;
 $('#unsorted-selected').textContent=n?`${fmt(n)} selected`:'';
 $('#unsorted-link').disabled=!n;
 $('#unsorted-all').textContent=unsortedItems.length&&unsortedItems.every(f=>unsortedSelected.has(f.id))?'Clear selection':'Select all shown';
}
async function loadUnsorted(more=false){
 const data=await api('faces_unsorted',{limit:String(FACE_PAGE),offset:String(more?unsortedItems.length:0)});
 unsortedItems=more?unsortedItems.concat(data.items):data.items;unsortedTotal=data.total;
 if(!more)unsortedSelected=new Set([...unsortedSelected].filter(id=>unsortedItems.some(f=>f.id===id)));
 $('#unsorted-count').textContent=unsortedTotal?`· ${fmt(unsortedTotal)}`:'';
 $('#unsorted-grid').innerHTML=unsortedItems.length?unsortedItems.map(f=>faceTile(f,unsortedSelected)).join(''):'<div class="empty"><p class="eyebrow">ALL SORTED</p><h2>Every clear face belongs to someone</h2></div>';
 $('#unsorted-more').classList.toggle('hidden',unsortedItems.length>=unsortedTotal);
 wireFaceTiles('#unsorted-grid',unsortedSelected,unsortedChanged);
 unsortedChanged();
}
function setPeopleView(view){
 peopleView=view;
 $$('[data-people-view]').forEach(b=>{const on=b.dataset.peopleView===view;b.classList.toggle('selected',on);b.setAttribute('aria-selected',String(on))});
 ['people','unsorted','review'].forEach(v=>$('#people-view-'+v).classList.toggle('hidden',view!==v));
}

// ---- the sorter's suggestions and changes
const WHO={auto:'Sorter',agent:'Agent',google:'Google Photos'};
function reviewRow(e){
 const pct=e.likeness==null?'':`${Math.round(e.likeness*100)}%`;
 const pname=e.person_name||(e.person_id?`Unnamed person #${e.person_id}`:'');
 const oname=e.other_name||(e.other_id?`Unnamed person #${e.other_id}`:'');
 const face=`<span class="picker-face">${faceImg(e.face_crop,'')}</span>`,pc=`<span class="picker-face">${faceImg(e.person_cover,'')}</span>`,oc=`<span class="picker-face">${faceImg(e.other_cover,'')}</span>`;
 const what={merge:[`${oc}<span class="arrow">→</span>${pc}`,`${esc(oname)} and ${esc(pname)} look like one person`+(e.detail.name?` (${esc(e.detail.name)} in ${esc(WHO[e.actor]||e.actor)})`:'')],
  link:[`${face}<span class="arrow">→</span>${pc}`,`This face looks like ${esc(pname)}`],
  move:[`${face}<span class="arrow">→</span>${pc}`,`This face of ${esc(oname)} looks more like ${esc(pname)}`],
  unlink:[`${face}<span class="arrow">✕</span>${pc}`,`This face looks unlike ${esc(pname)}`],
  name:[e.other_id?`${pc}<span class="arrow">or</span>${oc}`:pc,`${e.other_id?`Is “${esc(e.detail.name||'')}” ${esc(pname)} or ${esc(oname)}?`:`Call ${esc(pname)} “${esc(e.detail.name||'')}”`}`+(e.detail.share!=null?` · ${e.other_id?'the first is ':''}in ${Math.round(e.detail.share*100)}% of ${fmt(e.detail.of)} of their ${esc(WHO[e.actor]||e.actor)} items`:'')+(e.detail.why&&e.detail.why!=='proposed'?` · ${esc(e.detail.why)}`:'')]}[e.action]||['',''];
 const two=e.action==='name'&&e.other_id;
 const short=(n,id)=>n?esc(n.split(' ')[0]):`#${id}`;
 const buttons=e.status==='proposed'?(two?`<button class="button small primary" data-decide="apply" data-log="${e.id}" type="button">${short(e.person_name,e.person_id)}</button><button class="button small primary" data-decide="apply_other" data-log="${e.id}" type="button">${short(e.other_name,e.other_id)}</button>`
   :`<button class="button small primary" data-decide="apply" data-log="${e.id}" type="button">${e.action==='merge'?'Merge':e.action==='name'?'Use name':e.action==='unlink'?'Unlink':'Link'}</button>`)+`<button class="button small" data-decide="dismiss" data-log="${e.id}" type="button">${two?'Neither':'No'}</button>`
  :e.status==='applied'?`<button class="button small" data-decide="undo" data-log="${e.id}" type="button">Undo</button>`:'';
 const look=(e.person_id?`<button class="text-button" data-person-open="${e.person_id}" type="button">Open</button>`:'')+(two?`<button class="text-button" data-person-open="${e.other_id}" type="button">Open #${e.other_id}</button>`:'');
 return `<div class="review-row" data-log-row="${e.id}"><div class="review-faces">${what[0]}</div><div class="review-text"><strong>${what[1]}</strong><small>${pct&&e.action!=='name'?pct+' alike · ':''}${esc(WHO[e.actor]||e.actor)}${e.filename?' · '+esc(e.filename):''}</small></div><div class="face-actions">${look}${buttons}</div></div>`;
}
async function loadReview(more=false){
 const data=await api('people_review',{status:$('#review-status').value,limit:'50',offset:String(more?reviewItems.length:0)});
 reviewItems=more?reviewItems.concat(data.items):data.items;reviewTotal=data.total;
 const waiting=(data.counts||{}).proposed||0;$('#review-count').textContent=waiting?`· ${fmt(waiting)}`:'';
 $('#review-list').innerHTML=reviewItems.length?reviewItems.map(reviewRow).join(''):`<div class="empty"><p class="eyebrow">NOTHING HERE</p><h2>${$('#review-status').value==='proposed'?'No suggestions waiting':'Nothing yet'}</h2><p>Press Find suggestions to look for people split in two and faces that belong to someone.</p></div>`;
 $('#review-more').classList.toggle('hidden',reviewItems.length>=reviewTotal);
}
async function decideReview(id,decision){
 await api('people_review_decide',{id,decision},true);
 toast({apply:'Done',apply_other:'Done',dismiss:'Turned down; it will not be suggested again',undo:'Undone'}[decision]);
 reviewItems=reviewItems.filter(e=>e.id!==id);reviewTotal-=1;
 const row=$(`[data-log-row="${id}"]`);if(row)row.remove();
 if(!reviewItems.length)await loadReview();
}

// ---- one person: their faces, corrections and merging
function personToolbar(p){
 const n=personSelected.size,all=personFaces.length&&personFaces.every(f=>personSelected.has(f.id));
 return `<label class="related-search"><input type="checkbox" id="person-select-all" ${all?'checked':''}> Select all shown</label><span class="muted">${n?fmt(n)+' selected':'Select faces to move or unlink them'}</span>
  <button class="button small" id="person-move" type="button" ${n?'':'disabled'}>Link to another person…</button><button class="button small danger" id="person-unlink" type="button" ${n?'':'disabled'}>Unlink from ${p.name?esc(p.name):'this person'}</button>`;
}
function renderPersonFaces(p){
 $('#person-faces').innerHTML=personFaces.map(f=>faceTile(f,personSelected,`<button class="text-button danger" type="button" data-face-remove="${f.id}">Not ${esc(p.name||'them')}</button>`)).join('');
 $('#person-more').classList.toggle('hidden',personFaces.length>=p.face_count);
 wireFaceTiles('#person-faces',personSelected,()=>renderPersonToolbar(p));
 renderPersonToolbar(p);
}
function renderPersonToolbar(p){
 $('#person-toolbar').innerHTML=personToolbar(p);
 $('#person-select-all').onchange=e=>{personFaces.forEach(f=>e.target.checked?personSelected.add(f.id):personSelected.delete(f.id));renderPersonFaces(p)};
 $('#person-move').onclick=safe(async()=>{
  const ids=[...personSelected];
  const choice=await pickPerson({title:`Who are these ${plural(ids.length,'face')}?`,faces:personFaces.filter(f=>personSelected.has(f.id)),exclude:[p.id]});
  if(!choice)return;
  await linkFaces(ids,choice);await afterFacesLeft(p,ids);
 });
 $('#person-unlink').onclick=safe(async()=>{
  const ids=[...personSelected];
  await api('faces_unlink',{faces:ids},true);toast(`${plural(ids.length,'face')} unlinked from ${personName(p)}`);await afterFacesLeft(p,ids);
 });
}
async function afterFacesLeft(p,ids){
 const gone=new Set(ids);
 personFaces=personFaces.filter(f=>!gone.has(f.id));ids.forEach(id=>personSelected.delete(id));
 p.face_count=Math.max(0,p.face_count-ids.length);
 renderPersonFaces(p);
 $('#person-counts').textContent=`${plural(p.face_count,'face')}`;
 if(page==='people')await loadPeople();
}
async function openPerson(id){
 const p=await api('person',{id:String(id),limit:String(FACE_PAGE)});personOpen=p;personFaces=p.faces;personSelected=new Set();
 const d=$('#person-dialog');
 $('#person-content').innerHTML=`<div class="person-head"><div class="person-face big">${faceImg(p.faces[0]&&p.faces[0].crop,personName(p))}</div><div><p class="eyebrow">PERSON ${p.id}</p><input class="person-name big" id="person-dialog-name" value="${esc(p.name)}" placeholder="Who is this?" maxlength="120" aria-label="Name"><p class="muted">${plural(p.asset_count,'item')} · <span id="person-counts">${plural(p.face_count,'face')}</span>. Faces you unlink or move never come back to this person.</p><div class="detail-actions"><button class="button primary" id="person-dialog-media">Show their media</button><button class="button" id="person-dialog-merge">Merge with…</button><button class="button" id="person-dialog-hide">${p.hidden?'Show in People':'Hide (a passer-by)'}</button></div></div></div>
  <div class="face-actions person-toolbar" id="person-toolbar"></div>
  <div class="face-grid" id="person-faces"></div><button class="button hidden idea-more" id="person-more" type="button">Show more faces</button>`;
 renderPersonFaces(p);
 const nameInput=$('#person-dialog-name');
 nameInput.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();nameInput.blur()}};
 nameInput.onblur=safe(async()=>{const v=nameInput.value.trim();if(v!==p.name){await api('person_update',{id:p.id,name:v},true);p.name=v;toast(v?`Named ${v}`:'Name removed');renderPersonFaces(p);if(page==='people')await loadPeople()}});
 $('#person-dialog-media').onclick=safe(async()=>{d.close();await showPersonMedia(p)});
 $('#person-dialog-hide').onclick=safe(async()=>{await api('person_update',{id:p.id,hidden:!p.hidden},true);d.close();toast(p.hidden?'Shown again':'Hidden from People');if(page==='people')await loadPeople()});
 $('#person-dialog-merge').onclick=safe(async()=>{
  const choice=await pickPerson({title:p.name?`Who else is ${p.name}?`:'Who else is this?',eyebrow:'MERGE PEOPLE',faces:personFaces.slice(0,8),exclude:[p.id],allowNew:false});
  if(!choice)return;
  const into=p.name&&!choice.name?p.id:choice.person,other=into===p.id?choice.person:p.id;
  const kept=into===p.id?personLabel(p):personLabel({id:choice.person,name:choice.name});
  if(!confirm(`Merge ${personLabel(p)} and ${personLabel({id:choice.person,name:choice.name})} into one person (${kept})? Every face of both is kept.`))return;
  const r=await api('person_merge',{into,other},true);
  toast(`Merged: ${plural(r.moved_faces,'face')} moved to ${kept}`);
  if(page==='people')await loadPeople();
  await openPerson(into);
 });
 $('#person-more').onclick=safe(async()=>{
  const more=await api('person',{id:String(p.id),limit:String(FACE_PAGE),offset:String(personFaces.length)});
  personFaces=personFaces.concat(more.faces);renderPersonFaces(p);
 });
 if(!d.open)d.showModal();
}
async function handlePersonDialogClick(e){
 const remove=e.target.closest('[data-face-remove]');
 if(remove){const id=Number(remove.dataset.faceRemove);await api('faces_unlink',{faces:[id]},true);toast(`Face unlinked from ${personName(personOpen)}`);await afterFacesLeft(personOpen,[id]);return}
 const open=e.target.closest('[data-open-asset]');
 if(open){$('#person-dialog').close();await openDetail(open.dataset.openAsset)}
}
// a face in an item's details that belongs to no one yet
async function linkDetailFace(faceId){
 const f=(currentAsset.people||[]).find(x=>x.face_id===faceId);
 const choice=await pickPerson({title:'Who is this?',faces:[{id:faceId,crop:f&&f.crop}]});
 if(!choice)return;
 await linkFaces([faceId],choice);
 await openDetail(currentAsset.id);
}
function setupPeople(){
 $('#people-scan').onclick=safe(async()=>{const r=await api('people_scan',{},true);toast(r.started?'Finding faces on this Mac…':'A scan is already running');await loadPeople(false)});
 $('#people-regroup').onclick=safe(async()=>{const r=await api('people_regroup',{},true);toast(r.started?'Regrouping faces…':'A scan is already running');await loadPeople(false)});
 $('#people-more').onclick=safe(async()=>{peopleShown+=60;await loadPeople()});
 $('#people-show-hidden').onchange=safe(async e=>{peopleHidden=e.target.checked;await loadPeople()});
 $$('[data-people-view]').forEach(b=>b.onclick=safe(async()=>{setPeopleView(b.dataset.peopleView);await loadPeople()}));
 $('#people-merge').onclick=safe(async()=>{
  const chosen=peopleItems.filter(p=>peopleSelected.has(p.id));if(chosen.length<2)return;
  const into=chosen.find(p=>p.name)||chosen[0];
  if(!confirm(`Merge ${chosen.length} people into ${personLabel(into)}?`))return;
  for(const p of chosen)if(p.id!==into.id)await api('person_merge',{into:into.id,other:p.id},true);
  peopleSelected.clear();toast('Merged');await loadPeople()});
 $('#unsorted-more').onclick=safe(()=>loadUnsorted(true));
 $('#review-more').onclick=safe(()=>loadReview(true));
 $('#review-status').onchange=safe(()=>loadReview());
 $('#review-find').onclick=safe(async()=>{const r=await api('people_autosort',{apply:false},true);toast(r.started?'Looking for suggestions…':'A scan is already running');await loadPeople(false)});
 $('#review-sort').onclick=safe(async()=>{if(!confirm('Merge the people who are clearly one person and link the faces that clearly belong to someone? Everything else waits here for you, and every change can be undone.'))return;const r=await api('people_autosort',{apply:true},true);toast(r.started?'Sorting faces…':'A scan is already running');await loadPeople(false)});
 $('#review-list').addEventListener('click',safe(async e=>{
  const b=e.target.closest('[data-decide]');if(b){b.disabled=true;await decideReview(Number(b.dataset.log),b.dataset.decide);return}
  const o=e.target.closest('[data-person-open]');if(o)await openPerson(Number(o.dataset.personOpen));
 }));
 $('#unsorted-all').onclick=()=>{const all=unsortedItems.every(f=>unsortedSelected.has(f.id));unsortedItems.forEach(f=>all?unsortedSelected.delete(f.id):unsortedSelected.add(f.id));wireRefresh()};
 const wireRefresh=()=>{$$('#unsorted-grid .face-tile').forEach(t=>{const on=unsortedSelected.has(Number(t.dataset.face));t.classList.toggle('selected',on);t.querySelector('.card-check').checked=on});unsortedChanged()};
 $('#unsorted-link').onclick=safe(async()=>{
  const ids=[...unsortedSelected];
  const choice=await pickPerson({title:`Who are these ${plural(ids.length,'face')}?`,faces:unsortedItems.filter(f=>unsortedSelected.has(f.id))});
  if(!choice)return;
  await linkFaces(ids,choice);unsortedSelected.clear();await loadPeople();
 });
 $('#unsorted-grid').addEventListener('click',safe(async e=>{const open=e.target.closest('[data-open-asset]');if(open)await openDetail(open.dataset.openAsset)}));
 $('#person-close').onclick=()=>$('#person-dialog').close();
 $('#person-dialog').addEventListener('click',safe(handlePersonDialogClick));
 const picker=$('#person-picker');
 picker.addEventListener('close',()=>{if(pickerResolve)pickerDone(null)});
 picker.addEventListener('click',e=>{
  if(e.target===picker){const r=picker.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom){pickerDone(null);return}}
  const row=e.target.closest('[data-pick]');
  if(row){const p=pickerPeople.get(Number(row.dataset.pick));pickerDone({person:p.id,name:p.name})}
 });
 $('#picker-close').onclick=()=>pickerDone(null);
 $('#picker-search').oninput=()=>{clearTimeout(pickerTimer);pickerTimer=setTimeout(()=>pickerSearch().catch(e=>toast(e.message,true)),200)};
 $('#picker-new').onclick=()=>pickerDone({new_person:true,name:$('#picker-new-name').value.trim()});
 $('#picker-new-name').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('#picker-new').click()}};
 $('#people-forget').onclick=safe(async()=>{
  if(!confirm('Delete every face, person, name and correction? Your media and its analysis stay. This cannot be undone.'))return;
  const r=await api('people_forget',{confirm:true},true);peopleSelected.clear();unsortedSelected.clear();toast(`Deleted ${fmt(r.forgotten_faces)} faces`);await loadPeople()});
}
