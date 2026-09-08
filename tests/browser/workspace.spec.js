import {test, expect} from '@playwright/test';

// This suite checks real browser behavior against the deterministic HTTP demo.
// Rust, Python bootstrap, upstream routing and MCP are verified on the release.
const STUDIO = 'demo-project-0';
const WEBSITE = 'demo-project-1';
const AUTOMATIONS = 'demo-project-2';
const browserErrors = new WeakMap();

test.beforeEach(async ({context}) => {
  const errors = [];
  browserErrors.set(context, errors);
  const watch = page => page.on('pageerror', error => errors.push(error.message));
  context.pages().forEach(watch);
  context.on('page', watch);
});

test.afterEach(async ({context}) => {
  expect(browserErrors.get(context), 'No uncaught JavaScript errors').toEqual([]);
});

async function openWorkspace(page) {
  await page.goto('/ui/');
  await expect(page.locator('#workspace')).toBeVisible();
  await expect(page.locator('#token')).toHaveCount(0);
  await expect(page.locator('#provider-list')).toContainText('Demo');
}

async function post(page, path, data = {}) {
  const csrf = await page.locator('meta[name="csrf-token"]').getAttribute('content');
  const response = await page.request.post('/ui/api' + path, {
    data,
    headers: {'x-panel-csrf': csrf, Origin: new URL(page.url()).origin},
  });
  expect(response.ok(), `${path}: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function reset(page, scenario = 'showcase') {
  await openWorkspace(page);
  await post(page, '/__demo/reset', {scenario});
  await page.reload();
  await expect(page.locator('#provider-list')).toContainText('Demo');
  await expect(page.locator('#project-list [data-project]')).toHaveCount(3);
}

async function project(page, id) {
  await page.locator(`#project-list [data-project="${id}"]`).click();
  await expect(page.locator(`#project-list [data-project="${id}"]`)).toHaveClass(/selected/);
}

async function refresh(page) {
  const next = page.waitForResponse(response => response.url().includes('/ui/api/state?') && response.ok());
  await page.locator('#refresh-button').click();
  await next;
}

async function newChat(page) {
  const response = page.waitForResponse(response => response.url().endsWith('/ui/api/chats') && response.request().method() === 'POST');
  await page.locator('#new-task-button').click();
  const saved = await (await response).json();
  const id = saved.id || saved.task?.id;
  expect(id, 'New chat is persisted immediately').toBeTruthy();
  await expect(page.locator(`#task-list [data-task="${id}"]`)).toBeVisible();
  await expect(page.locator(`#task-list [data-task="${id}"]`)).toHaveClass(/selected/);
  return id;
}

async function start(page, prompt) {
  await expect(page.locator('#prompt')).toBeEnabled();
  await page.locator('#prompt').fill(prompt);
  await page.locator('#effort').selectOption('ultra');
  const response = page.waitForResponse(response => response.url().endsWith('/ui/api/tasks') && response.request().method() === 'POST');
  await page.locator('#run-button').click();
  const result = await (await response).json();
  expect(result.id).toBeTruthy();
  expect(result.run_id).toBeTruthy();
  await expect(page.locator('#prompt')).toHaveValue('');
  return result;
}

function taskStatus(page, taskId) {
  return page.locator(`#task-list [data-task="${taskId}"] [data-task-status]`);
}

function role(page, providerId, value) {
  return page.locator(`[data-provider="${providerId}"] .provider-routing [data-provider-role="${value}"]`);
}

test('opens without token, enforces the session boundary and preserves PL/EN on mobile', async ({page, context}) => {
  const denied = await page.request.get('/ui/api/state');
  expect(denied.status()).toBe(401);
  await reset(page);
  const response = await page.request.get('/ui/api/state');
  expect(response.ok()).toBeTruthy();
  const state = await response.json();
  expect(state.schema_version).toBe(2);
  expect(state.proxy.status).toBe('ready');
  expect(state.defaults.permission_mode).toBe('approval');
  const cookies = await context.cookies();
  expect(cookies.some(cookie => cookie.httpOnly && cookie.sameSite === 'Strict' && cookie.path === '/ui')).toBeTruthy();
  expect(await page.evaluate(() => document.cookie)).not.toContain('three_api_demo_session');
  const csrf = await page.locator('meta[name="csrf-token"]').getAttribute('content');
  const rejected = await page.request.post('/ui/api/settings', {data: {effort: 'high'}, headers: {Origin: 'http://evil.example', 'x-panel-csrf': csrf}});
  expect(rejected.status()).toBe(403);
  const missingCsrf = await page.request.post('/ui/api/settings', {data: {effort: 'high'}, headers: {Origin: new URL(page.url()).origin}});
  expect(missingCsrf.status()).toBe(403);
  const badHost = await page.request.get('/ui/api/state', {headers: {Host: 'evil.example'}});
  expect(badHost.status()).toBe(403);

  await expect(page.locator('html')).toHaveAttribute('lang', 'pl');
  await expect(page.locator('.experiment-toggle')).toContainText('Ultra → 2 × Ultra');
  await expect(role(page, 'demo1', 'main')).toHaveText('Główne · 1');
  await expect(role(page, 'demo2', 'auxiliary')).toHaveText('Pomocnicze · 1');
  await expect(role(page, 'demo3', 'free')).toHaveText('Wolne API');
  await page.locator('#language-picker').selectOption('en');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.locator('.experiment-toggle')).toContainText('Ultra → 2 × Ultra');
  await expect(page.locator('h1')).toContainText('projects');
  await expect(role(page, 'demo1', 'main')).toHaveText('Main · 1');
  await expect(role(page, 'demo2', 'auxiliary')).toHaveText('Auxiliary · 1');
  await expect(role(page, 'demo3', 'free')).toHaveText('Free API');
  await expect(page.locator('#run-history [data-history-heading]')).toHaveText('Run history');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await page.locator('#settings-button').click();
  await expect(page.locator('#settings-dialog')).toBeVisible();
  await page.locator('[data-settings-tab="limits"]').click();
  await expect(page.locator('[data-settings-pane="limits"]')).toContainText('token');
  await expect(page.locator('#save-settings')).toBeEnabled();
  await page.keyboard.press('Escape');
  await page.locator('#language-picker').selectOption('pl');
  await expect(page.locator('h1')).toContainText('projekty');

  await page.setViewportSize({width: 390, height: 844});
  await expect(page.locator('#sidebar')).toBeHidden();
  await page.locator('#menu-button').click();
  await expect(page.locator('#sidebar')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#sidebar')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await expect(page.locator('#prompt')).toBeVisible();
});

test('saves two empty chats immediately, isolates drafts and API preferences per project, reloads and reopens', async ({page, context}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  const first = await newChat(page);
  await page.locator('#prompt').fill('Studio first chat draft');
  await page.locator('#primary-api').selectOption('demo2');
  const second = await newChat(page);
  await expect(page.locator('#prompt')).toHaveValue('');
  await page.locator('#prompt').fill('Studio second chat draft');
  await expect(page.locator('#task-list [data-task]')).toHaveCount(2);
  const savedChats = await (await page.request.get(`/ui/api/projects/${STUDIO}/chats`)).json();
  expect(savedChats.items.map(chat => chat.id).sort()).toEqual([first, second].sort());

  await project(page, WEBSITE);
  const other = await newChat(page);
  await page.locator('#prompt').fill('Website independent draft');
  await page.locator('#primary-api').selectOption('demo3');
  await project(page, STUDIO);
  await expect(page.locator(`#task-list [data-task="${second}"]`)).toHaveClass(/selected/);
  await expect(page.locator('#prompt')).toHaveValue('Studio second chat draft');
  await expect(page.locator('#primary-api')).toHaveValue('demo2');
  await page.locator(`#task-list [data-task="${first}"]`).click();
  await expect(page.locator('#prompt')).toHaveValue('Studio first chat draft');
  await page.reload();
  await expect(page.locator(`#task-list [data-task="${first}"]`)).toHaveClass(/selected/);
  await expect(page.locator('#prompt')).toHaveValue('Studio first chat draft');
  await expect(page.locator('#primary-api')).toHaveValue('demo2');

  const reopened = await context.newPage();
  await page.close();
  await openWorkspace(reopened);
  await expect(reopened.locator(`#task-list [data-task="${first}"]`)).toHaveClass(/selected/);
  await expect(reopened.locator('#prompt')).toHaveValue('Studio first chat draft');
  await project(reopened, WEBSITE);
  await expect(reopened.locator(`#task-list [data-task="${other}"]`)).toHaveClass(/selected/);
  await expect(reopened.locator('#prompt')).toHaveValue('Website independent draft');
  await expect(reopened.locator('#primary-api')).toHaveValue('demo3');
});

test('keeps global main, auxiliary and free API roles visible across concurrent projects', async ({page}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  const first = await newChat(page);
  const studio = await start(page, 'Studio parallel implementation');
  await expect(taskStatus(page, first)).toHaveAttribute('data-task-status', 'running');
  await expect(page.locator(`#project-list [data-project="${STUDIO}"] [data-project-status]`)).toHaveAttribute('data-project-status', 'running');
  await expect(role(page, 'demo1', 'main')).toContainText('1');
  await expect(role(page, 'demo2', 'auxiliary')).toContainText('1');
  await expect(role(page, 'demo3', 'free')).toBeVisible();

  await project(page, WEBSITE);
  await expect(role(page, 'demo1', 'main')).toContainText('1');
  await expect(page.locator('[data-provider="demo1"]')).toContainText('Studio');
  await newChat(page);
  const website = await start(page, 'Website parallel review');
  await expect(role(page, 'demo3', 'main')).toContainText('1');
  await expect(role(page, 'demo2', 'auxiliary')).toContainText('2');
  await page.locator('[data-provider="demo2"] > summary').click();
  await expect(page.locator('[data-provider="demo2"] [data-provider-assignment]')).toHaveCount(2);
  await expect(page.locator(`[data-provider="demo2"] [data-provider-assignment][data-project-id="${STUDIO}"]`)).toHaveCount(1);
  await expect(page.locator(`[data-provider="demo2"] [data-provider-assignment][data-project-id="${WEBSITE}"]`)).toHaveCount(1);
  await project(page, AUTOMATIONS);
  await expect(role(page, 'demo1', 'main')).toContainText('1');
  await expect(role(page, 'demo2', 'auxiliary')).toContainText('2');
  await expect(role(page, 'demo3', 'main')).toContainText('1');
  await expect(page.locator('#active-agents')).toHaveText('4');

  await post(page, '/__demo/control', {run_id: studio.run_id, state: 'completed'});
  await refresh(page);
  await expect(role(page, 'demo1', 'free')).toBeVisible();
  await expect(role(page, 'demo2', 'auxiliary')).toContainText('1');
  await post(page, '/__demo/control', {run_id: website.run_id, state: 'interrupted'});
  await refresh(page);
  await expect(page.locator('.provider-routing [data-provider-role="free"]')).toHaveCount(3);
  await expect(page.locator('#active-agents')).toHaveText('0');
});

test('rejects late polling and retains messages, expanded diffs and scroll through summaries and network failure', async ({page}) => {
  await reset(page);
  await project(page, STUDIO);
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  await expect(page.locator('#conversation')).toContainText('Keep every chat');
  await page.locator('[data-tab="changes"]').click();
  const file = page.locator('#changes details').first();
  await file.locator('summary').click();
  await expect(file).toHaveAttribute('open', '');
  await page.locator('#changes').evaluate(el => { el.scrollTop = 95; });
  const originalScroll = await page.locator('#changes').evaluate(el => el.scrollTop);
  expect(originalScroll).toBeGreaterThan(0);
  const originalDiff = await file.locator('pre').textContent();
  await post(page, '/__demo/control', {summary_only: true});
  await refresh(page);
  await expect(file).toHaveAttribute('open', '');
  await expect(file.locator('pre')).toHaveText(originalDiff);
  await expect(page.locator('#conversation')).toContainText('Keep every chat');
  expect(await page.locator('#changes').evaluate(el => el.scrollTop)).toBeCloseTo(originalScroll, 0);

  let rejectState = true;
  await page.route('**/ui/api/state?*', async route => {
    if (rejectState) await route.abort('connectionfailed');
    else await route.continue();
  });
  const failed = page.waitForEvent('requestfailed', request => request.url().includes('/ui/api/state?'));
  await page.locator('#refresh-button').click();
  await failed;
  await expect(page.locator('#notice')).toBeVisible();
  await expect(file).toHaveAttribute('open', '');
  await expect(file.locator('pre')).toHaveText(originalDiff);
  rejectState = false;
  await page.unroute('**/ui/api/state?*');
  await post(page, '/__demo/control', {summary_only: false});

  // Capture a real response for Studio, switch twice while it is held, then
  // release it. The stale response must never replace Website's conversation.
  let held;
  let signalHeld;
  const captured = new Promise(resolve => { signalHeld = resolve; });
  await page.route('**/ui/api/state?*', async route => {
    const query = new URL(route.request().url()).searchParams;
    if (!held && query.get('task_id') === 'demo-chat-design') {
      const response = await route.fetch();
      let release;
      const wait = new Promise(resolve => { release = resolve; });
      held = {release};
      signalHeld();
      await wait;
      try { await route.fulfill({response}); } catch { /* AbortController may cancel the stale request. */ }
    } else await route.continue();
  });
  await page.locator('#refresh-button').click();
  await captured;
  await project(page, AUTOMATIONS);
  await project(page, WEBSITE);
  await page.locator('#task-list [data-task="demo-chat-website"]').click();
  await page.locator('[data-tab="conversation"]').click();
  held.release();
  await expect(page.locator('#breadcrumb-project')).toHaveText('Website');
  await expect(page.locator('#conversation')).toContainText('Review navigation');
  await expect(page.locator('#conversation')).not.toContainText('Keep every chat');
  await page.unroute('**/ui/api/state?*');
  await refresh(page);
  await expect(page.locator('#conversation')).toContainText('Review navigation');
});

test('stores separate run baselines and API history, keeps old runs after continuation and refresh', async ({page}) => {
  await reset(page);
  await project(page, STUDIO);
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  await expect(page.locator('#run-history [data-run-id]')).toHaveCount(2);
  await expect(page.locator('#project-change-summary')).toContainText('88');
  const older = page.locator('#run-history [data-run-id="demo-run-discovery"]');
  await expect(older).toContainText('demo1');
  await expect(older).toContainText('demo2');
  await expect(older).toContainText('2');
  await older.click();
  await expect(page.locator('#conversation')).toContainText('Plan a calm workspace');
  await expect(page.locator('#conversation')).not.toContainText('Keep every chat');
  await expect(page.locator('#history-live-button')).toBeVisible();
  await page.locator('[data-tab="changes"]').click();
  await expect(page.locator('#changes details')).toHaveCount(3);
  const original = await (await page.request.get('/ui/api/tasks/demo-chat-design/runs/demo-run-discovery')).json();
  expect(original).toMatchObject({state: 'completed', elapsed_seconds: 864, agents_count: 2, files: 3, added: 44, removed: 6});
  expect(original.api_events).toHaveLength(2);
  expect(original.api_events.map(event => event.role)).toEqual(['main', 'auxiliary']);
  await page.locator('[data-tab="api-events"]').click();
  await expect(page.locator('#api-events .run-api-event')).toHaveCount(2);
  await expect(page.locator('#api-events')).toContainText('demo1');
  await expect(page.locator('#api-events')).toContainText('Główne');
  await expect(page.locator('#api-events')).toContainText('Pomocnicze');
  await page.reload();
  await expect(page.locator('#history-live-button')).toBeVisible();
  await expect(older).toHaveAttribute('aria-pressed', 'true');
  await page.locator('[data-tab="conversation"]').click();
  await expect(page.locator('#conversation')).toContainText('Plan a calm workspace');
  await expect(page.locator('#conversation')).not.toContainText('Keep every chat');
  await page.locator('#history-live-button').click();
  const next = await start(page, 'Continue the workspace without erasing earlier runs');
  expect(next.id).toBe('demo-chat-design');
  expect(next.run_id).not.toBe('demo-run-design');
  await expect(page.locator('#lines-added')).toHaveText('+44');
  await expect(page.locator('#changes details')).toHaveCount(3);
  await post(page, '/__demo/control', {run_id: next.run_id, append_change: true, state: 'completed'});
  await refresh(page);
  await expect(page.locator('#lines-added')).toHaveText('+46');
  await expect(page.locator('#run-history [data-run-id]')).toHaveCount(3);
  await expect(page.locator('#project-change-summary')).toContainText('134');
  await refresh(page);
  await expect(page.locator('#lines-added')).toHaveText('+46');
  const preserved = await (await page.request.get('/ui/api/tasks/demo-chat-design/runs/demo-run-discovery')).json();
  expect(preserved).toEqual(original);
  await page.locator('#run-history [data-run-id="demo-run-design"]').click();
  await expect(page.locator('#lines-added')).toHaveText('+44');
});

test('pages a long archive without duplicate rows and refreshes every opened page', async ({page}) => {
  await reset(page);
  await project(page, STUDIO);
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  const path = '/ui/api/tasks/demo-chat-design/runs/demo-run-discovery';
  const original = await (await page.request.get(path)).json();
  const fields = ['messages', 'changes', 'api_events'];
  const total = 125;
  let revision = 1;
  let gate = null;
  const requests = [];
  const records = (field, version) => Array.from({length: total}, (_, index) => {
    const id = String(index).padStart(3, '0');
    if (field === 'messages') return {id: `archive-message-${id}`, role: 'assistant', text: `Archive message ${id}, revision ${version}`};
    if (field === 'changes') return {path: `src/archive/file-${id}.ts`, kind: 'modified', added: 1, removed: 0, diff: `@@ -1 +1 @@\n+export const revision = ${version}; // file ${id}`};
    return {id: `archive-event-${id}`, title: `Archive event ${id}, revision ${version}`, provider_id: index % 2 ? 'demo2' : 'demo1',
      role: index % 2 ? 'auxiliary' : 'main', time: original.started_at + index};
  });
  await page.route(url => url.pathname === path, async route => {
    const query = new URL(route.request().url()).searchParams;
    const field = fields.find(name => query.has(name + '_cursor')) || null;
    const cursor = field ? Number(query.get(field + '_cursor')) : 0;
    requests.push({field, cursor, revision});
    const response = {...original, files: total, added: total, removed: 0, touched_files: total, changes_revision: revision};
    for (const name of fields) {
      const offset = name === field ? cursor : 0;
      response[name] = records(name, revision).slice(offset, offset + 100);
      response[name + '_next_cursor'] = offset + 100 < total ? String(offset + 100) : null;
    }
    if (gate && field === gate.field && cursor === 100) {
      gate.reached();
      await gate.wait;
    }
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(response)});
  });
  const selected = page.locator('#run-history [data-run-id="demo-run-discovery"]');
  await selected.click();
  await expect(selected).toHaveAttribute('aria-pressed', 'true');
  const rows = {
    messages: page.locator('#conversation .message'),
    changes: page.locator('#changes details'),
    api_events: page.locator('#api-events .run-api-event'),
  };
  for (const field of fields) await expect(rows[field]).toHaveCount(100);
  for (const field of fields) {
    await page.locator(`[data-tab="${field === 'messages' ? 'conversation' : field === 'api_events' ? 'api-events' : 'changes'}"]`).click();
    let release;
    let reached;
    const captured = new Promise(resolve => { reached = resolve; });
    gate = {field, reached, wait: new Promise(resolve => { release = resolve; })};
    const more = page.locator(`[data-more-run="${field}"]`);
    try {
      // Keep page two in flight while both physical clicks reach the handler.
      await more.dblclick({delay: 30});
      await captured;
      expect(requests.filter(request => request.field === field && request.cursor === 100)).toHaveLength(1);
    } finally {
      gate = null;
      release();
    }
    await expect(rows[field]).toHaveCount(total);
    await expect(more).toHaveCount(0);
    const values = await rows[field].allTextContents();
    expect(new Set(values).size, `Unique ${field} after double click`).toBe(total);
  }
  await page.locator('[data-tab="changes"]').click();
  const expanded = page.locator('#changes details[data-path="src/archive/file-000.ts"]');
  await expanded.locator('summary').click();
  await page.locator('#changes').evaluate(element => { element.scrollTop = 180; });
  const scroll = await page.locator('#changes').evaluate(element => element.scrollTop);
  const beforeRefresh = requests.length;
  revision = 2;
  await refresh(page);
  await expect(page.locator('#conversation')).toContainText('Archive message 124, revision 2');
  await expect(page.locator('#api-events')).toContainText('Archive event 124, revision 2');
  await expect(expanded.locator('pre')).toContainText('revision = 2');
  await expect(expanded).toHaveAttribute('open', '');
  expect(await page.locator('#changes').evaluate(element => element.scrollTop)).toBeCloseTo(scroll, 0);
  for (const field of fields) await expect(rows[field]).toHaveCount(total);
  expect(requests.slice(beforeRefresh).map(request => [request.field, request.cursor])).toEqual([
    [null, 0], ['messages', 100], ['changes', 100], ['api_events', 100],
  ]);
  await page.reload();
  await expect(selected).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#history-live-button')).toBeVisible();
  await expect(rows.messages).toHaveCount(100);
  await expect(page.locator('#conversation')).toContainText('Archive message 099, revision 2');
});

test('distinguishes waiting, finalizing, success, error and interruption; renews an expired session without losing draft', async ({page}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  const chat = await newChat(page);
  const run = await start(page, 'Exercise task lifecycle');
  await expect(taskStatus(page, chat)).toHaveAttribute('data-task-status', 'running');
  for (const [state, polish, english] of [
    ['queued', 'W kolejce', 'Queued'],
    ['starting', 'Uruchamianie', 'Starting'],
    ['awaiting_input', 'Czeka na Ciebie', 'Waiting for you'],
    ['finalizing', 'Zapisywanie wyników', 'Saving results'],
    ['completed', 'Ukończone', 'Completed'],
  ]) {
    await post(page, '/__demo/control', {run_id: run.run_id, state});
    await refresh(page);
    await expect(taskStatus(page, chat)).toHaveAttribute('data-task-status', state);
    await expect(taskStatus(page, chat)).toHaveAttribute('aria-label', polish);
    await page.locator('#language-picker').selectOption('en');
    await expect(taskStatus(page, chat)).toHaveAttribute('aria-label', english);
    await page.locator('#language-picker').selectOption('pl');
    if (state === 'awaiting_input') await expect(page.locator('#conversation [data-approval]')).toHaveCount(2);
    if (state === 'finalizing') {
      await expect(page.locator('#run-button')).toBeHidden();
      await expect(page.locator('#run-button')).toBeDisabled();
      await expect(page.locator('#stop-task-button')).toBeDisabled();
      await expect(taskStatus(page, chat)).not.toHaveClass(/completed/);
      const unexpectedSubmissions = [];
      const observe = request => {
        if (request.method() === 'POST' && request.url().endsWith('/ui/api/tasks')) unexpectedSubmissions.push(request.url());
      };
      page.on('request', observe);
      try {
        await page.locator('#prompt').fill('Save this draft until finalizing has finished');
        await page.locator('#prompt').press('Control+Enter');
        await refresh(page);
        await expect(page.locator('#prompt')).toHaveValue('Save this draft until finalizing has finished');
        expect(unexpectedSubmissions, 'Ctrl+Enter must not submit a run during finalizing').toEqual([]);
        const saved = await (await page.request.get(`/ui/api/projects/${STUDIO}/history?task_id=${chat}`)).json();
        expect(saved.items.map(item => item.id)).toEqual([run.run_id]);
      } finally {
        page.off('request', observe);
      }
    }
    if (state === 'completed') await expect(taskStatus(page, chat)).toHaveClass(/completed/);
  }
  await expect(page.locator('#prompt')).toBeEnabled();
  const failed = await start(page, '[demo:failed] Simulate a provider failure');
  await expect(taskStatus(page, chat)).toHaveAttribute('data-task-status', 'failed');
  await expect(taskStatus(page, chat)).not.toHaveClass(/completed/);
  await expect(page.locator('#conversation')).toContainText('Demo provider returned an error');
  const stopped = await start(page, 'Stop this run while retaining changes');
  await page.locator('#stop-task-button').click();
  await expect(taskStatus(page, chat)).toHaveAttribute('data-task-status', 'interrupted');
  await expect(taskStatus(page, chat)).not.toHaveClass(/completed/);
  const history = await (await page.request.get(`/ui/api/projects/${STUDIO}/history?task_id=${chat}`)).json();
  expect(history.items.map(item => item.id)).toEqual([stopped.run_id, failed.run_id, run.run_id]);
  expect(history.items.map(item => item.state)).toEqual(['interrupted', 'failed', 'completed']);
  await page.locator('#prompt').fill('Keep this draft during session recovery');
  await post(page, '/__demo/control', {expire_sessions: true});
  await refresh(page);
  await expect(page.locator('#prompt')).toHaveValue('Keep this draft during session recovery');
  await expect(page.locator('#token')).toHaveCount(0);
  await expect(page.locator('#workspace')).toBeVisible();
});

test('deduplicates submitted client requests and pages chats, runs and run details', async ({page}) => {
  await reset(page, 'empty');
  const chat = await post(page, '/chats', {project_id: STUDIO, client_request_id: 'same-chat'});
  expect((await post(page, '/chats', {project_id: STUDIO, client_request_id: 'same-chat'})).id).toBe(chat.id);
  const body = {project_id: STUDIO, continue_task: chat.id, prompt: '[demo:completed] Persist one accepted run', api_ids: ['demo1', 'demo2'], effort: 'ultra', client_request_id: 'same-run'};
  const first = await post(page, '/tasks', body);
  expect(await post(page, '/tasks', body)).toEqual(first);
  await post(page, '/tasks', {...body, prompt: '[demo:completed] Follow-up run', client_request_id: 'next-run'});
  const list = await (await page.request.get(`/ui/api/projects/${STUDIO}/history?limit=1`)).json();
  expect(list.items).toHaveLength(1);
  expect(list.next_cursor).toBeTruthy();
  const second = await (await page.request.get(`/ui/api/projects/${STUDIO}/history?limit=1&cursor=${list.next_cursor}`)).json();
  expect(second.items[0].id).toBe(first.run_id);
  expect(second.next_cursor).toBeNull();
  const detail = await (await page.request.get(`/ui/api/tasks/${chat.id}/runs/${first.run_id}?messages_limit=1&changes_limit=1&api_events_limit=1`)).json();
  expect(detail.messages).toHaveLength(1);
  expect(detail.changes).toHaveLength(1);
  expect(detail.api_events).toHaveLength(1);
  expect(detail.messages_next_cursor).toBeTruthy();
  expect(detail.changes_next_cursor).toBeTruthy();
  expect(detail.api_events_next_cursor).toBeTruthy();
});

test('retries creation after a lost response without duplicating a persistent chat', async ({page}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  let first = true;
  const submittedIds = [];
  await page.route('**/ui/api/chats', async route => {
    submittedIds.push(route.request().postDataJSON().client_request_id);
    if (first) {
      first = false;
      await route.fetch(); // The server commits before the connection disappears.
      await route.abort('connectionfailed');
    } else await route.continue();
  });
  const lost = page.waitForEvent('requestfailed', request => request.url().endsWith('/ui/api/chats'));
  await page.locator('#new-task-button').click();
  await lost;
  await expect(page.locator('#new-task-button')).toBeEnabled();
  const retried = await newChat(page);
  const chats = await (await page.request.get(`/ui/api/projects/${STUDIO}/chats`)).json();
  expect(chats.items.map(item => item.id)).toEqual([retried]);
  expect(submittedIds).toHaveLength(2);
  expect(submittedIds[0]).toBeTruthy();
  expect(submittedIds[1]).toBe(submittedIds[0]);
  const conflict = await page.request.post('/ui/api/chats', {
    data: {project_id: WEBSITE, client_request_id: submittedIds[0]},
    headers: {'x-panel-csrf': await page.locator('meta[name="csrf-token"]').getAttribute('content'), Origin: new URL(page.url()).origin},
  });
  expect(conflict.status()).toBe(409);
  expect((await conflict.json()).code).toBe('request_conflict');
});

async function createWorkspace(page, {kind = 'project', name, path, access = 'isolated'} = {}) {
  await page.locator('[data-action="add-project"]').first().click();
  await page.locator(`[name="workspace-kind"][value="${kind}"]`).check();
  await page.locator('#workspace-name').fill(name || '');
  if (kind === 'project') await page.locator('#folder-path').fill(path);
  else {
    await page.locator(`[name="chat-access"][value="${access}"]`).check();
    if (access === 'full') await page.locator('#full-access-confirmed').check();
  }
  const created = page.waitForResponse(response => response.url().endsWith('/ui/api/projects') && response.request().method() === 'POST');
  await page.locator('#create-workspace').click();
  const response = await created;
  expect(response.ok()).toBeTruthy();
  const saved = await response.json();
  await expect(page.locator(`#project-list [data-project="${saved.id}"]`)).toHaveClass(/selected/);
  return saved;
}

test('retries a standalone chat workspace after a lost response and reload without another folder', async ({page}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  await page.locator('#prompt').fill('Keep my original project draft');
  let first = true, committed;
  const submittedIds = [];
  await page.route('**/ui/api/projects', async route => {
    const body = route.request().postDataJSON();
    submittedIds.push(body.client_request_id);
    if (first) {
      first = false;
      const response = await route.fetch();
      committed = await response.json();
      await route.abort('connectionfailed');
    } else await route.continue();
  });
  await page.locator('[data-action="add-project"]').first().click();
  await page.locator('[name="workspace-kind"][value="chat"]').check();
  await page.locator('#workspace-name').fill('A durable standalone chat');
  const lost = page.waitForEvent('requestfailed', request => request.url().endsWith('/ui/api/projects'));
  await page.locator('#create-workspace').click();
  await lost;
  await expect(page.locator('#create-workspace')).toBeEnabled();
  await expect(page.locator('#folder-error')).not.toBeEmpty();
  await page.reload();
  await expect(page.locator('#breadcrumb-project')).toHaveText('Studio');
  await expect(page.locator('#prompt')).toHaveValue('Keep my original project draft');
  const retried = await createWorkspace(page, {kind: 'chat', name: 'A durable standalone chat'});
  expect(retried.id).toBe(committed.id);
  expect(retried.path).toBe(committed.path);
  expect(submittedIds).toHaveLength(2);
  expect(submittedIds[0]).toBeTruthy();
  expect(submittedIds[1]).toBe(submittedIds[0]);
  const afterRetry = await (await page.request.get('/ui/api/state')).json();
  expect(afterRetry.projects.filter(item => item.kind === 'chat').map(item => item.id)).toEqual([committed.id]);
  const intentional = await createWorkspace(page, {kind: 'chat', name: 'A durable standalone chat'});
  expect(intentional.id).not.toBe(retried.id);
  expect(intentional.path).not.toBe(retried.path);
  expect(submittedIds[2]).not.toBe(submittedIds[0]);
  await project(page, STUDIO);
  await expect(page.locator('#prompt')).toHaveValue('Keep my original project draft');
  const headers = {'x-panel-csrf': await page.locator('meta[name="csrf-token"]').getAttribute('content'), Origin: new URL(page.url()).origin};
  const originalBody = {kind: 'chat', name: 'A durable standalone chat', access_mode: 'isolated', client_request_id: submittedIds[0]};
  const conflicting = await page.request.post('/ui/api/projects', {headers, data: {...originalBody, name: 'A different chat'}});
  expect(conflicting.status()).toBe(409);
  expect((await conflicting.json()).code).toBe('request_conflict');
  const removed = await page.request.delete(`/ui/api/projects/${committed.id}`, {headers});
  expect(removed.ok()).toBeTruthy();
  const archived = await page.request.post('/ui/api/projects', {headers, data: originalBody});
  expect(archived.status()).toBe(409);
  expect((await archived.json()).code).toBe('request_conflict');
});

test('creates projects and durable chats with an explicit fixed access scope', async ({page}) => {
  await reset(page, 'empty');
  const folder = await createWorkspace(page, {name: 'Nowy projekt', path: 'C:/Projects/Another'});
  expect(folder).toMatchObject({name: 'Nowy projekt', kind: 'project', access_mode: 'project'});
  await expect(page.locator('#permission-mode')).toBeEnabled();
  const isolated = await createWorkspace(page, {kind: 'chat', name: 'Mój czat'});
  expect(isolated).toMatchObject({name: 'Mój czat', kind: 'chat', access_mode: 'isolated'});
  await expect(page.locator('#permission-mode')).toHaveValue('approval');
  await expect(page.locator('#permission-mode')).toBeDisabled();
  await page.locator('#prompt').fill('Zachowaj ten szkic w moim czacie');
  await page.reload();
  await expect(page.locator('#breadcrumb-project')).toHaveText('Mój czat');
  await expect(page.locator('#project-path')).toContainText(isolated.path);
  await expect(page.locator('#prompt')).toHaveValue('Zachowaj ten szkic w moim czacie');

  await page.locator('[data-action="add-project"]').first().click();
  await page.locator('[name="workspace-kind"][value="chat"]').check();
  await page.locator('[name="chat-access"][value="full"]').check();
  await expect(page.locator('#full-access-confirmed')).not.toBeChecked();
  await expect(page.locator('#full-access-label')).toBeVisible();
  await page.locator('#create-workspace').click();
  await expect(page.locator('#project-dialog')).toBeVisible();
  expect(await page.locator('#full-access-confirmed').evaluate(el => el.validity.valueMissing)).toBeTruthy();
  await page.keyboard.press('Escape');
  const full = await createWorkspace(page, {kind: 'chat', name: 'Pełny dostęp', access: 'full'});
  expect(full).toMatchObject({kind: 'chat', access_mode: 'full'});
  await expect(page.locator('#permission-mode')).toHaveValue('yolo');
  await expect(page.locator('#permission-mode')).toBeDisabled();
  await page.locator('#language-picker').selectOption('en');
  await expect(page.locator('#breadcrumb-project')).toHaveText('Pełny dostęp');
  await expect(page.locator('#project-path')).toContainText('full computer access');
  await project(page, folder.id);
  await expect(page.locator('#permission-mode')).toBeEnabled();
  await project(page, isolated.id);
  await expect(page.locator('#permission-mode')).toHaveValue('approval');
  await expect(page.locator('#prompt')).toHaveValue('Zachowaj ten szkic w moim czacie');
});

test('removes only the project listing, preserves history and drafts, and rejects removing active work', async ({page}) => {
  await reset(page);
  await project(page, WEBSITE);
  await page.locator(`#project-list [data-remove-project="${WEBSITE}"]`).click();
  await page.locator('#confirm-remove-project').click();
  await expect(page.locator('#remove-project-error')).toContainText('Zakończ lub zatrzymaj');
  await expect(page.locator(`#project-list [data-project="${WEBSITE}"]`)).toBeVisible();
  await page.keyboard.press('Escape');

  await project(page, STUDIO);
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  await page.locator('#prompt').fill('Ten szkic też ma zostać');
  const original = await (await page.request.get(`/ui/api/projects/${STUDIO}/history`)).json();
  await page.locator(`#project-list [data-remove-project="${STUDIO}"]`).click();
  await expect(page.locator('#remove-project-dialog')).toContainText('Folder, pliki i zapisana historia pozostaną');
  await page.locator('#confirm-remove-project').click();
  await expect(page.locator(`#project-list [data-project="${STUDIO}"]`)).toHaveCount(0);
  await page.reload();
  await expect(page.locator(`#project-list [data-project="${STUDIO}"]`)).toHaveCount(0);
  expect(await (await page.request.get(`/ui/api/projects/${STUDIO}/history`)).json()).toEqual(original);
  const restored = await createWorkspace(page, {path: 'C:/Projects/Studio'});
  expect(restored.id).toBe(STUDIO);
  await expect(page.locator('#task-list [data-task="demo-chat-design"]')).toBeVisible();
  await expect(page.locator('#prompt')).toHaveValue('Ten szkic też ma zostać');
  await expect(page.locator('#run-history [data-run-id]')).toHaveCount(2);
});

test('keeps edited prompts through reload and a late settings response, saves exact user strings', async ({page}) => {
  await reset(page, 'empty');
  let held, signal;
  const captured = new Promise(resolve => { signal = resolve; });
  await page.route('**/ui/api/settings', async route => {
    if (route.request().method() === 'GET' && !held) {
      const response = await route.fetch();
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      held = {release}; signal(); await gate;
      await route.fulfill({response});
    } else await route.continue();
  });
  await page.locator('#settings-button').click();
  await captured;
  await expect(page.locator('#main-prompt')).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.locator('#settings-button').click();
  await expect(page.locator('#main-prompt')).toBeEnabled();
  expect((await page.locator('#main-prompt').inputValue()).length).toBeGreaterThan(1000);
  await page.locator('#main-prompt').fill('Mój własny prompt\nZachowaj dokładną treść.');
  await page.locator('#coordinator-prompt').fill(''); // Empty is an intentional user value.
  held.release();
  await expect(page.locator('#main-prompt')).toHaveValue('Mój własny prompt\nZachowaj dokładną treść.');
  await page.unroute('**/ui/api/settings');
  await page.reload();
  await page.locator('#settings-button').click();
  await expect(page.locator('#main-prompt')).toHaveValue('Mój własny prompt\nZachowaj dokładną treść.');
  await expect(page.locator('#coordinator-prompt')).toHaveValue('');
  await expect(page.locator('#settings-draft-note')).toBeVisible();
  await page.locator('#save-settings').click();
  await expect(page.locator('#settings-dialog')).not.toBeVisible();
  const saved = await (await page.request.get('/ui/api/settings')).json();
  expect(saved.main_prompt).toBe('Mój własny prompt\nZachowaj dokładną treść.');
  expect(saved.coordinator_prompt).toBe('');
  await page.locator('#language-picker').selectOption('en');
  await page.reload();
  await page.locator('#settings-button').click();
  await expect(page.locator('#main-prompt')).toHaveValue(saved.main_prompt);
  await expect(page.locator('#coordinator-prompt')).toHaveValue('');
  await expect(page.locator('#settings-draft-note')).toBeHidden();
  await expect(page.locator('[data-settings-pane="prompts"]')).toContainText('planner works at Ultra');
});

test('edits and persists separate API token thresholds and shows budget pressure', async ({page}) => {
  await reset(page);
  await page.locator('[data-provider="demo1"] > summary').click();
  await page.locator('[data-edit-api="demo1"]').click();
  await expect(page.locator('#api-tpm')).toHaveValue('1000000');
  await expect(page.locator('#api-tpm-soft')).toHaveValue('900000');
  await expect(page.locator('#api-tpm-hard')).toHaveValue('950000');
  await page.locator('#api-tpm').fill('1000000001');
  await page.locator('#save-provider').click();
  expect(await page.locator('#api-tpm').evaluate(el => el.validity.rangeOverflow)).toBeTruthy();
  await expect(page.locator('#provider-dialog')).toBeVisible();
  await page.locator('#api-tpm').fill('1000000');
  await page.locator('#api-tpm-soft').fill('950000');
  await page.locator('#save-provider').click();
  await expect(page.locator('#provider-error')).toContainText('0 < próg miękki < próg twardy');
  await page.locator('#api-tpm').fill('30000');
  await page.locator('#api-tpm-soft').fill('10000');
  await page.locator('#api-tpm-hard').fill('20000');
  await page.locator('#save-provider').click();
  await expect(page.locator('#provider-dialog')).not.toBeVisible();
  await expect(page.locator('[data-token-budget="demo1"]')).toHaveClass(/hard/);
  await expect(page.locator('[data-token-budget="demo1"]')).toContainText('Próg twardy');
  await page.locator('[data-edit-api="demo1"]').click();
  await page.locator('#api-tpm').fill('2000000');
  await page.locator('#api-tpm-soft').fill('1800000');
  await page.locator('#api-tpm-hard').fill('1900000');
  await page.locator('#save-provider').click();
  await expect(page.locator('#provider-dialog')).not.toBeVisible();
  await page.reload();
  const values = await (await page.request.get('/ui/api/providers')).json();
  expect(values.providers.find(p => p.id === 'demo1')).toMatchObject({tokens_per_minute: 2000000, soft_tokens_per_minute: 1800000, hard_tokens_per_minute: 1900000});
  expect(values.providers.find(p => p.id === 'demo2')).toMatchObject({tokens_per_minute: 1000000, soft_tokens_per_minute: 900000, hard_tokens_per_minute: 950000});
  await page.locator('#language-picker').selectOption('en');
  await expect(page.locator('[data-token-budget="demo1"]')).toContainText('last 60 s');
  await page.locator('[data-provider="demo1"] > summary').click();
  await page.locator('[data-edit-api="demo1"]').click();
  await expect(page.locator('#api-tpm')).toHaveValue('2000000');
  await expect(page.locator('.api-budget-fields')).toContainText('Soft threshold');
});

test('shows archived run measurements independently from a newer experiment in the same chat', async ({page}) => {
  await reset(page);
  await page.route('**/ui/api/state?*', async route => {
    const response = await route.fetch();
    const value = await response.json();
    const task = value.tasks.find(item => item.id === 'demo-chat-design');
    Object.assign(task, {working_copies: true, experiment: {phase: 'workers'}, files: 99});
    value.proxy.routes = [{task_id: task.id, run_id: task.run_id, last_provider: 'demo3'}];
    await route.fulfill({response, json: value});
  });
  await project(page, STUDIO);
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  await refresh(page);
  await expect(page.locator('#files-count')).toHaveText('99 plików w kopiach roboczych');
  await expect(page.locator('#task-token-count')).toContainText('Demo · Studio');
  await page.locator('#run-history [data-run-id="demo-run-discovery"]').click();
  await expect(page.locator('#files-count')).toHaveText('3 zmienionych plików');
  await expect(page.locator('#task-token-count')).toBeHidden();
  await refresh(page);
  await expect(page.locator('#files-count')).toHaveText('3 zmienionych plików');
  await page.locator('#history-live-button').click();
  await expect(page.locator('#files-count')).toHaveText('99 plików w kopiach roboczych');
  await expect(page.locator('#task-token-count')).toContainText('Demo · Studio');
});

test('keeps the last real project outcome after an empty chat and prioritizes active work', async ({page}) => {
  await reset(page, 'empty');
  await project(page, STUDIO);
  const mark = page.locator(`#project-list [data-project="${STUDIO}"] [data-project-status]`);
  const first = await newChat(page);
  await start(page, '[demo:completed] Complete the first useful run');
  await expect(mark).toHaveAttribute('data-project-status', 'completed');
  await newChat(page);
  await expect(mark).toHaveAttribute('data-project-status', 'completed');
  await expect(mark).toHaveClass(/completed/);
  const second = await start(page, 'Run from the new chat');
  await expect(mark).toHaveAttribute('data-project-status', 'running');
  await post(page, '/__demo/control', {run_id: second.run_id, state: 'failed'});
  await refresh(page);
  await expect(mark).toHaveAttribute('data-project-status', 'failed');
  await page.locator(`#task-list [data-task="${first}"]`).click();
  await start(page, '[demo:completed] Complete a newer run');
  await expect(mark).toHaveAttribute('data-project-status', 'completed');
  await page.reload();
  await expect(mark).toHaveAttribute('data-project-status', 'completed');
});
