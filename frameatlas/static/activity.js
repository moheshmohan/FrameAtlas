'use strict';
// Activity: what connected AI agents did, the calls waiting for the owner's OK, and what agents may do.
let activityReads=false, activityOffset=0;
const ACTIVITY_PAGE=50;
const POLICY_LABELS={allow:'Allow',confirm:'Ask in the chat first',owner:'Ask me here first',deny:'Never'};
const TIER_LABELS={write:'Changes',destructive:'Deletes and merges',outside:'Google, Insta360 and downloads'};
const TIER_NOTES={write:'Adding and editing ideas, naming people, linking faces, corrections, imports.',
  destructive:'Deleting ideas, merging people or items, forgetting all faces or places, closing a Google selection.',
  outside:'Queuing analysis that may download, editing downloads, Google Photos selections, Insta360 transfers.'};
const when=t=>{const d=new Date(t*1000);return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})+' '+d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})};
const clientName=c=>c==='mcp'?'Agent':c.startsWith('mcp:')?c.slice(4):c==='owner'?'You':c;

function setupActivity(){
  $('#refresh-health').onclick=safe(loadHealth);
  $('#activity-refresh').onclick=safe(loadActivity);
  $('#activity-reads').onchange=safe(async e=>{activityReads=e.target.checked;activityOffset=0;await loadActivity()});
  $('#activity-prev').onclick=safe(async()=>{activityOffset=Math.max(0,activityOffset-ACTIVITY_PAGE);await loadActivity()});
  $('#activity-next').onclick=safe(async()=>{activityOffset+=ACTIVITY_PAGE;await loadActivity()});
}

async function loadActivity(){
  const r=await api('activity',{include_reads:activityReads,limit:ACTIVITY_PAGE,offset:activityOffset});
  renderPending(r.pending);renderPermissions(r.permissions);renderLog(r);
  $('#nav-activity').textContent=r.pending.length||'';
}

function renderPending(items){
  $('#activity-pending-panel').classList.toggle('hidden',!items.length);
  $('#activity-pending').innerHTML=items.map(p=>`<article class="activity-ask"><div><p class="activity-what">${esc(p.summary)}</p><p class="fine">${esc(clientName(p.client))} · ${esc(TIER_LABELS[p.tier]||p.tier)} · asked ${esc(when(p.created))} · expires ${esc(when(p.expires))}${p.policy==='confirm'?' · the agent can also go ahead once you say yes in the chat':''}</p></div><div class="activity-buttons"><button class="button primary small" data-confirm-token="${esc(p.token)}" data-decision="approved">Approve</button><button class="button small" data-confirm-token="${esc(p.token)}" data-decision="declined">Decline</button></div></article>`).join('');
  $$('[data-confirm-token]').forEach(b=>b.onclick=safe(async()=>{await api('confirmation_decide',{token:b.dataset.confirmToken,decision:b.dataset.decision},true);toast(b.dataset.decision==='approved'?'Approved. The agent can now go ahead.':'Declined. The agent was told not to.');await loadActivity()}));
}

function renderPermissions(perms){
  $('#activity-permissions').innerHTML=Object.keys(TIER_LABELS).map(t=>`<div class="activity-perm"><label for="perm-${t}">${esc(TIER_LABELS[t])}</label><select id="perm-${t}" data-tier="${t}">${Object.entries(POLICY_LABELS).map(([v,l])=>`<option value="${v}"${perms[t]===v?' selected':''}>${esc(l)}</option>`).join('')}</select><p class="fine">${esc(TIER_NOTES[t])}</p></div>`).join('');
  $$('#activity-permissions select').forEach(s=>s.onchange=safe(async()=>{await api('permissions_set',{permissions:{[s.dataset.tier]:s.value}},true);toast('Saved: '+TIER_LABELS[s.dataset.tier]+' · '+POLICY_LABELS[s.value]);await loadActivity()}));
}

function renderLog(r){
  $('#activity-count').textContent=r.total?`${fmt(r.offset+1)}–${fmt(r.offset+r.items.length)} of ${fmt(r.total)} ${activityReads?'calls':'changes'}`:'';
  $('#activity-prev').disabled=r.offset===0;$('#activity-next').disabled=r.offset+r.items.length>=r.total;
  $('#activity-log').innerHTML=r.items.length?`<div class="activity-table"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Kind</th><th>Result</th><th></th></tr></thead><tbody>${r.items.map(i=>`<tr><td class="nowrap">${esc(when(i.at))}</td><td>${esc(clientName(i.client))}</td><td><code>${esc(i.tool)}</code><details><summary>Arguments</summary><pre>${esc(i.args)}</pre></details></td><td><span class="tier tier-${esc(i.tier)}">${esc(i.tier)}</span></td><td>${i.ok?'<span class="status ready">done</span>':`<span class="status failed">${esc(i.code||'failed')}</span>`} ${i.task?`<span class="fine">task #${esc(i.task)}</span>`:''}<div class="fine">${esc(i.summary)}</div></td><td>${i.undone?'<span class="fine">undone</span>':i.can_undo?`<button class="button small" data-undo="${i.id}">Undo</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h2>No agent activity yet</h2><p>When an AI agent connected to FrameAtlas changes something, it is listed here and can often be undone.</p></div>';
  $$('[data-undo]').forEach(b=>b.onclick=safe(async()=>{const u=await api('activity_undo',{id:Number(b.dataset.undo)},true);toast('Undone: '+u.tool);await loadActivity()}));
}

// What needs attention (Sources & storage): the same report agents read with catalog_health.
async function loadHealth(){
  const r=await api('health');
  $('#health-list').innerHTML=r.items.length?`<ul class="health-list">${r.items.map(i=>`<li class="health-item ${esc(i.level)}"><span class="status ${i.level==='action'?'failed':'queued'}">${i.level==='action'?'act':'note'}</span><div><strong>${esc(i.title)}</strong><p class="fine">${esc(i.detail)}</p>${healthLink(i)}</div></li>`).join('')}</ul>`:'<p class="muted">Nothing needs attention.</p>';
  $$('[data-health-go]').forEach(b=>b.onclick=safe(async()=>{const go=b.dataset.healthGo;if(go==='never'){resetLibraryFilters();$('#status').value='metadata_only';provider=b.dataset.provider||'';$$('[data-source]').forEach(x=>x.classList.toggle('selected',x.dataset.source===provider));navigate('library');offset=0;await search()}else navigate(go)}));
}
function healthLink(i){
  const go={failed_jobs:'queue',queue_paused:'queue',agent_waiting:'activity',people_review:'people',faces_unsorted:'people',not_analysed:'never'}[i.kind];
  return go?`<button class="text-button" data-health-go="${go}"${i.next&&i.next.provider?` data-provider="${esc(i.next.provider)}"`:''}>Open →</button>`:'';
}
