import {t, language, onLanguageChange, translateMessage} from './i18n.js';

export function createControls({api, getState, getTask, getProject, isHistory=()=>false, refresh, toast, icon, escape: e, compact, full}) {
  const $ = selector => document.querySelector(selector);
  let known = [], models = [], primary = '', selected = [], initialized = false, selectionProject = null, pendingPreferences = null;
  let editing = null, deleting = false, settingsProject = null, agentsVersion = null, agentsFile = null, skillItems = [], lastTask = null, providerKey = '', routingTask = null;
  const projectPreferences = new Map();
  const active = task => task && ['queued','starting','running','stopping','awaiting_input','finalizing'].includes(task.state);
  const providers = () => (getState()?.proxy?.providers || known).map(p => ({...known.find(k=>k.id===p.id), ...p}));
  const compatible = () => providers().filter(p => p.configured && p.enabled !== false && (p.model || p.deployment) === $('#model-picker').value);
  const textLabels = new Map();
  const setText = (id, text) => { textLabels.set(id, text); $(id).textContent = translateMessage(text); };
  const notify = (message, error) => toast(translateMessage(message), error);
  let settingsGeneration=0,settingsReady=false,settingsSaving=false,settingsBaseline=null,settingsDraft={};
  const settingsFields={main_prompt:'#main-prompt',coordinator_prompt:'#coordinator-prompt',permission_mode:'#default-permission',effort:'#default-effort'};
  document.querySelectorAll('[data-pref]').forEach(el=>settingsFields[el.dataset.pref]='[data-pref="'+el.dataset.pref+'"]');
  try{const draft=JSON.parse(localStorage.getItem('3api-settings-draft-v1'));if(draft&&typeof draft==='object'&&!Array.isArray(draft))for(const key of Object.keys(settingsFields))if(Object.hasOwn(draft,key)&&['string','number'].includes(typeof draft[key]))settingsDraft[key]=draft[key]}catch{/* A browser draft is optional. */}
  function settingsValue(){return Object.fromEntries(Object.entries(settingsFields).map(([key,selector])=>[key,key in {main_prompt:1,coordinator_prompt:1,permission_mode:1,effort:1}?$(selector).value:Number($(selector).value)]))}
  function settingsInputs(enabled){for(const selector of Object.values(settingsFields))$(selector).disabled=!enabled;$('#save-settings').disabled=!enabled||settingsSaving;}
  function saveSettingsDraft(){
    if(!settingsReady||!settingsBaseline)return;
    settingsDraft=Object.fromEntries(Object.entries(settingsValue()).filter(([key,value])=>value!==settingsBaseline[key]));
    try{if(Object.keys(settingsDraft).length)localStorage.setItem('3api-settings-draft-v1',JSON.stringify(settingsDraft));else localStorage.removeItem('3api-settings-draft-v1')}catch{/* Keep editing when browser storage is unavailable. */}
    $('#settings-draft-note').hidden=!Object.keys(settingsDraft).length;
  }
  for(const selector of Object.values(settingsFields))for(const event of ['input','change'])$(selector).addEventListener(event,saveSettingsDraft);

  function options(el, values, chosen) {
    const markup = values.map(v => `<option value="${e(v.value)}">${e(v.label)}</option>`).join('');
    if (el.innerHTML !== markup) el.innerHTML = markup;
    if (values.some(v=>v.value===chosen)) el.value=chosen;
  }
  function readPreference(id) {
    if(projectPreferences.has(id))return projectPreferences.get(id);
    try {
      const value=JSON.parse(localStorage.getItem('3api-controls-v2:'+id));
      if(value && typeof value==='object' && !Array.isArray(value))return value;
    } catch { /* Private browsing or invalid stored data must not block the panel. */ }
    return null;
  }
  function save() {
    if(!selectionProject || !initialized)return;
    const value={model:$('#model-picker').value,effort:$('#effort').value,permission_mode:$('#permission-mode').value,mode:$('#experimental').checked?'experimental':'standard',primary,api_ids:[...selected]};
    projectPreferences.set(selectionProject,value);
    try { localStorage.setItem('3api-controls-v2:'+selectionProject,JSON.stringify(value)); } catch { /* Keep the current session usable without browser storage. */ }
  }
  function effortsFor(model) {
    const info=models.find(m=>m.model===model);
    const values=info?.supportedReasoningEfforts?.map(v=>typeof v==='string'?v:v.reasoningEffort).filter(Boolean);
    return values?.length ? values : ['low','medium','high','xhigh','max','ultra'];
  }
  function effortOptions(chosen) {
    options($('#effort'), effortsFor($('#model-picker').value).map(value=>({value,label:({xhigh:t('XH · Bardzo wysokie'),ultra:t('Ultra · agenci'),max:'Max',high:t('Wysokie'),medium:t('Średnie'),low:t('Niskie')})[value]||value})), chosen);
  }
  function restoreProject(state, list) {
    const id=getProject()?.id||'';
    if(id!==selectionProject){
      save();selectionProject=id;initialized=false;pendingPreferences=readPreference(id);providerKey='';
    }
    if(initialized || !list.length)return;
    const task=getTask()?.project_id===id ? getTask() : null;
    // A task is only a migration/default source. Switching chats never replaces project preferences.
    const saved=pendingPreferences||{}, initial=pendingPreferences?null:task;
    const eligible=list.filter(p=>p.configured&&p.enabled!==false);
    let preferred=saved.primary||initial?.api_ids?.[0]||'';
    if(!preferred){try { preferred=localStorage.getItem('3api-primary')||''; } catch { /* Optional legacy preference. */ }}
    const provider=eligible.find(p=>p.id===preferred)||eligible[0];
    const chosen=saved.model||initial?.model||provider?.model||provider?.deployment||state.model;
    const names=[...new Set(list.map(p=>p.model||p.deployment).filter(Boolean))];
    options($('#model-picker'),names.map(value=>({value,label:value})),chosen);
    const ids=Array.isArray(saved.api_ids)?saved.api_ids:initial?.api_ids;
    selected=Array.isArray(ids)?ids.filter(id=>typeof id==='string'):compatible().map(p=>p.id);
    primary=preferred||selected[0]||'';
    effortOptions(saved.effort||initial?.effort||state.defaults?.effort||'max');
    $('#permission-mode').value=saved.permission_mode||initial?.permission_mode||state.defaults?.permission_mode||'approval';
    if(getProject()?.kind==='chat')$('#permission-mode').value=getProject().access_mode==='full'?'yolo':'approval';
    if(!$('#permission-mode').value)$('#permission-mode').value='approval';
    $('#experimental').checked=(saved.mode||initial?.mode)==='experimental';
    initialized=true;pendingPreferences=null;
  }
  function updateSelection() {
    const workspace=getProject();
    if(workspace?.kind==='chat')$('#permission-mode').value=workspace.access_mode==='full'?'yolo':'approval';
    const available=compatible();
    if (!available.some(p=>p.id===primary)) primary=available[0]?.id || '';
    selected=selected.filter(id=>available.some(p=>p.id===id));
    if (primary && !selected.includes(primary)) selected.unshift(primary);
    selected=[...new Set([primary,...selected.filter(id=>id!==primary)].filter(Boolean))];
    options($('#primary-api'), available.map(p=>({value:p.id,label:p.label||p.id})), primary);
    const info=models.find(m=>m.model===$('#model-picker').value);
    const efforts=effortsFor($('#model-picker').value);
    effortOptions($('#effort').value);
    $('#experimental').disabled=!!info && !efforts.includes('ultra');
    if ($('#experimental').disabled) $('#experimental').checked=false;
    const experiment=$('#experimental').checked;
    $('#effort').disabled=experiment||active(getTask());
    const role = id => !experiment ? (id===primary?t('preferowane'):t('pula API')) : id===primary?t('Ultra → rezerwa'):selected.indexOf(id)>0?'Ultra '+selected.indexOf(id):t('wykonawca');
    const chips=available.map(p=>`<label class="api-chip ${selected.includes(p.id)?'checked':''}"><input type="checkbox" data-cohort="${e(p.id)}" ${selected.includes(p.id)?'checked':''} ${p.id===primary||active(getTask())?'disabled':''}><span>${e(p.label||p.id)}<small>${e(role(p.id))}</small></span></label>`).join('') || `<span class="field-help">${e(t('Dodaj lub włącz API, aby rozpocząć.'))}</span>`;
    if($('#api-cohort').dataset.markup!==chips){$('#api-cohort').innerHTML=chips;$('#api-cohort').dataset.markup=chips}
    setText('#mode-description', t(experiment ? 'Polcio, wybierz dokładnie 3 API. Główne planuje na Ultra; pozostałe dwa pracują równolegle na Ultra w osobnych kopiach projektu. Po planie główne API jest rezerwą na wypadek limitu. Panel sprawdza kolizje i łączy gotowe pliki.' : $('#effort').value==='ultra' ? 'Ultra może uruchamiać subagentów. Główny wątek preferuje wolne API, a pomocnicze współdzielą API pomocnicze z zaznaczonej puli. Zajęcie jest wspólne dla wszystkich projektów; rzeczywiste role widać na kartach API.' : 'Zaznaczone API tworzą pulę dla tego projektu. Preferencja nie oznacza rezerwacji; rzeczywiste przypisania i limity widać na kartach API. Po limicie pracę może przejąć inne zgodne API.'));
    setText('#permission-description', t(workspace?.kind==='chat' ? workspace.access_mode==='full' ? 'Czat · pełny dostęp do komputera.' : 'Czat · zapis w folderze tymczasowym, zatwierdzanie dodatkowych operacji.' : $('#permission-mode').value==='yolo' ? 'YOLO: dostęp do plików i sieci, bez pytań o zgodę.' : 'Zatwierdzanie: zapis w projekcie; dodatkowy dostęp wymaga Twojej zgody.'));
    $('#primary-api').disabled=!!routingTask||!!active(getTask())&&['experimental'].includes(getTask().mode)||getTask()?.state==='finalizing';
    $('#permission-mode').disabled=!!active(getTask())||workspace?.kind==='chat';
    $('#model-picker').disabled=!!active(getTask());
    if(active(getTask())) $('#experimental').disabled=true;
    save();
  }

  function renderProviders() {
    const state=getState(), list=providers(), metrics=state?.metrics||{};
    const task=getTask(), projects=state?.projects||[], tasks=state?.tasks||[];
    const projectName=assignment=>projects.find(p=>p.id===assignment.project_id)?.name||assignment.project_name||assignment.project_id||t('Nieprzypisany projekt');
    const key=JSON.stringify([language(),selectionProject,primary,list,metrics.providers,projects.map(p=>[p.id,p.name]),tasks.map(v=>[v.id,v.title]),task?.id,task?.state,task?.mode,task?.model,routingTask]);
    if(key===providerKey)return;
    providerKey=key;
    const open=new Set([...$('#provider-list').querySelectorAll('details[open]')].map(v=>v.dataset.provider));
    const scroll=new Map([...$('#provider-list').querySelectorAll('[data-assignment-list]')].map(v=>[v.dataset.assignmentList,v.scrollTop]));
    const focused=document.activeElement?.closest('[data-select-api], [data-edit-api]');
    const focusSelector=focused?.hasAttribute('data-select-api')?'data-select-api':focused?.hasAttribute('data-edit-api')?'data-edit-api':null;
    const focusId=focusSelector?focused.getAttribute(focusSelector):null;
    setText('#providers-count', `${list.filter(p=>p.available).length} / ${list.length}`);
    $('#provider-list').innerHTML=list.map((p,i)=>{
      const usage=metrics.providers?.find(v=>v.id===p.id)||{};
      const assignments=Array.isArray(p.assignments)?p.assignments:[];
      const count=(value,role)=>Number.isFinite(value)?Math.max(0,value):assignments.filter(a=>a.role===role).length;
      const main=count(p.main_count,'main'), auxiliary=count(p.auxiliary_count,'auxiliary');
      const hasRoles=Number.isFinite(p.main_count)||Number.isFinite(p.auxiliary_count)||Array.isArray(p.assignments);
      const cooling=p.cooldown_remaining_seconds>0, working=p.in_flight>0, reserved=main+auxiliary>0;
      const disabled=p.enabled===false||!p.configured;
      const status=t(p.enabled===false?'Wyłączone':!p.configured?'Brak klucza':cooling?'Limit / przerwa · {seconds} s':working?'Pracuje · {count}':reserved?'Zarezerwowane':hasRoles?'Wolne':'Stan niedostępny',{count:full(p.in_flight||0),seconds:full(Math.ceil(p.cooldown_remaining_seconds||0))});
      const roleTag=(role,label)=>`<span class="provider-role ${role}" data-provider-role="${role}">${e(label)}</span>`;
      const roles=(main?roleTag('main',t('Główne · {count}',{count:full(main)})):'')+(auxiliary?roleTag('auxiliary',t('Pomocnicze · {count}',{count:full(auxiliary)})):'')||roleTag(disabled?'disabled':cooling?'cooldown':working?'busy':hasRoles?'free':'unknown',t(disabled?'Niedostępne':cooling?'Przerwa':working?'Żądanie w toku':hasRoles?'Wolne API':'Brak danych o rolach'));
      const assignedProjects=[...new Set(assignments.map(projectName))];
      const assignmentRows=assignments.map(a=>{
        const title=tasks.find(v=>v.id===a.task_id)?.title||a.task_title||a.task_id||'—';
        const role=a.role==='main'?'main':a.role==='auxiliary'?'auxiliary':'unknown';
        return `<li data-provider-assignment data-project-id="${e(a.project_id||'')}" data-task-id="${e(a.task_id||'')}" data-run-id="${e(a.run_id||'')}" data-thread-id="${e(a.thread_id||'')}"><div class="assignment-heading"><strong>${e(projectName(a))}</strong>${roleTag(role,t(role==='main'?'Główne':role==='auxiliary'?'Pomocnicze':'Rola nieznana'))}</div><span>${e(t('Czat: {task}',{task:title}))}</span>${a.run_id?`<small>${e(t('Uruchomienie: {run}',{run:a.run_id}))}</small>`:''}${a.thread_id?`<small>${e(t('Wątek: {thread}',{thread:a.thread_id}))}</small>`:''}</li>`;
      }).join('');
      const cannotSelect=disabled||!!routingTask||active(task)&&(task.mode==='experimental'||task.state==='finalizing'||(p.model||p.deployment)!==(task.model||$('#model-picker').value));
      const budget=p.token_budget;
      const budgetHtml=budget?`<div class="provider-budget ${budget.hard_reached?'hard':budget.soft_reached?'soft':''}" data-token-budget="${e(p.id)}"><div><span>${e(t('TPM · ostatnie 60 s'))}</span><strong>${full(budget.total_tokens||0)} / ${full(budget.limit)}</strong></div><progress max="${e(budget.limit||1)}" value="${e(Math.min(budget.total_tokens||0,budget.limit||1))}" aria-label="${e(t('Wykorzystany budżet tokenów'))}"></progress><p>${e(t('Usage: {used} · rezerwacja: {reserved} · szacunek: {estimated}',{used:full(budget.used_tokens||0),reserved:full(budget.reserved_tokens||0),estimated:full(budget.estimated_tokens||0)}))}</p>${budget.hard_reached?`<p class="budget-note">${e(t('Próg twardy · ponowna próba za {seconds} s',{seconds:full(Math.ceil(budget.retry_after_seconds||0))}))}</p>`:budget.soft_reached?`<p class="budget-note">${e(t('Próg miękki · preferowane inne API'))}</p>`:''}</div>`:'';
      return `<article class="provider-card ${p.id===primary?'is-active':''}" style="--api-color:${['#94b8ff','#86cfc6','#e7be7b','#c6a1ee'][i%4]}"><details data-provider="${e(p.id)}" ${open.has(p.id)?'open':''}>
        <summary><div class="provider-top"><span class="provider-number">${String(i+1).padStart(2,'0')}</span><span class="provider-identity"><span class="provider-name">${e(p.label||p.id)}</span><span class="provider-model">${e(p.model||p.deployment||'—')}</span></span><span class="provider-state ${cooling?'cooldown':working||reserved?'busy':disabled?'disabled':''}">${e(status)}</span></div>
        <div class="provider-routing" aria-label="${e(t('Globalne zajęcie API'))}">${roles}<span class="provider-in-flight" data-provider-in-flight="${e(p.id)}">${e(t('Żądania · {count}',{count:full(p.in_flight||0)}))}</span></div>${assignedProjects.length?`<p class="provider-projects" title="${e(assignedProjects.join(' · '))}">${e(assignedProjects.join(' · '))}</p>`:''}
        <div class="provider-bottom"><span><b>${full(usage.generations||0)}</b> ${e(t('odpowiedzi'))}</span><span><b>${compact(usage.total_tokens)}</b> ${e(t('tokenów'))}</span><span class="expand-label">${e(t('Szczegóły ⌄'))}</span></div>${budgetHtml}</summary>
        <div class="provider-detail">${assignmentRows?`<p class="assignment-title">${e(t('Przypisania we wszystkich projektach'))}</p><ul class="provider-assignments" data-assignment-list="${e(p.id)}">${assignmentRows}</ul>`:''}<dl><div><dt>${e(t('Wejście'))}</dt><dd>${compact(usage.input_tokens)}</dd></div><div><dt>${e(t('Wyjście'))}</dt><dd>${compact(usage.output_tokens)}</dd></div><div><dt>${e(t('Cache (w wejściu)'))}</dt><dd>${compact(usage.cached_tokens)}</dd></div><div><dt>${e(t('Rozumowanie (w wyjściu)'))}</dt><dd>${compact(usage.reasoning_tokens)}</dd></div><div><dt>${e(t('Wszystkie próby'))}</dt><dd>${full(usage.attempts||0)}</dd></div><div><dt>${e(t('Limity od startu proxy'))}</dt><dd>${full(p.rate_limit_events||0)}</dd></div><div><dt>${e(t('Próby bez usage'))}</dt><dd>${full(usage.unreported||0)}</dd></div><div><dt>${e(t('Przerwa domyślna'))}</dt><dd>${full(p.cooldown_seconds??60)} s</dd></div></dl><p class="endpoint-preview">${e(p.base_url||'')}</p><button type="button" class="small-button" data-edit-api="${e(p.id)}">${icon('settings')} ${e(t('Edytuj API / klucz'))}</button></div>
        </details><button type="button" class="select-api" data-select-api="${e(p.id)}" ${cannotSelect?'disabled':''}>${icon(p.id===primary?'route':'arrow-right')} ${e(t(p.id===primary?'Preferowane w projekcie':'Wybierz dla projektu'))}</button></article>`;
    }).join('')+`<button type="button" class="add-api-card" id="add-api-button" aria-label="${e(t('Dodaj nowe API'))}" title="${e(t('Dodaj nowe API'))}">${icon('plus')}<span>${e(t('Dodaj'))}<br>${e(t('nowe'))}</span></button>`;
    $('#provider-list').querySelectorAll('[data-assignment-list]').forEach(el=>el.scrollTop=scroll.get(el.dataset.assignmentList)||0);
    if(focusSelector)[...$('#provider-list').querySelectorAll('['+focusSelector+']')].find(el=>el.getAttribute(focusSelector)===focusId)?.focus({preventScroll:true});
  }

  function render() {
    const state=getState(); if(!state)return;
    if(state.models?.length)models=state.models;
    const list=providers();
    restoreProject(state,list);
    const chosen=$('#model-picker').value;
    const names=[...new Set(list.map(p=>p.model||p.deployment).filter(Boolean))];
    if(names.length)options($('#model-picker'),names.map(value=>({value,label:value})),chosen);
    const task=getTask();
    if(task?.id!==lastTask){
      lastTask=task?.id;
      $('#submit-error').hidden=true;
    }
    updateSelection();renderProviders();
    const routes=state.proxy?.routes||[];
    if(task&&!isHistory()){
      const live=routes.filter(r=>r.task_id===task.id||r.id===task.id||r.id?.startsWith(task.id+'-')).filter(r=>r.last_provider&&(!task.run_id||!r.run_id||r.run_id===task.run_id));
      const current=live.map(r=>list.find(p=>p.id===r.last_provider)?.label||r.last_provider);
      if(current.length) setText('#task-token-count',(task.tokens?t('{count} tokenów · ',{count:full(task.tokens.totalTokens)}):'')+t('Ostatnie API: {providers}',{providers:[...new Set(current)].join(', ')}));
      $('#task-token-count').hidden=!task.tokens&&!current.length;
      if(task.working_copies && task.experiment?.phase!=='completed')setText('#files-count',t('{count} plików w kopiach roboczych',{count:full(task.files||0)}));
    }
    setText('#footer-status', state.proxy?t('{count} API · {requests} żądań od startu proxy',{count:full(list.length),requests:full(state.proxy.stats?.requests||0)}):t('Proxy jest niedostępne'));
  }

  async function selectApi(id) {
    const p=providers().find(p=>p.id===id);if(!p||!p.configured||p.enabled===false||routingTask)return;
    const task=getTask(), project=selectionProject;
    if(active(task)){
      if(task.mode==='experimental'){notify(t('W eksperymencie wybór API jest zapisany w rolach planisty i wykonawców.'),true);return}
      if(task.state==='finalizing')return;
      if((p.model||p.deployment)!==(task.model||$('#model-picker').value)){notify(t('W trakcie zadania wybierz API zgodne z jego modelem.'),true);return}
      routingTask=task.id;updateSelection();renderProviders();
      try{await api('/tasks/'+task.id+'/route',{provider_id:id})}catch(err){notify(err.message,true);return}
      finally{routingTask=null;updateSelection();renderProviders()}
      notify(t('Kolejne żądanie tego czatu trafi do wybranego API.'));
      if(selectionProject!==project||getTask()?.id!==task.id)return;
    }
    if($('#model-picker').value!==(p.model||p.deployment)){ $('#model-picker').value=p.model||p.deployment;selected=[]; }
    primary=id;
    if(!selected.length)selected=compatible().map(p=>p.id);
    updateSelection();renderProviders();
    if(!active(task))$('#prompt').focus();
  }
  $('#provider-list').addEventListener('click',event=>{
    const add=event.target.closest('#add-api-button'), edit=event.target.closest('[data-edit-api]'), use=event.target.closest('[data-select-api]');
    if(add)openProvider();else if(edit)openProvider(edit.dataset.editApi);else if(use)selectApi(use.dataset.selectApi);
  });
  $('#primary-api').addEventListener('change',event=>selectApi(event.target.value));
  $('#model-picker').addEventListener('change',()=>{selected=compatible().map(p=>p.id);primary=selected[0]||'';updateSelection();renderProviders()});
  $('#api-cohort').addEventListener('change',event=>{const id=event.target.dataset.cohort;if(!id)return;selected=event.target.checked?[...selected,id]:selected.filter(v=>v!==id);updateSelection()});
  for(const id of ['#effort','#experimental','#permission-mode'])$(id).addEventListener('change',updateSelection);

  async function loadKnown(){const result=await api('/providers');known=result.providers||[];render()}
  function providerDialogLabels(){
    setText('#provider-dialog-title',t(editing?'Ustawienia API':'Dodaj nowe API'));
    $('#api-key').dataset.i18nPlaceholder=editing?'Pozostaw puste, aby zachować klucz':'Wklej klucz API';
    $('#api-key').placeholder=t($('#api-key').dataset.i18nPlaceholder);
    setText('#delete-provider',t(deleting?'Kliknij ponownie, aby usunąć API i klucz':'Usuń API'));
  }
  async function openProvider(id=null){
    try{await loadKnown();editing=id;deleting=false;const p=known.find(p=>p.id===id)||{};
      providerDialogLabels();
      $('#api-label').value=p.label||'';$('#api-endpoint').value=p.base_url||'';$('#api-model').value=p.model||'gpt-6-astra';$('#api-key').value='';$('#api-key').required=!id;
      $('#api-auth').value=p.auth_type||'api-key';$('#api-version').value=p.api_version||'';$('#api-cooldown').value=p.cooldown_seconds??60;$('#api-enabled').checked=p.enabled!==false;
      $('#api-tpm').value=p.tokens_per_minute??1000000;$('#api-tpm-soft').value=p.soft_tokens_per_minute??900000;$('#api-tpm-hard').value=p.hard_tokens_per_minute??950000;
      $('#delete-provider').hidden=!id;setText('#provider-error','');$('#provider-dialog').showModal();
    }catch(err){notify(err.message,true)}
  }
  $('#provider-form').addEventListener('submit',async event=>{
    event.preventDefault();$('#save-provider').disabled=true;setText('#provider-error','');
    try{
      const limits={tokens_per_minute:Number($('#api-tpm').value),soft_tokens_per_minute:Number($('#api-tpm-soft').value),hard_tokens_per_minute:Number($('#api-tpm-hard').value)};
      if(!Object.values(limits).every(v=>Number.isSafeInteger(v)&&v>0&&v<=1000000000)||limits.soft_tokens_per_minute>=limits.hard_tokens_per_minute||limits.hard_tokens_per_minute>limits.tokens_per_minute)throw new Error(t('Limity muszą spełniać: 0 < próg miękki < próg twardy ≤ limit API ≤ 1 000 000 000.'));
      const value=await api('/providers',{id:editing,label:$('#api-label').value,base_url:$('#api-endpoint').value,deployment:$('#api-model').value,key:$('#api-key').value.trim(),auth_type:$('#api-auth').value,api_version:$('#api-version').value,cooldown_seconds:Number($('#api-cooldown').value),enabled:$('#api-enabled').checked,...limits});$('#api-key').value='';$('#provider-dialog').close();notify(value.message);await loadKnown();await refresh()}
    catch(err){setText('#provider-error',err.message)}finally{$('#save-provider').disabled=false}
  });
  $('#delete-provider').addEventListener('click',async()=>{
    if(!deleting){deleting=true;providerDialogLabels();return}
    try{await api('/providers',{id:editing,delete:true});$('#api-key').value='';$('#provider-dialog').close();await loadKnown();await refresh();notify(t('Usunięto API. Historia zużycia została zachowana.'))}catch(err){setText('#provider-error',err.message)}
  });
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>$('#'+button.dataset.close).close()));
  $('#provider-dialog').addEventListener('close',()=>{$('#api-key').value=''});

  async function loadAgents(generation=settingsGeneration){
    const project=settingsProject;if(!project)throw new Error(t('Najpierw wybierz projekt.'));
    const result=await api('/projects/'+project.id+'/agents');if(generation!==settingsGeneration||project.id!==settingsProject?.id)return;agentsVersion=result.version;$('#agents-editor').value=result.content;
    agentsFile={path:result.path,exists:result.exists};
    setText('#agents-file-path',result.path+(result.exists?'':t(' · nowy plik')));
  }
  async function loadSkills(generation=settingsGeneration){
    skillItems=[];if(!settingsProject){setText('#skills-list',t('Wybierz projekt.'));return}
    const project=settingsProject;const result=await api('/skills?'+new URLSearchParams({project_id:project.id}));if(generation!==settingsGeneration||project.id!==settingsProject?.id)return;skillItems=result.skills||[];
    textLabels.delete('#skills-list');
    $('#skills-list').innerHTML=skillItems.map((s,i)=>`<label class="skill-item"><input type="checkbox" data-skill-index="${i}" ${s.enabled?'checked':''}><span><strong>${e(s.name)}</strong><small>${e(s.description)}</small></span></label>`).join('')||`<p data-i18n="Brak skills w tym projekcie.">${e(t('Brak skills w tym projekcie.'))}</p>`;
    if(result.errors?.length)$('#skills-list').insertAdjacentHTML('afterbegin',`<p class="form-error" data-i18n="Część skills nie została wczytana przez Codex.">${e(t('Część skills nie została wczytana przez Codex.'))}</p>`);
  }
  $('#settings-button').addEventListener('click',async()=>{
    const generation=++settingsGeneration;settingsReady=false;settingsInputs(false);
    settingsProject=getProject();agentsVersion=null;agentsFile=null;skillItems=[];setText('#settings-error','');$('#settings-dialog').showModal();
    try{
      const config=await api('/settings');if(generation!==settingsGeneration)return;settingsBaseline=config;
      for(const [key,selector] of Object.entries(settingsFields))$(selector).value=Object.hasOwn(settingsDraft,key)?settingsDraft[key]:config[key]??'';
      settingsReady=true;settingsInputs(true);saveSettingsDraft();
      const results=await Promise.allSettled([loadAgents(generation),loadSkills(generation)]);
      if(generation===settingsGeneration)results.forEach((r,i)=>{if(r.status==='rejected')setText(i?'#skills-list':'#agents-file-path',r.reason.message)});
    }catch(err){if(generation===settingsGeneration)setText('#settings-error',err.message)}
  });
  $('#settings-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!settingsReady||settingsSaving)return;setText('#settings-error','');settingsSaving=true;$('#save-settings').disabled=true;
    const config=settingsValue(),generation=settingsGeneration,project=settingsProject;
    const skills=[...$('#skills-list').querySelectorAll('[data-skill-index]')].map(el=>({path:skillItems[Number(el.dataset.skillIndex)].path,enabled:el.checked}));
    try{
      await api('/settings',config);
      if(skills.length)await api('/skills',{project_id:project.id,skills});
      if(generation===settingsGeneration){settingsBaseline=config;saveSettingsDraft();if(!Object.keys(settingsDraft).length)$('#settings-dialog').close();}
      if(!active(getTask())&&project?.id===getProject()?.id){$('#permission-mode').value=config.permission_mode;$('#effort').value=config.effort;updateSelection()}await refresh();notify(t('Zapisano ustawienia zespołu.'));
    }catch(err){if(generation===settingsGeneration)setText('#settings-error',err.message)}finally{settingsSaving=false;$('#save-settings').disabled=!settingsReady}
  });
  $('#settings-dialog').addEventListener('close',()=>{saveSettingsDraft();settingsGeneration++;settingsReady=false;});
  $('#save-agents').addEventListener('click',async()=>{try{if(agentsVersion===null)throw new Error(t('Najpierw wczytaj AGENTS.md.'));const result=await api('/projects/'+settingsProject.id+'/agents',{content:$('#agents-editor').value,version:agentsVersion});agentsVersion=result.version;agentsFile={path:result.path,exists:true};setText('#agents-file-path',result.path);setText('#settings-error','');notify(t('Zapisano AGENTS.md.'))}catch(err){setText('#settings-error',err.message)}});
  $('#reload-agents').addEventListener('click',()=>loadAgents().catch(err=>setText('#settings-error',err.message)));
  const settingsTabs=[...document.querySelectorAll('[data-settings-tab]')];
  function settingsTab(value){settingsTabs.forEach(b=>{b.setAttribute('aria-selected',String(b.dataset.settingsTab===value));b.tabIndex=b.dataset.settingsTab===value?0:-1});document.querySelectorAll('[data-settings-pane]').forEach(p=>p.hidden=p.dataset.settingsPane!==value);$('#save-settings').hidden=value==='agentsmd'}
  settingsTabs.forEach(b=>{b.addEventListener('click',()=>settingsTab(b.dataset.settingsTab));b.addEventListener('keydown',ev=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(ev.key))return;ev.preventDefault();let i=ev.key==='Home'?0:ev.key==='End'?settingsTabs.length-1:(settingsTabs.indexOf(b)+(ev.key==='ArrowRight'?1:-1)+settingsTabs.length)%settingsTabs.length;settingsTab(settingsTabs[i].dataset.settingsTab);settingsTabs[i].focus()})});settingsTab('prompts');

  let scene=null, loading3d=null;
  $('#polcio-button').addEventListener('click',()=>{$('#polcio-section').open=true;$('#polcio-section').scrollIntoView({behavior:'smooth',block:'start'});$('#sidebar').classList.remove('open')});
  $('#polcio-section').addEventListener('toggle',async()=>{
    if($('#polcio-section').open&&!loading3d){loading3d=import('./polcio.js').then(m=>m.mountPolcio($('#polcio-canvas'),$('#polcio-motion'))).then(value=>{scene=value;scene.setVisible($('#polcio-section').open)}).catch(()=>{setText('#polcio-motion',t('3D niedostępne w tej przeglądarce'));$('#polcio-motion').disabled=true})}
    scene?.setVisible($('#polcio-section').open);
  });
  onLanguageChange(()=>{
    for(const [id,source] of textLabels)$(id).textContent=translateMessage(source);
    if(agentsFile)setText('#agents-file-path',agentsFile.path+(agentsFile.exists?'':t(' · nowy plik')));
    providerDialogLabels();
    render();
  });
  Promise.allSettled([loadKnown(),api('/capabilities').then(r=>{models=r.models||[];render()})]);
  return {render, save, taskOptions(){render();updateSelection();if(!selected.length)throw new Error(t('Wybierz skonfigurowane API.'));if($('#experimental').checked&&selected.length!==3)throw new Error(t('Eksperyment wymaga dokładnie trzech zaznaczonych API.'));return {mode:$('#experimental').checked?'experimental':'standard',effort:$('#effort').value,permission_mode:$('#permission-mode').value,api_ids:[...selected]}}};
}
