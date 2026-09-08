// Browser-only preferences. Conversation and run records always come from the server.
const key = '3api-workspace-v2';
function read() {
  try { const value=JSON.parse(localStorage.getItem(key));return value&&typeof value==='object'&&!Array.isArray(value)?value:{}; } catch { return {}; }
}
export function createWorkspaceMemory() {
  const saved = read();
  if (!saved.projects || typeof saved.projects !== 'object'||Array.isArray(saved.projects)) saved.projects = {};
  const persist = () => { try { localStorage.setItem(key, JSON.stringify(saved)); } catch {} };
  const project = id => {
    if(!Object.hasOwn(saved.projects,id)||!saved.projects[id]||typeof saved.projects[id]!=='object')
      Object.defineProperty(saved.projects,id,{value:{taskId:'',runId:'',drafts:{},pending:{}},writable:true,enumerable:true,configurable:true});
    const value=saved.projects[id];
    for(const key of ['drafts','pending'])if(!value[key]||typeof value[key]!=='object'||Array.isArray(value[key]))value[key]={};
    return value;
  };
  return {
    selectedProject() { return saved.projectId || ''; },
    selection(id) { const value=project(id); return {taskId:value.taskId || '',runId:value.runId || ''}; },
    select(projectId, taskId='', runId='') {
      saved.projectId=projectId;
      Object.assign(project(projectId), {taskId,runId}); persist();
    },
    draft(projectId, taskId='') { return project(projectId).drafts?.[taskId || 'new'] || ''; },
    saveDraft(projectId, taskId, text) {
      if (!projectId) return;
      const value=project(projectId); value.drafts ||= {};
      value.drafts[taskId || 'new']=text; persist();
    },
    request(projectId, taskId, body) {
      const value=project(projectId); value.pending ||= {};
      const slot=taskId || 'new', signature=JSON.stringify(body), previous=value.pending[slot];
      if (previous?.signature===signature) return previous.id;
      const id=globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      value.pending[slot]={id,signature}; persist(); return id;
    },
    accepted(projectId, taskId) {
      const value=project(projectId); delete value.pending?.[taskId || 'new']; persist();
    }
  };
}

export const activeStates = new Set(['queued','starting','pendingInit','running','awaiting_input','stopping','finalizing']);
export function mergeTask(previous, incoming) {
  if(typeof previous?.updated==='number'&&typeof incoming.updated==='number'&&previous.updated>incoming.updated)return previous;
  const merged={...previous,...incoming};
  // A continuation starts a fresh baseline; the permanent chat keeps its messages.
  if (previous?.run_id && incoming.run_id && previous.run_id!==incoming.run_id) {
    for (const key of ['changes','api_events','changes_revision','scan_error','scan_skipped']) {
      if (!(key in incoming)) delete merged[key];
    }
    for (const key of ['files','added','removed','touched_files']) {
      if (!(key in incoming)) merged[key]=0;
    }
  }
  return merged;
}

export function mergeState(previous, incoming) {
  const tasks=new Map((previous?.tasks || []).map(task=>[task.id,task]));
  for (const task of incoming.tasks || []) tasks.set(task.id,mergeTask(tasks.get(task.id),task));
  return {...previous,...incoming,tasks:[...tasks.values()]};
}

export function matchesSelection(response, selection) {
  for (const field of ['project_id','task_id','run_id']) {
    const key='requested_'+field;
    if (Object.hasOwn(response,key) && String(response[key] || '')!==String(selection[field] || '')) return false;
  }
  return true;
}
