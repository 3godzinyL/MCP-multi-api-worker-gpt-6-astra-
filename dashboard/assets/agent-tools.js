import {t, onLanguageChange} from './i18n.js';

// Optional browser standard. Normal browsers do not need this capability.
export function registerPanelTools({getState, refresh, startTask}) {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  let lifecycle = new AbortController();
  const definitions = () => [{
    name: 'get_3api_status', title: t('Status 3API'),
    description: t('Odczytaj identyfikatory projektów, rzeczywiste zużycie dostawców i podsumowania zadań programistycznych.'),
    inputSchema: {type: 'object', properties: {}, additionalProperties: false},
    annotations: {readOnlyHint: true, untrustedContentHint: true},
    async execute(input) {
      if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error(t('Oczekiwano pustego obiektu.'));
      await refresh();
      const state = getState();
      if (!state) throw new Error(t('Panel nie jest połączony.'));
      return {projects: state.projects, providers: state.proxy?.providers, metrics: state.metrics,
        active_agents: state.active_agents,
        tasks: state.tasks.map(t => ({id: t.id, project_id: t.project_id, title: t.title,
          state: t.state, files: t.files, added: t.added, removed: t.removed}))};
    }
  }, {
    name: 'start_3api_task', title: t('Uruchom zadanie Codex'),
    description: t('Uruchom rzeczywiste zadanie programistyczne w skonfigurowanym projekcie. Zużywa tokeny API i może zmieniać pliki projektu. Zwraca identyfikator zadania; odczytaj status, aby śledzić ukończenie.'),
    inputSchema: {type: 'object', properties: {project_id: {type: 'string'}, prompt: {type: 'string', minLength: 1, maxLength: 32000},
      effort: {type: 'string', enum: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']},
      mode: {type:'string',enum:['standard','experimental']}, permission_mode:{type:'string',enum:['yolo','approval']},
      api_ids:{type:'array',items:{type:'string'},uniqueItems:true,minItems:1,maxItems:32}}, required: ['project_id', 'prompt'], additionalProperties: false},
    annotations: {readOnlyHint: false, untrustedContentHint: true},
    async execute(input) {
      if (!input || typeof input !== 'object' || typeof input.project_id !== 'string' ||
          typeof input.prompt !== 'string' || !input.prompt.trim() || input.prompt.length > 32000 ||
          Object.keys(input).some(k => !['project_id', 'prompt', 'effort','mode','permission_mode','api_ids'].includes(k)) ||
          (input.effort && !['low', 'medium', 'high', 'xhigh','max','ultra'].includes(input.effort))) throw new Error(t('Nieprawidłowe zadanie.'));
      await refresh();
      if (!getState()?.projects.some(p => p.id === input.project_id)) throw new Error(t('Nieznany projekt.'));
      return await startTask({...input, effort: input.effort || getState()?.defaults?.effort || 'max'});
    }
  }];
  function register() {
    for (const tool of definitions()) {
      try { Promise.resolve(context.registerTool(tool, {signal: lifecycle.signal})).catch(() => {}); } catch {}
    }
  }
  const unsubscribe = onLanguageChange(() => {
    lifecycle.abort();
    lifecycle = new AbortController();
    for (const name of ['get_3api_status', 'start_3api_task']) {
      try { context.unregisterTool?.(name); } catch {}
    }
    register();
  });
  window.addEventListener('pagehide', () => { unsubscribe(); lifecycle.abort(); }, {once: true});
  register();
}
