'use strict';
// The shared screen with an AI agent: links such as #item/<id>@12.5, #person/3, #place/4,
// #idea/7 or #library?query=beach&since=2024-05-25 open what they name; an agent's ui_show and
// ui_select arrive by polling; and the page reports what it shows so the agent can read it (ui_state).
let agentLast=-1, agentReported='', agentPolling=false;

async function routeHash(hash){
  const raw=String(hash||'').replace(/^#/,'');
  if(!raw)return;
  const [path,query='']=raw.split('?');
  const [view,arg='']=path.split('/');
  const params=new URLSearchParams(query);
  if(view!=='item'&&$('#detail').open)$('#detail').close();
  if(view==='item'&&arg){
    const [id,at]=decodeURIComponent(arg).split('@');
    const t=at!==undefined&&at!==''?Number(at):null;
    await openDetail(id,Number.isFinite(t)?{kind:'citation',label:'Shown by an agent',start:t,end:t}:undefined);
    return;
  }
  if(view==='person'&&/^\d+$/.test(arg)){navigate('people');await openPerson(Number(arg));return}
  if(view==='idea'&&/^\d+$/.test(arg)){navigate('ideapad');await showFreshIdeas([Number(arg)]);return}
  if(view==='place'&&/^[\d,]+$/.test(arg)){await showPlaceMedia(arg,params.get('name')||'');return}
  if(view==='library'&&query){
    resetLibraryFilters();
    $('#query').value=params.get('query')||'';
    if(params.get('kind'))$('#kind').value=params.get('kind');
    for(const key of ['since','until'])if(params.get(key)){$('#'+key).value=params.get(key);$('#date-row').classList.remove('hidden')}
    if(params.get('provider')){provider=params.get('provider');$$('[data-source]').forEach(b=>b.classList.toggle('selected',b.dataset.source===provider))}
    if(params.get('person'))personFilter={id:params.get('person'),name:'person #'+params.get('person')};
    if(params.get('place'))placeFilter={ids:params.get('place'),name:'the place'};
    navigate('library');offset=0;await search();
    return;
  }
  if(['library','people','ideapad','ideas','queue','sources','activity'].includes(view))navigate(view);
}

function agentSelect(event){
  if(event.mode==='replace'||event.mode==='clear')selected.clear();
  for(const [id,filename] of event.items||[])selected.set(id,filename);
  navigate('library');
  if(typeof renderCards==='function')renderCards();
}

async function agentPoll(){
  if(agentPolling)return;
  agentPolling=true;
  try{
    while(true){
      if(document.hidden){await new Promise(r=>setTimeout(r,3000));continue}
      let r;
      try{r=await api('ui_poll',{after:agentLast,wait:agentLast<0?0:20},true)}
      catch(e){await new Promise(r=>setTimeout(r,5000));continue}
      agentLast=r.last;
      for(const event of r.events){
        try{
          if(event.message)toast(event.message);
          if(event.kind==='show'){if(location.hash==='#'+event.hash)await routeHash(event.hash);else location.hash=event.hash}
          if(event.kind==='select')agentSelect(event);
        }catch(e){toast(e.message,true)}
      }
    }
  }finally{agentPolling=false}
}

function pageState(){
  const detail=$('#detail').open&&currentAsset?{id:currentAsset.id,filename:currentAsset.filename}:null;
  const {limit,offset:pageOffset,...criteria}=filters();
  const shown=Object.fromEntries(Object.entries(criteria).filter(([,v])=>v!==''&&v!==false));
  return {page,hash:location.hash,library:{filters:shown,offset:pageOffset,total,
    results:results.slice(0,24).map(a=>({id:a.id,filename:a.filename}))},
    selected:[...selected].slice(0,200).map(([id,filename])=>({id,filename})),selected_total:selected.size,
    open_item:detail,open_person:typeof personOpen!=='undefined'&&personOpen?{id:personOpen.id??personOpen,name:personOpen.name||''}:null};
}

async function agentReport(){
  if(document.hidden)return;
  const state=pageState(),text=JSON.stringify(state);
  if(text===agentReported)return;
  try{await api('ui_report',{state},true);agentReported=text}catch(e){}
}

function setupAgentLink(){
  $('#search-meaning').onclick=safe(searchMeaning);
  window.addEventListener('hashchange',()=>routeHash(location.hash).catch(e=>toast(e.message,true)));
  setInterval(agentReport,2000);
  agentPoll();
}

// Search by meaning: previews that look like the words (SigLIP 2 vectors, semantic.py), with the Library's filters.
async function searchMeaning(){
  const {limit,offset:_o,query,filename,speech_only,scene_only,...rest}=filters();
  if(!query.trim()){toast('Type what you are looking for first',true);return}
  const criteria=Object.fromEntries(Object.entries(rest).filter(([,v])=>v!==''&&v!==false&&v!=null));
  const button=$('#search-meaning');button.disabled=true;button.textContent='Looking at the previews…';$('#media-grid').classList.add('loading');
  try{
    const r=await api('search_meaning',{query,limit:48,...criteria},true);
    results=r.items;total=results.length;relatedResults=true;renderCards();
    $('#result-count').textContent=fmt(total)+' items';$('#results-title').textContent='Looks like “'+query+'”';
    $('#prev').disabled=true;$('#next').disabled=true;$('#page-info').textContent=`Best ${fmt(total)} of ${fmt(r.searched||0)} previews`;
  }finally{button.disabled=false;button.textContent='Search by meaning';$('#media-grid').classList.remove('loading')}
}
