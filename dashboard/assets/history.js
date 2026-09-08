import {language, locale, onLanguageChange, t} from './i18n.js';

const messages = {
  'Historia uruchomień': 'Run history',
  'Cały projekt': 'Whole project',
  'Ten czat': 'This chat',
  'Odśwież historię': 'Refresh history',
  'Pokaż kolejne': 'Show more',
  'Wybierz projekt, aby zobaczyć historię.': 'Select a project to view its history.',
  'W tym projekcie nie ma jeszcze uruchomień.': 'This project has no runs yet.',
  'Ten czat nie ma jeszcze uruchomień.': 'This chat has no runs yet.',
  'Wybierz czat, aby zobaczyć jego uruchomienia.': 'Select a chat to view its runs.',
  'Wczytywanie historii…': 'Loading history…',
  'Nie udało się odświeżyć historii. Zapisane wyniki nadal są widoczne.': 'Could not refresh history. Previously loaded results remain visible.',
  'Nie udało się wczytać historii. Spróbuj ponownie.': 'Could not load history. Try again.',
  'Suma uruchomień': 'Sum of runs',
  'Zmiany w projekcie': 'Project changes',
  'uruchomień': 'runs',
  'zmian plików': 'file changes',
  'łączny czas': 'total time',
  'Suma różnic od początku każdego uruchomienia. Ten sam plik może wystąpić kilka razy.': 'Sum of changes from each run’s baseline. The same file may appear more than once.',
  'Wczytano {count} uruchomień; podsumowanie jest jeszcze niepełne.': 'Loaded {count} runs; the summary is not complete yet.',
  'Część uruchomień nie ma dawnych pomiarów. Suma obejmuje dostępne wartości.': 'Some older run measurements are unavailable. Totals include the available values.',
  'Uruchomienie {id}': 'Run {id}',
  'Czat {id}': 'Chat {id}',
  'Pliki': 'Files',
  'Agenci': 'Agents',
  'Czas': 'Duration',
  'Zaobserwowane pliki': 'Observed files',
  'Obejmuje także zaobserwowane edycje, które później cofnięto.': 'Includes observed edits that were later reverted.',
  'Brak pomiaru': 'Not measured',
  'Brak zapisanej daty': 'Date unavailable',
  'API nie zostało jeszcze przypisane': 'No API assigned yet',
  'Brak zapisanych API': 'API history unavailable',
  'Tokeny raportowane przez API': 'Tokens reported by the API',
  '{count} tokenów': '{count} tokens',
  'W kolejce': 'Queued',
  'Przygotowanie': 'Preparing',
  'W pracy': 'Working',
  'Oczekuje na odpowiedź': 'Waiting for input',
  'Oczekuje na zgodę': 'Waiting for approval',
  'Oczekiwanie': 'Waiting',
  'Końcowy zapis': 'Finalizing',
  'Zatrzymywanie': 'Stopping',
  'Ukończono': 'Completed',
  'Błąd': 'Failed',
  'Przerwano': 'Interrupted',
  'Anulowano': 'Cancelled',
  'Gotowy': 'Ready',
  'Nieznany stan': 'Unknown state',
  'Trwa skanowanie zmian': 'Scanning changes',
  'Częściowy pomiar zmian': 'Partial change measurement',
  'Nie udało się zmierzyć wszystkich zmian': 'Could not measure all changes',
  'Pomiar zmian niedostępny': 'Change measurement unavailable',
  'Stan skanowania: {state}': 'Scan status: {state}',
  'Wybrane uruchomienie': 'Selected run',
};

function text(source, params = {}) {
  const value = language() === 'en' ? (messages[source] ?? t(source)) : source;
  return value.replace(/\{(\w+)\}/g, (token, key) => Object.hasOwn(params, key) ? String(params[key]) : token);
}

const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
const identity = value => typeof value === 'object' && value !== null ? String(value.id ?? '') : String(value ?? '');
const measurement = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const count = value => Array.isArray(value) ? value.length : measurement(value);
const full = value => measurement(value) === null ? '—' : value.toLocaleString(locale());
const shortId = value => String(value ?? '').slice(0, 8);
const pageSize = 12;
const activeStates = new Set(['queued', 'pending', 'starting', 'preparing', 'running', 'working', 'executing', 'awaiting_input', 'awaiting_approval', 'waiting', 'finalizing', 'stopping']);

function duration(value) {
  const seconds = measurement(value);
  if (seconds === null) return '—';
  const total = Math.floor(seconds);
  if (total < 60) return `${total} s`;
  if (total < 3600) return `${Math.floor(total / 60)} min ${total % 60} s`;
  return `${Math.floor(total / 3600)} h ${Math.floor(total % 3600 / 60)} min`;
}

function runDate(value) {
  if (measurement(value) === null) return {label: text('Brak zapisanej daty'), iso: ''};
  const date = new Date(value * 1000);
  if (Number.isNaN(date.valueOf())) return {label: text('Brak zapisanej daty'), iso: ''};
  return {label: date.toLocaleString(locale(), {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}), iso: date.toISOString()};
}

function status(state) {
  const labels = {
    queued: 'W kolejce', pending: 'W kolejce', preparing: 'Przygotowanie', starting: 'Przygotowanie',
    running: 'W pracy', working: 'W pracy', executing: 'W pracy', awaiting_input: 'Oczekuje na odpowiedź',
    awaiting_approval: 'Oczekuje na zgodę', waiting: 'Oczekiwanie', finalizing: 'Końcowy zapis',
    stopping: 'Zatrzymywanie', completed: 'Ukończono', failed: 'Błąd', error: 'Błąd',
    interrupted: 'Przerwano', cancelled: 'Anulowano', canceled: 'Anulowano', stopped: 'Przerwano', ready: 'Gotowy',
  };
  if (state === 'completed') return {label: text(labels[state]), kind: 'completed', symbol: '✓'};
  if (['failed', 'error'].includes(state)) return {label: text(labels[state]), kind: 'failed', symbol: '×'};
  if (['interrupted', 'cancelled', 'canceled', 'stopped'].includes(state)) return {label: text(labels[state]), kind: 'interrupted', symbol: '■'};
  if (['awaiting_input', 'awaiting_approval', 'waiting'].includes(state)) return {label: text(labels[state]), kind: 'waiting', symbol: 'Ⅱ'};
  if (state === 'finalizing') return {label: text(labels[state]), kind: 'finalizing', symbol: ''};
  if (activeStates.has(state)) return {label: text(labels[state]), kind: state === 'stopping' ? 'stopping' : 'working', symbol: ''};
  return {label: text(labels[state] || 'Nieznany stan'), kind: 'unknown', symbol: '·'};
}

function scanNote(value) {
  const state = typeof value === 'object' && value !== null ? value.state || value.status : value;
  if (!state || ['complete', 'completed', 'ok', 'ready', 'clean'].includes(state)) return '';
  if (['running', 'pending', 'scanning', 'in_progress'].includes(state)) return text('Trwa skanowanie zmian');
  if (['partial', 'skipped', 'limited'].includes(state)) return text('Częściowy pomiar zmian');
  if (['error', 'failed'].includes(state)) return text('Nie udało się zmierzyć wszystkich zmian');
  if (['unknown', 'unavailable', 'not_scanned', 'legacy'].includes(state)) return text('Pomiar zmian niedostępny');
  return text('Stan skanowania: {state}', {state});
}

function usageTokens(usage) {
  if (!usage || typeof usage !== 'object') return null;
  return measurement(usage.total_tokens) ?? measurement(usage.totalTokens);
}

/**
 * API paths are relative to /ui/api, just like app.js's api helper.
 * getProject/getTask may return an id, an object with id, or null.
 * init() mounts existing #run-history and #project-change-summary containers.
 * Call render() when selection changes and refresh() after state polling.
 */
export function createHistory({api, getProject, getTask, onSelectRun}) {
  const projects = new Map();
  let root = null;
  let summary = null;
  let currentProject = '';
  let currentTask = '';
  let selectedRun = '';
  let generation = 0;
  let disposed = false;
  let unsubscribe = null;

  function projectCache(id) {
    if (!projects.has(id)) projects.set(id, {
      runs: new Map(), loaded: false, complete: false, cursor: null, error: false,
      fetching: false, request: 0, fetchedAt: 0, scope: 'project', visible: pageSize, scroll: 0,
    });
    return projects.get(id);
  }

  function syncContext() {
    const project = identity(getProject());
    const task = identity(getTask());
    if (project !== currentProject) {
      if (currentProject && root) projectCache(currentProject).scroll = root.querySelector('.run-history-list')?.scrollTop || 0;
      currentProject = project;
      currentTask = task;
      selectedRun = '';
      generation += 1;
      if (root) root.dataset.project = project;
    } else if (task !== currentTask) {
      currentTask = task;
      if (currentProject && projectCache(currentProject).scope === 'task') {
        projectCache(currentProject).visible = pageSize;
        projectCache(currentProject).scroll = 0;
        const list = root?.querySelector('.run-history-list');
        if (list) list.scrollTop = 0;
      }
    }
  }

  function sortedRuns(cache) {
    return [...cache.runs.values()].sort((a, b) => (measurement(b.started_at) ?? -1) - (measurement(a.started_at) ?? -1) || String(b.id).localeCompare(String(a.id)));
  }

  function sum(runs, field) {
    const values = runs.map(run => count(run[field]));
    const measured = values.filter(value => value !== null);
    return {value: runs.length && !measured.length ? null : measured.reduce((total, value) => total + value, 0), missing: measured.length !== runs.length};
  }

  function renderSummary(cache) {
    if (!summary) return;
    summary.classList.add('project-change-summary');
    summary.setAttribute('aria-label', text('Zmiany w projekcie'));
    if (!currentProject) {
      summary.innerHTML = `<p class="history-empty">${escape(text('Wybierz projekt, aby zobaczyć historię.'))}</p>`;
      return;
    }
    const runs = sortedRuns(cache);
    const added = sum(runs, 'added');
    const removed = sum(runs, 'removed');
    const files = sum(runs, 'files');
    const elapsed = sum(runs, 'elapsed_seconds');
    const pending = !cache.loaded;
    const missing = [added, removed, files, elapsed].some(value => value.missing);
    summary.innerHTML = `<div class="project-change-heading"><div><span class="history-eyebrow">${escape(text('Suma uruchomień'))}</span><h2>${escape(text('Zmiany w projekcie'))}</h2></div><div class="project-change-lines"><strong class="added">${pending || added.value === null ? '—' : '+' + full(added.value)}</strong><strong class="removed">${pending || removed.value === null ? '—' : '−' + full(removed.value)}</strong></div></div>
      <dl class="project-change-metrics"><div><dt>${escape(text('uruchomień'))}</dt><dd>${pending ? '—' : full(runs.length)}</dd></div><div><dt>${escape(text('zmian plików'))}</dt><dd>${pending ? '—' : full(files.value)}</dd></div><div><dt>${escape(text('łączny czas'))}</dt><dd>${pending ? '—' : duration(elapsed.value)}</dd></div></dl>
      <p class="project-change-note">${escape(text('Suma różnic od początku każdego uruchomienia. Ten sam plik może wystąpić kilka razy.'))}</p>
      ${pending ? `<p class="history-hint">${escape(text('Wczytywanie historii…'))}</p>` : !cache.complete ? `<p class="history-hint">${escape(text('Wczytano {count} uruchomień; podsumowanie jest jeszcze niepełne.', {count: full(runs.length)}))}</p>` : ''}
      ${missing ? `<p class="history-hint">${escape(text('Część uruchomień nie ma dawnych pomiarów. Suma obejmuje dostępne wartości.'))}</p>` : ''}`;
  }

  function rowHtml(run) {
    const state = status(run.state);
    const date = runDate(run.started_at);
    const task = getTask();
    const taskTitle = run.task_title || (identity(task) === String(run.task_id) && typeof task === 'object' ? task?.title : '');
    const title = run.title || text('Uruchomienie {id}', {id: shortId(run.id)});
    const chat = taskTitle || text('Czat {id}', {id: shortId(run.task_id)});
    const apiIds = Array.isArray(run.api_ids) ? [...new Set(run.api_ids.map(identity).filter(Boolean))] : [];
    const note = scanNote(run.scan_status);
    const touched = count(run.touched_files);
    const tokens = usageTokens(run.usage);
    const valueTitle = value => measurement(value) === null ? ` title="${escape(text('Brak pomiaru'))}"` : '';
    return `<span class="history-run-heading"><span class="history-run-state ${state.kind}" data-task-status="${escape(run.state || 'unknown')}"><i aria-hidden="true">${state.symbol}</i><span>${escape(state.label)}</span></span><time${date.iso ? ` datetime="${date.iso}"` : ''}>${escape(date.label)}</time></span>
      <strong class="history-run-title">${escape(title)}</strong><span class="history-run-chat">${escape(chat)}</span>
      <span class="history-run-metrics"><span${valueTitle(run.elapsed_seconds)}><small>${escape(text('Czas'))}</small><b>${duration(run.elapsed_seconds)}</b></span><span${valueTitle(run.agents_count)}><small>${escape(text('Agenci'))}</small><b>${full(run.agents_count)}</b></span><span${valueTitle(run.files)}><small>${escape(text('Pliki'))}</small><b>${full(run.files)}</b></span><span class="history-run-lines"><b class="added"${valueTitle(run.added)}>${measurement(run.added) === null ? '—' : '+' + full(run.added)}</b><b class="removed"${valueTitle(run.removed)}>${measurement(run.removed) === null ? '—' : '−' + full(run.removed)}</b></span></span>
      <span class="history-run-apis">${apiIds.length ? apiIds.map(id => `<span>${escape(id)}</span>`).join('') : `<small>${escape(text(activeStates.has(run.state) ? 'API nie zostało jeszcze przypisane' : 'Brak zapisanych API'))}</small>`}</span>
      <span class="history-run-observed" title="${escape(text('Obejmuje także zaobserwowane edycje, które później cofnięto.'))}">${escape(text('Zaobserwowane pliki'))}: <b${valueTitle(touched)}>${full(touched)}</b>${tokens === null ? '' : `<span title="${escape(text('Tokeny raportowane przez API'))}"> · ${escape(text('{count} tokenów', {count: full(tokens)}))}</span>`}</span>
      ${note ? `<span class="history-run-scan">${escape(note)}</span>` : ''}`;
  }

  function renderRows(cache) {
    const list = root.querySelector('.run-history-list');
    const sameProject = list.dataset.project === currentProject;
    const previousScroll = sameProject ? list.scrollTop : cache.scroll;
    const previousRows = new Map([...list.querySelectorAll('[data-run-id]')].map(row => [row.dataset.runId, row]));
    const runs = sortedRuns(cache).filter(run => cache.scope === 'project' || currentTask && String(run.task_id) === currentTask);
    const visible = runs.slice(0, cache.visible);
    const children = [];
    for (const run of visible) {
      const row = previousRows.get(String(run.id)) || document.createElement('button');
      const contents = rowHtml(run);
      row.type = 'button';
      row.className = 'history-run' + (selectedRun === String(run.id) ? ' selected' : '');
      row.dataset.runId = String(run.id);
      row.dataset.runState = String(run.state || 'unknown');
      row.setAttribute('aria-pressed', String(selectedRun === String(run.id)));
      if (row.innerHTML !== contents) row.innerHTML = contents;
      children.push(row);
    }
    if (!children.length) {
      const empty = document.createElement('p');
      empty.className = 'history-empty';
      empty.textContent = !currentProject ? text('Wybierz projekt, aby zobaczyć historię.')
        : cache.scope === 'task' && !currentTask ? text('Wybierz czat, aby zobaczyć jego uruchomienia.')
        : !cache.loaded ? text(cache.error ? 'Nie udało się wczytać historii. Spróbuj ponownie.' : 'Wczytywanie historii…')
        : text(cache.scope === 'task' ? 'Ten czat nie ma jeszcze uruchomień.' : 'W tym projekcie nie ma jeszcze uruchomień.');
      children.push(empty);
    }
    // Retain row elements and focused controls when polling changes another run.
    for (let index = 0; index < children.length; index += 1) {
      if (list.children[index] !== children[index]) list.insertBefore(children[index], list.children[index] || null);
    }
    while (list.children.length > children.length) list.lastElementChild.remove();
    list.dataset.project = currentProject;
    list.scrollTop = previousScroll;
    cache.scroll = list.scrollTop;
    root.querySelector('[data-history-more]').hidden = runs.length <= cache.visible;
    root.querySelector('.history-count').textContent = cache.loaded ? `${full(visible.length)} / ${full(runs.length)}${cache.complete ? '' : '+'}` : '—';
  }

  function render() {
    if (disposed) return;
    syncContext();
    const cache = projectCache(currentProject);
    renderSummary(cache);
    if (!root) return;
    root.querySelector('[data-history-heading]').textContent = text('Historia uruchomień');
    root.querySelector('[data-history-scope="project"]').textContent = text('Cały projekt');
    root.querySelector('[data-history-scope="task"]').textContent = text('Ten czat');
    root.querySelector('[data-history-scope="task"]').disabled = !currentTask;
    for (const button of root.querySelectorAll('[data-history-scope]')) button.setAttribute('aria-pressed', String(button.dataset.historyScope === cache.scope));
    const refreshButton = root.querySelector('[data-history-refresh]');
    refreshButton.setAttribute('aria-label', text('Odśwież historię'));
    refreshButton.title = text('Odśwież historię');
    refreshButton.disabled = cache.fetching || !currentProject;
    root.querySelector('[data-history-more]').textContent = text('Pokaż kolejne');
    const notice = root.querySelector('.history-notice');
    notice.hidden = !cache.error;
    notice.textContent = text(cache.loaded ? 'Nie udało się odświeżyć historii. Zapisane wyniki nadal są widoczne.' : 'Nie udało się wczytać historii. Spróbuj ponownie.');
    root.querySelector('.history-loading').hidden = !cache.fetching;
    root.querySelector('.history-loading').textContent = text('Wczytywanie historii…');
    renderRows(cache);
  }

  async function refresh({force = false} = {}) {
    if (disposed) return;
    syncContext();
    render();
    const projectId = currentProject;
    if (!projectId) return;
    const cache = projectCache(projectId);
    if (cache.fetching || !force && cache.loaded && Date.now() - cache.fetchedAt < 4000) return;
    const context = generation;
    const request = ++cache.request;
    const valid = () => !disposed && generation === context && identity(getProject()) === projectId && cache.request === request;
    cache.fetching = true;
    cache.error = false;
    render();
    try {
      // Load the project, not only the selected chat. Read all pages once so the
      // project totals are complete; polling subsequently updates recent runs.
      let cursor = null;
      const seenCursors = new Set();
      do {
        const params = new URLSearchParams({limit: '100'});
        if (cursor !== null) params.set('cursor', cursor);
        const result = await api(`/projects/${encodeURIComponent(projectId)}/history?${params}`);
        if (!valid()) return;
        if (!result || !Array.isArray(result.items)) throw new Error('Invalid run history response');
        for (const run of result.items) {
          if (!run || !identity(run) || run.project_id && String(run.project_id) !== projectId) continue;
          const old = cache.runs.get(String(run.id));
          cache.runs.set(String(run.id), {...old, ...run});
        }
        cache.loaded = true;
        const next = result.next_cursor === undefined || result.next_cursor === null || result.next_cursor === '' ? null : String(result.next_cursor);
        if (next === null) cache.complete = true;
        // A complete cache still needs older pages when an older run is active.
        const seenIds = new Set(result.items.map(run => String(run?.id ?? '')));
        const olderActive = [...cache.runs.values()].some(run => activeStates.has(run.state) && !seenIds.has(String(run.id)));
        if (cache.complete && !olderActive) break;
        cursor = next;
        cache.cursor = cursor;
        render();
        if (cursor !== null) {
          if (seenCursors.has(cursor)) throw new Error('Repeated history cursor');
          seenCursors.add(cursor);
        }
      } while (cursor !== null);
      if (valid()) cache.fetchedAt = Date.now();
    } catch {
      if (valid()) cache.error = true;
    } finally {
      if (cache.request === request) cache.fetching = false;
      if (valid()) render();
    }
  }

  function handleClick(event) {
    const button = event.target.closest('button');
    if (!button || !root.contains(button)) return;
    const cache = projectCache(currentProject);
    if (button.dataset.runId) {
      const run = cache.runs.get(button.dataset.runId);
      if (run) onSelectRun?.(run);
    } else if (button.dataset.historyScope) {
      cache.scope = button.dataset.historyScope;
      cache.visible = pageSize;
      cache.scroll = 0;
      root.querySelector('.run-history-list').scrollTop = 0;
      render();
    } else if (button.hasAttribute('data-history-more')) {
      cache.visible += pageSize;
      render();
    } else if (button.hasAttribute('data-history-refresh')) refresh({force: true});
  }

  function init({history = document.querySelector('#run-history'), summary: summaryElement = document.querySelector('#project-change-summary')} = {}) {
    if (disposed) return;
    root?.removeEventListener('click', handleClick);
    root = history;
    summary = summaryElement;
    if (root) {
      root.classList.add('run-history-panel');
      root.innerHTML = '<div class="panel-header history-panel-header"><h2 data-history-heading></h2><button type="button" class="icon-button bordered" data-history-refresh><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17"/></svg></button></div><div class="history-scope" role="group"><button type="button" data-history-scope="project"></button><button type="button" data-history-scope="task"></button></div><p class="history-notice" role="status" hidden></p><div class="run-history-list"></div><p class="history-loading" role="status" hidden></p><div class="history-footer"><span class="history-count"></span><button type="button" class="small-button" data-history-more hidden></button></div>';
      root.addEventListener('click', handleClick);
    }
    if (!unsubscribe) unsubscribe = onLanguageChange(render);
    render();
  }

  function setSelectedRun(run) {
    syncContext();
    selectedRun = identity(run);
    render();
  }

  function dispose() {
    disposed = true;
    generation += 1;
    root?.removeEventListener('click', handleClick);
    unsubscribe?.();
    projects.clear();
  }

  return {init, render, refresh, setSelectedRun, dispose};
}
