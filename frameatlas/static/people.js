'use strict';
// People: faces found and grouped on this Mac; name them, merge, hide, correct, and find their media.
let peopleItems=[], peopleShown=60, peopleHidden=false, peopleSelected=new Set(), peoplePoll=null, personOpen=null;
const personName=p=>p&&p.name?p.name:'Unnamed person';
const faceImg=(crop,alt)=>crop?`<img loading="lazy" src="/face/${esc(crop)}" alt="${esc(alt)}">`:'<span class="media-symbol">☺</span>';
function years(a,b){const x=(a||'').slice(0,4),y=(b||'').slice(0,4);return x&&y?(x===y?x:`${x}–${y}`):''}
function peopleLine(list){
 if(!list||!list.length)return'';
 const seen=new Map();list.forEach(f=>{const k=f.person_id||('f'+f.face_id);if(!seen.has(k))seen.set(k,f)});
 return `<div class="people-line">${[...seen.values()].filter(f=>!f.hidden).map(f=>`<button class="person-chip" ${f.person_id?`data-person-open="${f.person_id}"`:'disabled'} title="${esc(f.name||'Not grouped yet')}">${f.crop?`<img src="/face/${esc(f.crop)}" alt="">`:''}<span>${esc(f.person_id?(f.name||'Unnamed'):'Face')}</span></button>`).join('')}</div>`;
}
async function loadPeopleStatus(){
 const s=await api('people_status');const st=s.scan||{};
 const pct=s.previews?Math.round(s.scanned/s.previews*100):0;
 $('#people-status').innerHTML=`<div><span>PREVIEWS SCANNED</span><strong>${fmt(s.scanned)}</strong><small>of ${fmt(s.previews)} (${pct}%)</small></div><div><span>FACES</span><strong>${fmt(s.faces)}</strong><small>${fmt(s.grouped)} grouped into people</small></div><div><span>PEOPLE</span><strong>${fmt(s.people)}</strong><small>${fmt(s.named)} named</small></div><div><span>SCAN</span><strong>${s.running?(st.state==='grouping'?'Grouping…':'Scanning…'):st.state==='failed'?'Stopped':st.state==='done'?'Done':'—'}</strong><small>${s.running&&st.rate?`${st.rate} previews/s`:st.error?esc(st.error):''}</small></div>`;
 $('#people-scan').disabled=s.running;$('#people-regroup').disabled=s.running||!s.faces;
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
  <p class="fine">${fmt(p.assets)} item${p.assets===1?'':'s'} · ${fmt(p.faces)} face${p.faces===1?'':'s'}${years(p.first_seen,p.last_seen)?' · '+years(p.first_seen,p.last_seen):''}</p>
  <div class="person-actions"><button class="text-button" data-person-media="${p.id}">Media →</button><button class="text-button" data-person-hide="${p.id}">${p.hidden?'Show':'Hide'}</button></div>
 </article>`;
}
async function loadPeople(withList=true){
 const s=await loadPeopleStatus();
 if(!withList&&!s.running)withList=true;
 if(!withList)return;
 const data=await api('people',{limit:String(Math.min(peopleShown,500)),include_hidden:String(peopleHidden)});
 peopleItems=data.items;
 $('#people-grid').innerHTML=peopleItems.length?peopleItems.map(personCard).join(''):`<div class="empty"><p class="eyebrow">NO PEOPLE YET</p><h2>${s.faces?'No groups of three or more faces yet':'Find the faces in your library first'}</h2><p>Faces are found in the stored previews and grouped on this Mac. Nothing is uploaded.</p></div>`;
 $('#people-more').classList.toggle('hidden',data.total<=peopleItems.length||peopleShown>=500);
 $('#people-merge').classList.toggle('hidden',peopleSelected.size<2);
 $('#people-merge').textContent=`Merge ${peopleSelected.size} people`;
 $$('.person-card').forEach(card=>{
  const id=Number(card.dataset.personId);const p=peopleItems.find(x=>x.id===id);
  const name=card.querySelector('.person-name');
  name.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();name.blur()}else if(e.key==='Escape'){name.value=p.name;name.blur()}};
  name.onblur=safe(async()=>{const v=name.value.trim();if(v!==p.name){await api('person_update',{id,name:v},true);p.name=v;toast(v?`Named ${v}`:'Name removed')}});
  card.querySelector('.card-check').onchange=e=>{e.target.checked?peopleSelected.add(id):peopleSelected.delete(id);card.classList.toggle('selected',e.target.checked);$('#people-merge').classList.toggle('hidden',peopleSelected.size<2);$('#people-merge').textContent=`Merge ${peopleSelected.size} people`};
 });
 $$('[data-person-open]').forEach(b=>b.onclick=safe(()=>openPerson(Number(b.dataset.personOpen))));
 $$('[data-person-media]').forEach(b=>b.onclick=safe(()=>showPersonMedia(peopleItems.find(x=>x.id===Number(b.dataset.personMedia)))));
 $$('[data-person-hide]').forEach(b=>b.onclick=safe(async()=>{const p=peopleItems.find(x=>x.id===Number(b.dataset.personHide));await api('person_update',{id:p.id,hidden:!p.hidden},true);toast(p.hidden?'Shown again':'Hidden from People');await loadPeople()}));
}
async function showPersonMedia(p){resetLibraryFilters();personFilter={id:p.id,name:personName(p)};navigate('library');offset=0;await search()}
async function openPerson(id){
 const p=await api('person',{id:String(id),limit:'120'});personOpen=p;
 const d=$('#person-dialog');
 $('#person-content').innerHTML=`<div class="person-head"><div class="person-face big">${faceImg(p.faces[0]&&p.faces[0].crop,personName(p))}</div><div><p class="eyebrow">PERSON ${p.id}</p><input class="person-name big" id="person-dialog-name" value="${esc(p.name)}" placeholder="Who is this?" maxlength="120"><p class="muted">${fmt(p.asset_count)} items · ${fmt(p.face_count)} faces. Remove faces that are someone else; they will not come back to this person.</p><div class="detail-actions"><button class="button primary" id="person-dialog-media">Show their media</button><button class="button" id="person-dialog-hide">${p.hidden?'Show in People':'Hide (a passer-by)'}</button></div></div></div>
  <div class="person-faces">${p.faces.map(f=>`<figure class="person-face-tile" data-face="${f.id}"><button class="person-face" data-open-asset="${esc(f.asset_id)}" title="${esc(f.filename)}">${faceImg(f.crop,f.filename)}</button><figcaption>${esc(date(f.captured))}${f.confirmed?' · ✓':''}</figcaption><button class="text-button danger" data-face-remove="${f.id}">Not ${esc(p.name||'them')}</button></figure>`).join('')}</div>`;
 const nameInput=$('#person-dialog-name');
 nameInput.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();nameInput.blur()}};
 nameInput.onblur=safe(async()=>{const v=nameInput.value.trim();if(v!==p.name){await api('person_update',{id:p.id,name:v},true);p.name=v;toast(v?`Named ${v}`:'Name removed');if(page==='people')await loadPeople()}});
 $('#person-dialog-media').onclick=safe(async()=>{d.close();await showPersonMedia(p)});
 $('#person-dialog-hide').onclick=safe(async()=>{await api('person_update',{id:p.id,hidden:!p.hidden},true);d.close();toast(p.hidden?'Shown again':'Hidden from People');if(page==='people')await loadPeople()});
 $$('#person-content [data-face-remove]').forEach(b=>b.onclick=safe(async()=>{await api('face_assign',{face:Number(b.dataset.faceRemove)},true);b.closest('figure').remove();toast('Face removed from this person')}));
 $$('#person-content [data-open-asset]').forEach(b=>b.onclick=safe(async()=>{d.close();await openDetail(b.dataset.openAsset)}));
 if(!d.open)d.showModal();
}
function setupPeople(){
 $('#people-scan').onclick=safe(async()=>{const r=await api('people_scan',{},true);toast(r.started?'Finding faces on this Mac…':'A scan is already running');await loadPeople(false)});
 $('#people-regroup').onclick=safe(async()=>{const r=await api('people_regroup',{},true);toast(r.started?'Regrouping faces…':'A scan is already running');await loadPeople(false)});
 $('#people-more').onclick=safe(async()=>{peopleShown+=60;await loadPeople()});
 $('#people-show-hidden').onchange=safe(async e=>{peopleHidden=e.target.checked;await loadPeople()});
 $('#people-merge').onclick=safe(async()=>{
  const chosen=peopleItems.filter(p=>peopleSelected.has(p.id));if(chosen.length<2)return;
  const into=chosen.find(p=>p.name)||chosen[0];
  if(!confirm(`Merge ${chosen.length} people into “${personName(into)}”?`))return;
  for(const p of chosen)if(p.id!==into.id)await api('person_merge',{into:into.id,other:p.id},true);
  peopleSelected.clear();toast('Merged');await loadPeople()});
 $('#person-close').onclick=()=>$('#person-dialog').close();
 $('#people-forget').onclick=safe(async()=>{
  if(!confirm('Delete every face, person, name and correction? Your media and its analysis stay. This cannot be undone.'))return;
  const r=await api('people_forget',{confirm:true},true);peopleSelected.clear();toast(`Deleted ${fmt(r.forgotten_faces)} faces`);await loadPeople()});
}
