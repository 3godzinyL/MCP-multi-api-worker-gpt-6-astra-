import {t, locale, language, initI18n, translateDom, translateMessage, onLanguageChange} from './i18n.js';
import {registerPanelTools} from './agent-tools.js';
import {createControls} from './controls.js';
import {createHistory} from './history.js';
import {createWorkspaceMemory,activeStates,mergeState,mergeTask,matchesSelection} from './workspace.js';
const paths={activity:'M3 12h4l3-8 4 16 3-8h4',plus:'M12 5v14M5 12h14',folder:'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10H3z',file:'M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h6',code:'m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18',agents:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.9M16 3a4 4 0 0 1 0 8M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',tokens:'M12 3 3 8v8l9 5 9-5V8zM3 8l9 5 9-5M12 13v8M7 5.8l9 5',sparkles:'m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7zM20 2v4M18 4h4',refresh:'M20 7v5h-5M4 17v-5h5M6.5 6.5A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.5 5.5',shield:'M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6zM8 12l3 3 5-6',route:'M5 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4M19 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4M7 6h9a4 4 0 0 1 0 8H8a2 2 0 0 0 0 4h9',terminal:'m5 7 5 5-5 5M13 17h6',layers:'m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5',lock:'M5 10h14v11H5zM8 10V7a4 4 0 0 1 8 0v3',close:'m6 6 12 12M6 18 18 6','arrow-up':'M12 19V5m-6 6 6-6 6 6','arrow-right':'M5 12h14m-6-6 6 6-6 6',menu:'M3 6h18M3 12h18M3 18h18',stop:'M6 6h12v12H6z',check:'m5 12 4 4L19 6',clock:'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',error:'M12 8v5M12 17h.01M12 3 2 21h20z'};
const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.activity}"/></svg>`;
const $=selector=>document.querySelector(selector);
const e=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons=(root=document)=>root.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
icons();
initI18n();
const mobileLayout=matchMedia('(max-width:760px)');
function updateSidebar(){
  const sidebar=$('#sidebar'),button=$('#menu-button'),expanded=sidebar.classList.contains('open');
  sidebar.inert=mobileLayout.matches&&!expanded;
  button.setAttribute('aria-controls','sidebar');
  button.setAttribute('aria-expanded',String(mobileLayout.matches&&expanded));
  button.dataset.i18nAriaLabel=expanded?'Zamknij projekty':'Pokaż projekty';
  button.setAttribute('aria-label',t(button.dataset.i18nAriaLabel));
}
new MutationObserver(updateSidebar).observe($('#sidebar'),{attributes:true,attributeFilter:['class']});
mobileLayout.addEventListener('change',()=>{
  const moveFocus=mobileLayout.matches&&$('#sidebar').contains(document.activeElement);
  $('#sidebar').classList.remove('open');updateSidebar();
  if(moveFocus)$('#menu-button').focus({preventScroll:true});
});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&mobileLayout.matches&&$('#sidebar').classList.contains('open')){$('#sidebar').classList.remove('open');$('#menu-button').focus({preventScroll:true})}});
document.addEventListener('click',event=>{if(mobileLayout.matches&&!event.target.closest('#sidebar,#menu-button'))$('#sidebar').classList.remove('open')});
updateSidebar();
const compact=value=>value===null||value===undefined?'—':new Intl.NumberFormat(locale(),{notation:value>=10000?'compact':'standard',maximumFractionDigits:value>=10000?1:0}).format(value);
const full=value=>new Intl.NumberFormat(locale()).format(value||0);
const clock=value=>new Date((value||Date.now()/1000)*1000).toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'});
const statusLabel=value=>t(({stopping:'Zatrzymywanie',finalizing:'Zapisywanie wyników',loading:'Ładowanie',pendingInit:'Uruchamianie',shutdown:'Zakończony',notFound:'Niedostępny',running:'W trakcie',starting:'Uruchamianie',queued:'W kolejce',completed:'Ukończone',failed:'Błąd',interrupted:'Przerwane',cancelled:'Przerwane',awaiting_input:'Czeka na Ciebie',idle:'Gotowe',pending:'Oczekuje',closed:'Zakończony',errored:'Błąd'})[value]||value||'Gotowy');
const memory=createWorkspaceMemory();
let state=null,projectId=memory.selectedProject(),taskId=memory.selection(projectId).taskId,runId=memory.selection(projectId).runId,tab='conversation',folderParent='',sending=false,toastTimer;
let selectionVersion=0,requestNumber=0,acceptedRequest=0,connectionLost=false,history=null,creatingChat=false;
const requests=new Map(),runCache=new Map(),chatPages=new Map(),runRequests=new Map(),runPageCounts=new Map();
paths.settings='M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3M12 19v3M2 12h3M19 12h3m-5-7 2-2M5 19l2-2M5 5l2 2m10 10 2 2';
paths.search='M21 21l-6-6M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0';
icons();
const emptyConversation=$('#conversation').innerHTML;
let controls=null;
let csrf=$('meta[name="csrf-token"]').content, sessionRecovery=null;
async function renewSession(){if(!sessionRecovery)sessionRecovery=(async()=>{const response=await fetch('/ui/',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error(t('Nie można odnowić sesji panelu.'));const doc=new DOMParser().parseFromString(await response.text(),'text/html');const value=doc.querySelector('meta[name="csrf-token"]')?.content;if(!value)throw new Error(t('Nie można odnowić sesji panelu.'));csrf=value;$('meta[name="csrf-token"]').content=value})().finally(()=>{sessionRecovery=null});return sessionRecovery}
async function api(path,body,retry=true,method=null){
  const options={credentials:'same-origin',cache:'no-store',headers:{'accept-language':locale()}};
  if(body!==undefined){options.method='POST';options.headers={...options.headers,'content-type':'application/json','x-panel-csrf':csrf};options.body=JSON.stringify(body)}
  if(method)options.method=method;
  let response;
  try{response=await fetch('/ui/api'+path,options)}catch{throw new Error(body===undefined?t('Brak połączenia z panelem. Ponawiam połączenie…'):t('Połączenie przerwane. Ponów to samo polecenie, aby sprawdzić przyjęte uruchomienie.'))}
  let result;try{result=await response.json()}catch{throw new Error(t('Panel nie otrzymał poprawnej odpowiedzi.'))}
  if(retry&&(response.status===401||['gateway_session_expired','session_expired','csrf_expired'].includes(result.code))){await renewSession();return api(path,body,false,method)}
  if(!response.ok){const error=new Error(translateMessage(result.error)||t('Nie udało się wykonać operacji.'));error.code=result.code;throw error}
  return result;
}
function toast(text,error=false){clearTimeout(toastTimer);const el=$('#toast');el.textContent=translateMessage(text);el.classList.toggle('error',error);el.hidden=false;toastTimer=setTimeout(()=>el.hidden=true,5000)}
function text(selector,value){const el=$(selector);if(el)el.textContent=value}
function currentProject(){return state?.projects?.find(p=>p.id===projectId)}
function currentTask(){return state?.tasks?.find(task=>task.id===taskId&&task.project_id===projectId)}
function viewedTask(){const task=currentTask();return runId?{state:'loading',...runCache.get(taskId+':'+runId),id:taskId,run_id:runId,project_id:projectId,approvals:[],title:task?.title}:task}
function saveDraft(){memory.saveDraft(projectId,taskId,$('#prompt').value)}
function restoreDraft(){$('#prompt').value=memory.draft(projectId,taskId)}
function select(project,task='',run=''){
  saveDraft();projectId=project;taskId=task;runId=run;selectionVersion++;
  memory.select(projectId,taskId,runId);restoreDraft();history?.setSelectedRun(runId||null);
  $('#submit-error').hidden=true;$('#sidebar').classList.remove('open');render();
  refresh();loadChats();if(runId)loadRun();
}
function setProject(id){const saved=memory.selection(id);select(id,saved.taskId,saved.runId)}
function setTask(id){const task=state?.tasks?.find(task=>task.id===id);select(task?.project_id||projectId,id)}
function taskStatus(task){return task?.state||'idle'}
function statusMark(value,kind='task'){
  const symbol=value==='completed'?'check':['failed','errored'].includes(value)?'error':['interrupted','cancelled','stopped'].includes(value)?'stop':value==='awaiting_input'?'clock':activeStates.has(value)?'refresh':'clock';
  return `<span class="status-mark ${e(value)}" data-${kind}-status="${e(value)}" role="img" aria-label="${e(statusLabel(value))}" title="${e(statusLabel(value))}">${icon(symbol)}</span>`;
}
function projectStatus(project){
  const tasks=(state?.tasks||[]).filter(task=>task.project_id===project.id);
  for(const value of ['running','starting','pendingInit','queued','finalizing','stopping','awaiting_input'])if(tasks.some(task=>task.state===value))return value;
  const finished=tasks.filter(task=>['completed','failed','errored','interrupted','cancelled','stopped'].includes(task.state));
  finished.sort((a,b)=>(b.finished_at||b.updated||b.created||0)-(a.finished_at||a.updated||a.created||0));
  return finished[0]?.state||'idle';
}
async function loadChats(more=false){
  const project=projectId,version=selectionVersion;if(!project)return;
  const previous=chatPages.get(project),cursor=more?previous?.next_cursor:null;
  if(more&&!cursor)return;
  try{
    const query=new URLSearchParams({limit:'50'});if(cursor)query.set('cursor',cursor);
    const result=await api('/projects/'+encodeURIComponent(project)+'/chats?'+query);
    if(version!==selectionVersion)return;
    state=mergeState(state,{tasks:result.items||[]});chatPages.set(project,result);
    if(!taskId){
      const selected=result.items?.[0];if(selected){taskId=selected.id;memory.select(projectId,taskId);restoreDraft();selectionVersion++;refresh()}
    }
    render();
  }catch{/* Keep the last successfully loaded chat list on a transient failure. */}
}
async function loadRun(cursorField=null,cursor=null){
  if(!runId||!taskId)return;
  const project=projectId,task=taskId,run=runId,version=selectionVersion,key=task+':'+run;
  const pending=key+':'+version;if(runRequests.has(pending))return runRequests.get(pending);
  if(cursorField&&cursor!==runCache.get(key)?.[cursorField+'_next_cursor'])return;
  const fields=['messages','changes','api_events'],counts=runPageCounts.get(key)||{};
  const fetchPage=async(field=null,next=null)=>{
    const query=new URLSearchParams({limit:'100'});if(field&&next)query.set(field+'_cursor',next);
    return api('/tasks/'+encodeURIComponent(task)+'/runs/'+encodeURIComponent(run)+'?'+query);
  };
  const normalize=result=>{
    const detail={...(result.run||result)};
    for(const field of fields){const page=result[field]??detail[field];
      if(page&&!Array.isArray(page)){detail[field]=page.items;detail[field+'_next_cursor']=page.next_cursor}
      else if(page!==undefined)detail[field]=page;
      if(result[field+'_next_cursor']!==undefined)detail[field+'_next_cursor']=result[field+'_next_cursor'];
    }return detail;
  };
  const promise=(async()=>{try{
    let detail=normalize(await fetchPage(cursorField,cursor));
    if(version!==selectionVersion)return;
    if(cursorField){
      const items=detail[cursorField],next=detail[cursorField+'_next_cursor'];
      detail={...runCache.get(key),[cursorField]:[...(runCache.get(key)?.[cursorField]||[]),...(items||[])],[cursorField+'_next_cursor']:next};
      counts[cursorField]=(counts[cursorField]||1)+1;
    }else{
      // Re-fetch the pages the reader has opened; never replace a long view with page one.
      for(const field of fields){
        let next=detail[field+'_next_cursor'];
        for(let page=1;page<(counts[field]||1)&&next;page++){
          const more=normalize(await fetchPage(field,next));
          if(version!==selectionVersion)return;
          detail[field]=[...(detail[field]||[]),...(more[field]||[])];next=more[field+'_next_cursor'];
        }
        if(detail[field]!==undefined)detail[field+'_next_cursor']=next||null;
      }
    }
    if(version!==selectionVersion||project!==projectId)return;
    runPageCounts.set(key,counts);runCache.set(key,{...runCache.get(key),...detail});render();
  }catch(err){if(version===selectionVersion){$('#notice').hidden=false;text('#notice',err.message)}}finally{runRequests.delete(pending)}})();
  runRequests.set(pending,promise);return promise;
}
function render(){if(!state)return;const projects=state.projects||[];if(!projects.some(p=>p.id===projectId)){projectId=projects[0]?.id||'';const saved=memory.selection(projectId);taskId=saved.taskId;runId=saved.runId;memory.select(projectId,taskId,runId);restoreDraft()}const project=currentProject();const task=viewedTask();const live=currentTask();const proxy=state.proxy;const metrics=state.metrics||{};const ready=!!proxy&&!connectionLost;const running=!runId&&live&&activeStates.has(live.state);
  $('#connection').classList.toggle('offline',!ready);$('#connection').innerHTML=`<span class="status-dot ${ready?'online':'offline'}"></span><span>${ready?t('Proxy działa'):t('Brak połączenia')}</span>`;$('#side-status').className='status-dot '+(ready?'online':'offline');text('#side-status-text',ready?t('Proxy działa lokalnie'):t('Proxy jest wyłączone'));text('#breadcrumb-project',project?.name||t('Wybierz projekt'));text('#model-label',state.model||'Codex');text('#total-tokens',compact(metrics.total_tokens));text('#input-tokens',compact(metrics.input_tokens));text('#output-tokens',compact(metrics.output_tokens));text('#cached-tokens',compact(metrics.cached_tokens));text('#generations',compact(metrics.generations??proxy?.stats?.responses_completed??0));text('#active-agents',full(state.active_agents||0));text('#agents-description',t('Zadania uruchomione w panelu'));text('#lines-added',task&&task.added==null?'\u2014':'+'+full(task?.added||0));text('#lines-removed',task&&task.removed==null?'\u2014':'\u2212'+full(task?.removed||0));text('#files-count',t(task?.files===1?'{count} zmieniony plik':'{count} zmienionych plików',{count:full(task?.files||0)}));$('#total-tokens').title=metrics.total_tokens===null?t('Licznik jest zasilany danymi usage zwracanymi przez API.'):t('{count} tokenów raportowanych przez API',{count:full(metrics.total_tokens)});
  $('#project-list').innerHTML=projects.map(p=>`<div class="project-row"><button class="project-button ${p.id===projectId?'selected':''}" data-project="${e(p.id)}" title="${e(p.path)}"><span data-icon="${p.kind==='chat'?'terminal':'folder'}">${icon(p.kind==='chat'?'terminal':'folder')}</span><span>${e(p.name)}</span>${statusMark(projectStatus(p),'project')}</button><button type="button" class="remove-project icon-button" data-remove-project="${e(p.id)}" aria-label="${e(t('Usuń {name} z listy',{name:p.name}))}" title="${e(t('Usuń z listy'))}">${icon('close')}</button></div>`).join('');
  const tasks=(state.tasks||[]).filter(task=>task.project_id===projectId);text('#history-count',full(tasks.length));$('#task-list').innerHTML=tasks.length?tasks.map(task=>`<button class="history-task ${task.id===taskId?'selected':''}" data-task="${e(task.id)}" title="${e(task.title||t('Nowy czat'))}">${statusMark(taskStatus(task))}<span>${e(task.title||t('Nowy czat'))}</span></button>`).join(''):`<p class="sidebar-empty">${t('Tutaj pojawią się Twoje czaty.')}</p>`;
  $('#more-chats').hidden=!chatPages.get(projectId)?.next_cursor;
  text('#task-heading',task?.title||t('Nowy czat'));text('#project-path',project?.kind==='chat'?t(project.access_mode==='full'?'Czat · pełny dostęp do komputera':'Czat · folder tymczasowy')+' · '+project.path:project?.path||t('Wybierz folder projektu'));$('#project-path').title=project?.path||'';
  $('#task-state').innerHTML=statusMark(taskStatus(task))+`<span>${e(task?statusLabel(task.state):t('Gotowe do pracy'))}</span>`;$('#task-state').className='task-state '+e(task?.state||'idle');
  text('#changes-tab-count',full(task?.files||0));text('#agents-tab-count',compact(task?.agents_count??task?.agents?.length));
  $('#stop-task-button').disabled=['stopping','finalizing'].includes(live?.state);$('#run-button').hidden=!!running;$('#stop-task-button').hidden=!running;$('#run-button').disabled=sending||!!running||!project||!state.runner?.available||!ready;$('#new-task-button').disabled=creatingChat||!project;
  text('#composer-hint',running?t('Możesz przygotować szkic następnego polecenia.'):!state.runner?.available?t('Połączenie z Codex jest przygotowywane.'):live?.thread_id?t('Kolejny prompt będzie kontynuacją tego czatu.'):t('Codex będzie pracować w wybranym folderze.'));
  $('#prompt').disabled=false;$('#prompt-form').hidden=!!runId;$('#history-selection').hidden=!runId;text('#selected-run-label',t('Archiwalne uruchomienie · {id}',{id:runId}));
  text('#usage-scope',metrics.since?t('Całe proxy · pomiar od {date}',{date:new Date(metrics.since*1000).toLocaleString(locale(),{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}):t('Całe proxy'));text('#usage-note',metrics.unreported?t('{count} prób bez raportu tokenów. Cache jest częścią wejścia.',{count:full(metrics.unreported)}):t('Tokeny są raportowane po odpowiedzi API. Cache jest częścią wejścia.'));text('#task-token-count',task?.tokens?t('{count} tokenów wątku Codex',{count:full(task.tokens.totalTokens)}):'');$('#task-token-count').hidden=!task?.tokens;renderTask(task,running);renderTraffic(state.timeline||[]);renderActivity(state.activity||[]);text('#updated-at',t('Aktualizacja {time}',{time:new Date().toLocaleTimeString(locale())}));text('#footer-status',ready?t('3 API · {count} żądań od startu proxy',{count:full(proxy.stats?.requests||0)}):t('Uruchom proxy, aby rozpocząć pracę.'));
  if(state.notice){$('#notice').textContent=translateMessage(state.notice);$('#notice').hidden=false}else if(ready){$('#notice').hidden=true}
  controls?.render();history?.setSelectedRun(runId||null);
  translateDom();
}
function renderTask(task,running){const pane=$('#conversation');const key=task?JSON.stringify([language(),task.id,task.run_id,task.state,task.messages,task.approvals,task.messages_next_cursor]):'empty-'+language();if(pane.dataset.key!==key&&!pane.contains(document.activeElement?.closest?.('[data-input-request]'))){const wasAtBottom=pane.scrollHeight-pane.scrollTop-pane.clientHeight<110;pane.dataset.key=key;if(!task){pane.innerHTML=emptyConversation;translateDom(pane)}else if(!task.messages){pane.innerHTML='<div class="empty-secondary"><p><span data-i18n="Ładowanie zadania…">Ładowanie zadania…</span></p></div>'}else{pane.innerHTML=(task.messages||[]).map(m=>{const role=m.role||'assistant';const tool=role==='tool';return `<article class="message ${e(role)}"><div class="message-header"><span class="message-avatar">${role==='user'?t('TY'):tool?'›':'C'}</span><span>${role==='user'?t('Ty'):tool?e((m.agent_name?m.agent_name+' \u00b7 ':'')+(translateMessage(m.title)||t('Narzędzie'))):e(m.agent_name||'Codex')}</span></div>${tool?`<pre class="tool-output">${e(m.text)}</pre>`:`<div class="message-body">${e(role==='error'?translateMessage(m.text):m.text)}</div>`}</article>`}).join('');for(const a of task.approvals||[]){if(a.kind==='input'){pane.innerHTML+=inputCard(a);continue}pane.innerHTML+=`<div class="approval-card"><strong>${e(translateMessage(a.title)||t('Codex potrzebuje Twojej decyzji'))}</strong><p>${e(a.description||'')}</p><div class="approval-actions"><button data-approval="${e(a.id)}" data-decision="accept"><span data-i18n="Zezwól">Zezwól</span></button><button data-approval="${e(a.id)}" data-decision="decline"><span data-i18n="Odmów">Odmów</span></button></div></div>`}if(running)pane.innerHTML+='<div class="task-progress"><span class="working-dot"></span> '+(task.state==='awaiting_input'?t('Codex czeka na Twoją odpowiedź.'):task.state==='finalizing'?t('Zapisywanie wynik\u00f3w'):task.state==='stopping'?t('Zatrzymywanie zadania…'):t('Codex pracuje nad zadaniem…'))+'</div>';if(!task.messages?.length&&!running)pane.innerHTML='<div class="empty-secondary"><p><span data-i18n="Zadanie nie zawiera jeszcze wiadomości.">Zadanie nie zawiera jeszcze wiadomości.</span></p></div>';if(wasAtBottom||pane.dataset.task!==task.id)pane.scrollTop=pane.scrollHeight;pane.dataset.task=task.id}}
  if(runId&&task?.messages_next_cursor&&!pane.querySelector('[data-more-run="messages"]'))pane.insertAdjacentHTML('beforeend',moreRunButton('messages',task.messages_next_cursor));
  renderChanges(task);renderApiEvents(task);
  $('#agents').innerHTML=task?.agents?.length?task.agents.map((a,i)=>`<div class="agent-row"><div class="agent-avatar">${i?'A'+i:'C'}</div><div><strong>${e(a.name||'Codex')}</strong><small>${e(translateMessage(a.description)||(i?t('Agent pomocniczy'):t('Agent główny')))}</small></div><span class="provider-state ${a.status==='running'?'busy':''}">${e(statusLabel(a.status))}</span></div>`).join(''):'<div class="empty-secondary"><span data-icon="agents">'+icon('agents')+'</span><p><span data-i18n="Tutaj zobaczysz agentów pracujących nad wybranym zadaniem.">Tutaj zobaczysz agentów pracujących nad wybranym zadaniem.</span></p></div>';setTab(tab)
}
const changeViews=new Map();
function renderChanges(task){
  const pane=$('#changes'),view=task?task.id+':'+(task.run_id||'chat'):'empty';
  if(pane.dataset.view){changeViews.set(pane.dataset.view,{expanded:[...pane.querySelectorAll('details[open]')].map(el=>el.dataset.path),scroll:pane.scrollTop,code:[...pane.querySelectorAll('details')].map(el=>[el.dataset.path,el.querySelector('pre')?.scrollLeft||0])})}
  const changes=task?.changes||[],key=JSON.stringify([view,language(),changes,task?.scan_error,task?.scan_status,task?.changes_next_cursor]);
  if(pane.dataset.key===key)return;
  const saved=changeViews.get(view)||{expanded:[],scroll:0,code:[]},expanded=new Set(saved.expanded);
  pane.dataset.key=key;pane.dataset.view=view;
  pane.innerHTML=changes.length?changes.map(c=>`<details class="file-change" data-path="${e(c.path)}" ${expanded.has(c.path)?'open':''}><summary><span data-icon="file">${icon('file')}</span><span title="${e(c.path)}">${e(c.path)}</span><small>${e(translateMessage(c.kind)||t('zmiana'))}</small><b class="added">+${full(c.added)}</b><b class="removed">−${full(c.removed)}</b></summary><pre>${String(c.diff||'').split('\n').map(line=>`<span class="diff-line ${line.startsWith('+')&&!line.startsWith('+++')?'plus':line.startsWith('-')&&!line.startsWith('---')?'minus':line.startsWith('@@')?'hunk':''}">${e(line)||' '}</span>`).join('')}</pre></details>`).join(''):`<div class="empty-secondary">${icon('code')}<p>${t(task?.state==='loading'?'Ładowanie zmian…':'Zmiany w plikach pojawią się podczas pracy nad zadaniem.')}</p></div>`;
  pane.insertAdjacentHTML('afterbegin',`<p class="scan-note">${e(translateMessage(task?.scan_error)||(task?.scan_skipped?t('Część plików pominięto ze względu na rozmiar lub dostęp.'):t('Różnica od początku tego uruchomienia. Cofnięte edycje pozostają w historii.')))}</p>`);
  if(runId&&task?.changes_next_cursor)pane.insertAdjacentHTML('beforeend',moreRunButton('changes',task.changes_next_cursor));
  pane.scrollTop=saved.scroll;
  const offsets=new Map(saved.code);for(const detail of pane.querySelectorAll('details'))detail.querySelector('pre').scrollLeft=offsets.get(detail.dataset.path)||0;
}
function moreRunButton(field,cursor){return `<button class="small-button more-run" data-more-run="${e(field)}" data-cursor="${e(cursor)}">${t('Pokaż kolejne')}</button>`}
function renderApiEvents(task){
  const pane=$('#api-events'),events=task?.api_events||[],key=JSON.stringify([language(),task?.id,task?.run_id,events,task?.api_events_next_cursor]);
  if(pane.dataset.key===key)return;
  const scroll=pane.scrollTop;pane.dataset.key=key;
  pane.innerHTML=events.length?events.map(event=>`<article class="run-api-event"><span>${e(event.provider_id||event.api_id||'—')}</span><strong>${e(translateMessage(event.title||event.kind||event.status||''))}</strong><time>${event.time||event.timestamp?e(clock(event.time||event.timestamp)):'—'}</time><small>${e(event.role?event.role==='main'?t('Główne'):t('Pomocnicze'):'')}</small></article>`).join(''):`<div class="empty-secondary"><p>${t('Zdarzenia API tego uruchomienia pojawią się tutaj.')}</p></div>`;
  if(runId&&task?.api_events_next_cursor)pane.insertAdjacentHTML('beforeend',moreRunButton('api_events',task.api_events_next_cursor));
  pane.scrollTop=scroll;
}
function setTab(value){tab=value;document.querySelectorAll('[data-tab]').forEach(b=>{b.setAttribute('aria-selected',String(b.dataset.tab===tab));b.tabIndex=b.dataset.tab===tab?0:-1});['conversation','changes','agents','api-events'].forEach(id=>$('#'+id).hidden=id!==tab)}
function renderTraffic(values){const total=values.reduce((sum,b)=>sum+(b.tokens||0),0);text('#hour-tokens',values.length?compact(total):'—');if(!values.length||!total){$('#traffic-chart').innerHTML='<div class="chart-empty"><span data-i18n="Historia pojawi się po pierwszej odpowiedzi.">Historia pojawi się po pierwszej odpowiedzi.</span></div>';return}const maximum=Math.max(...values.map(v=>v.tokens||0),1);const w=300,h=91,step=w/values.length;$('#traffic-chart').innerHTML=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${e(t('Tokeny raportowane przez API w ostatniej godzinie'))}">${values.map((v,i)=>{const height=Math.max(2,(v.tokens||0)/maximum*(h-9));return `<rect x="${(i*step+1).toFixed(2)}" y="${h-height}" width="${Math.max(1,step-2)}" height="${height}" rx="1.3" fill="${i===values.length-1?'#c0f475':'#809d5c'}" opacity="${v.tokens?.75:.12}"><title>${e(clock(v.time))}: ${e(t('{count} tokenów',{count:full(v.tokens)}))}</title></rect>`}).join('')}</svg>`}
function activitySubtitle(event){
  if(!['attempt','completed','rotation','cooldown','failed','client_disconnected','upstream_interrupted'].includes(event.kind))return event.subtitle;
  const tokens=/^([\d\s,]+) tokenów$/.exec(event.subtitle||'');
  return tokens?t('{count} tokenów',{count:full(Number(tokens[1].replace(/[^0-9]/g,'')))}):translateMessage(event.subtitle);
}
function renderActivity(events){$('#activity-feed').innerHTML=events.length?events.slice(0,30).map(v=>`<div class="activity-event ${v.level==='error'?'error':''}"><span class="event-icon">${icon(v.icon||'activity')}</span><div class="event-text"><strong>${e(translateMessage(v.title||v.message||v.kind))}</strong>${v.subtitle?`<span>${e(activitySubtitle(v))}</span>`:''}</div><time>${clock(v.time)}</time></div>`).join(''):'<div class="activity-empty">'+icon('activity')+'<p><span data-i18n="Połączenia, uruchomione narzędzia i przełączenia API pojawią się tutaj.">Połączenia, uruchomione narzędzia i przełączenia API pojawią się tutaj.</span></p></div>'}
async function refresh(){
  const selection={project_id:projectId,task_id:taskId,run_id:runId},version=selectionVersion,key=JSON.stringify([version,selection]);
  if(requests.has(key))return requests.get(key);
  const request=++requestNumber;
  const promise=(async()=>{try{
    const result=await api('/state?'+new URLSearchParams(selection));
    if(version!==selectionVersion||request<acceptedRequest||!matchesSelection(result,selection))return;
    acceptedRequest=request;state=mergeState(state,result);connectionLost=false;$('#session-notice').hidden=true;
    const initialProject=projectId;render();history?.refresh();
    if(!selection.project_id||initialProject!==projectId){loadChats();if(runId)loadRun()}
  }catch(err){
    if(version!==selectionVersion||request<acceptedRequest)return;
    connectionLost=true;render();$('#notice').hidden=false;$('#notice').textContent=err.message;text('#updated-at',t('Połączenie przerwane'));
  }finally{requests.delete(key)}})();
  requests.set(key,promise);return promise;
}
async function poll(){await refresh();if(runId&&activeStates.has(viewedTask()?.state))await loadRun();setTimeout(poll,1250)}
let folderCurrent='',folderQueryTimer,folderRequest=0;
$('#folder-list').insertAdjacentHTML('beforebegin','<label class="folder-search"><span>'+icon('search')+'</span><input id="folder-search" type="search" placeholder="Szukaj folderu w tym miejscu" data-i18n-placeholder="Szukaj folderu w tym miejscu" aria-label="Szukaj folderu w tym miejscu" data-i18n-aria-label="Szukaj folderu w tym miejscu" autocomplete="off"></label><p id="folder-results" class="folder-results"></p>');
async function browse(path='',query=''){
 const request=++folderRequest;
 if(!query)$('#folder-search').value='';
 try{const result=await api('/folders?'+new URLSearchParams({path,q:query}));if(request!==folderRequest)return;folderCurrent=result.path;folderParent=result.parent;$('#folder-path').value=result.path;text('#folder-current',result.path);$('#folder-list').innerHTML=result.folders.length?result.folders.map(f=>`<button type="button" class="folder-item" data-folder="${e(f.path)}"><span>${icon('folder')}</span>${e(f.name)}<span>${icon('arrow-right')}</span></button>`).join(''):'<p class="sidebar-empty">'+(query?t('Brak folderów pasujących do wyszukiwania.'):t('Ten folder nie zawiera podfolderów. Możesz dodać go jako projekt.'))+'</p>';text('#folder-results',t('{count} / {total} folderów',{count:full(result.folders.length),total:full(result.total)}));text('#folder-error','')}catch(err){if(request===folderRequest)text('#folder-error',err.message)}
}
$('#folder-search').addEventListener('input',event=>{clearTimeout(folderQueryTimer);const query=event.target.value;folderQueryTimer=setTimeout(()=>browse(folderCurrent,query),180)});
$('#folder-search').addEventListener('keydown',event=>{if(event.key==='Enter')event.preventDefault()});

function updateWorkspaceForm(){
  const chat=$('[name="workspace-kind"]:checked').value==='chat',full=chat&&$('[name="chat-access"]:checked').value==='full';
  $('#project-folder-fields').hidden=chat;$('#chat-access-fields').hidden=!chat;$('#folder-path').required=!chat;
  $('#full-access-label').hidden=!full;$('#full-access-confirmed').required=full;
  $('#workspace-create-note').dataset.i18n=chat?'Rozmowa i folder czatu pozostają po odświeżeniu aplikacji.':'Codex będzie pracować w wybranym folderze.';
  translateDom($('#workspace-create-note'));
}
document.querySelectorAll('[name="workspace-kind"],[name="chat-access"]').forEach(el=>el.addEventListener('change',updateWorkspaceForm));
document.querySelectorAll('[data-action="add-project"]').forEach(b=>b.addEventListener('click',()=>{
  text('#folder-error','');updateWorkspaceForm();$('#project-dialog').showModal();
  if(!$('#folder-path').value)browse();
}));
$('#close-project-dialog').addEventListener('click',()=>$('#project-dialog').close());
$('#project-dialog').addEventListener('click',event=>{if(event.target===$('#project-dialog'))$('#project-dialog').close()});
$('#folder-list').addEventListener('click',event=>{const b=event.target.closest('[data-folder]');if(b)browse(b.dataset.folder)});
$('#browse-path').addEventListener('click',()=>browse($('#folder-path').value));
$('#folder-up').addEventListener('click',()=>browse(folderParent));
$('#folder-path').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();browse(event.target.value)}});
$('#project-form').addEventListener('submit',async event=>{
  event.preventDefault();if($('#create-workspace').disabled)return;
  const kind=$('[name="workspace-kind"]:checked').value;
  const body={kind,name:$('#workspace-name').value.trim()};
  if(kind==='project')body.path=$('#folder-path').value;
  else {body.access_mode=$('[name="chat-access"]:checked').value;if(body.access_mode==='full')body.full_access_confirmed=$('#full-access-confirmed').checked;}
  $('#create-workspace').disabled=true;text('#folder-error','');
  try{
    const requestId=memory.request('$workspaces','create',body);
    const project=await api('/projects',{...body,client_request_id:requestId});
    memory.accepted('$workspaces','create');
    state={...state,projects:[...(state?.projects||[]).filter(p=>p.id!==project.id),project]};
    setProject(project.id);$('#project-dialog').close();$('#workspace-name').value='';$('#full-access-confirmed').checked=false;
    toast(t(kind==='chat'?'Czat został utworzony.':'Projekt został dodany.'));
  }catch(err){text('#folder-error',err.message)}finally{$('#create-workspace').disabled=false}
});
let removingProject=null;
$('#project-list').addEventListener('click',event=>{
  const remove=event.target.closest('[data-remove-project]');
  if(remove){
    removingProject=state?.projects?.find(p=>p.id===remove.dataset.removeProject);if(!removingProject)return;
    text('#remove-project-name',removingProject.name);text('#remove-project-error','');
    $('#confirm-remove-project').disabled=false;$('#remove-project-dialog').showModal();return;
  }
  const button=event.target.closest('[data-project]');if(button)setProject(button.dataset.project);
});
$('#remove-project-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!removingProject||$('#confirm-remove-project').disabled)return;
  const removing=removingProject;$('#confirm-remove-project').disabled=true;
  try{
    await api('/projects/'+encodeURIComponent(removing.id),{},true,'DELETE');
    saveDraft();selectionVersion++;
    state={...state,projects:state.projects.filter(p=>p.id!==removing.id)};
    if(projectId===removing.id){projectId='';taskId='';runId='';$('#prompt').value='';}
    chatPages.delete(removing.id);render();$('#remove-project-dialog').close();await refresh();loadChats();
    toast(t('Usunięto z listy. Pliki i historia zostały zachowane.'));
  }catch(error){text('#remove-project-error',error.message)}finally{$('#confirm-remove-project').disabled=false}
});
$('#task-list').addEventListener('click',event=>{const button=event.target.closest('[data-task]');if(button)setTask(button.dataset.task)});
$('#more-chats').addEventListener('click',()=>loadChats(true));
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>setTab(button.dataset.tab)));
$('#refresh-button').addEventListener('click',()=>{refresh();loadChats();if(runId)loadRun();history?.refresh({force:true})});
$('#overview-button').addEventListener('click',()=>select(projectId,taskId));
$('#history-live-button').addEventListener('click',()=>select(projectId,taskId));
$('#menu-button').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));
async function createChat(){
  if(!projectId||creatingChat)return null;
  const project=projectId,version=selectionVersion;creatingChat=true;render();
  try{
    const body={project_id:project},requestId=memory.request(project,'create-chat',body);
    const result=await api('/chats',{...body,client_request_id:requestId});
    memory.accepted(project,'create-chat');
    const chat={project_id:project,state:'idle',messages:[],changes:[],...(result.chat||result)};
    state=mergeState(state,{tasks:[chat]});
    if(version===selectionVersion){select(project,chat.id);$('#prompt').focus()}
    return chat;
  }catch(error){toast(error.message,true);return null}finally{creatingChat=false;render()}
}
$('#new-task-button').addEventListener('click',createChat);
async function submitTask(input){
  const project=input.project_id,task=input.continue_task||'',body={...input};
  body.client_request_id ||= memory.request(project,task,input);
  const result=await api('/tasks',body),previous=state?.tasks?.find(value=>value.id===result.id);
  const summary={...previous,id:result.id,project_id:project,run_id:result.run_id,state:result.state};
  if(previous?.run_id!==result.run_id)Object.assign(summary,{changes:[],files:0,added:0,removed:0,api_events:[]});
  state=mergeState(state,{tasks:[summary]});memory.accepted(project,task);
  if(memory.draft(project,task).trim()===input.prompt.trim()){
    memory.saveDraft(project,task,'');
    if(projectId===project&&taskId===task&&$('#prompt').value.trim()===input.prompt.trim())$('#prompt').value='';
  }
  if(task!==result.id)memory.saveDraft(project,result.id,memory.draft(project,task));
  if(projectId===project&&taskId===task){select(project,result.id);restoreDraft()}
  await refresh();history?.refresh({force:true});return result;
}
$('#prompt-form').addEventListener('submit',async event=>{
  event.preventDefault();const prompt=$('#prompt').value.trim();
  if(!prompt||sending||$('#run-button').disabled||runId||activeStates.has(currentTask()?.state))return;
  const project=projectId,task=taskId;saveDraft();sending=true;$('#submit-error').hidden=true;render();
  try{
    await submitTask({project_id:project,prompt,...controls.taskOptions(),continue_task:task||null});
    toast(t('Polecenie zostało przyjęte.'));
  }catch(error){if(projectId===project&&taskId===task){$('#submit-error').hidden=false;text('#submit-error',error.message)}toast(error.message,true)}
  finally{sending=false;render()}
});
$('#prompt').addEventListener('input',saveDraft);
window.addEventListener('pagehide',saveDraft);
$('#prompt').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('#prompt-form').requestSubmit()}});
$('#stop-task-button').addEventListener('click',async()=>{if(!taskId||runId)return;try{await api('/tasks/'+encodeURIComponent(taskId)+'/stop',{});toast(t('Wysłano zatrzymanie zadania.'));await refresh()}catch(error){toast(error.message,true)}});
$('#conversation').addEventListener('click',async event=>{const button=event.target.closest('[data-approval]');if(!button||runId)return;try{await api('/tasks/'+encodeURIComponent(taskId)+'/approval',{id:button.dataset.approval,decision:button.dataset.decision});await refresh()}catch(error){toast(error.message,true)}});
$('#workspace').addEventListener('click',event=>{const button=event.target.closest('[data-more-run]');if(button)loadRun(button.dataset.moreRun,button.dataset.cursor)});

const inputDrafts=new Map();
$('#conversation').addEventListener('input',event=>{const request=event.target.closest('[data-input-request]');if(request&&event.target.dataset.questionId)inputDrafts.set(request.dataset.inputRequest+':'+event.target.dataset.questionId,event.target.value)});
function inputCard(a){return `<form class="approval-card input-card" data-input-request="${e(a.id)}"><strong>${e(translateMessage(a.title))}</strong>${(a.questions||[]).map(q=>`<label>${e(q.question)}${q.options?.length?`<small class="question-options">${q.options.map(o=>`${e(o.label)}${o.description?' — '+e(o.description):''}`).join('<br>')}</small>`:''}<input type="${q.isSecret?'password':'text'}" data-question-id="${e(q.id)}" required maxlength="10000" autocomplete="off" value="${e(inputDrafts.get(a.id+':'+q.id)||'')}"></label>`).join('')}<button type="submit" class="small-button"><span data-i18n="Wyślij odpowiedź">Wyślij odpowiedź</span></button></form>`}
$('#conversation').addEventListener('submit',async event=>{const form=event.target.closest('[data-input-request]');if(!form)return;event.preventDefault();const answers={};form.querySelectorAll('[data-question-id]').forEach(el=>answers[el.dataset.questionId]=el.value);try{await api('/tasks/'+taskId+'/approval',{id:form.dataset.inputRequest,answers});document.activeElement.blur();await refresh()}catch(err){toast(err.message,true)}});
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')];const index=event.key==='Home'?0:event.key==='End'?tabs.length-1:(tabs.indexOf(button)+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;setTab(tabs[index].dataset.tab);tabs[index].focus()}));
registerPanelTools({getState:()=>state,refresh,startTask:submitTask});

controls=createControls({api,getState:()=>state,getTask:currentTask,getProject:currentProject,isHistory:()=>!!runId,refresh,toast,icon,escape:e,compact,full});
history=createHistory({api,getProject:currentProject,getTask:currentTask,onSelectRun:run=>select(run.project_id||projectId,run.task_id,run.id)});
history.init();history.setSelectedRun(runId||null);
onLanguageChange(()=>{
  render();
  for(const selector of ['#toast','#submit-error','#folder-error','#notice']){const el=$(selector);if(el&&!el.hidden)el.textContent=translateMessage(el.textContent)}
  if($('#project-dialog').open)updateWorkspaceForm();
});
translateDom();
$('#logout-button').addEventListener('click',async()=>{
  saveDraft();const button=$('#logout-button');button.disabled=true;
  try{await renewSession();await refresh();toast(t('Sesja lokalna została odnowiona.'))}
  catch(error){toast(error.message,true)}finally{button.disabled=false}
});
restoreDraft();

refresh().then(()=>{loadChats();if(runId)loadRun()});
setTimeout(poll,1250);
