// Opt-in documentation capture. It runs only the offline demo and writes no
// screenshots during `npm run test:ui`. Paths and text are fictional demo data.
import {chromium} from '@playwright/test';
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {resolve, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as pause} from 'node:timers/promises';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const output = resolve(root, option('--output', 'docs/images'));
const port = Number(option('--port', '44102'));
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Choose a local demo port from 1024 to 65535.');
const python = process.env.TEST_PYTHON || (process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const child = spawn(resolve(root, python), ['scripts/serve_demo.py', '--port', String(port)], {
  cwd: root, stdio: ['ignore', 'inherit', 'inherit'], windowsHide: true,
});
let childError;
child.once('error', error => { childError = error; });
const baseURL = `http://127.0.0.1:${port}`;
let browser;
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (childError) throw childError;
    if (child.exitCode !== null) throw new Error(`Demo exited with code ${child.exitCode}.`);
    try { ready = (await fetch(baseURL + '/health')).ok; } catch { /* Starting the local fixture. */ }
    if (ready) break;
    await pause(100);
  }
  if (!ready) throw new Error('Offline demo did not start.');
  browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
  const page = await browser.newPage({viewport: {width: 1512, height: 1100}, deviceScaleFactor: 1, locale: 'pl-PL'});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseURL + '/ui/');
  await page.locator('#project-list [data-project="demo-project-0"]').click();
  await page.locator('#task-list [data-task="demo-chat-design"]').click();
  await page.locator('#conversation').getByText('Workspace updated.', {exact: false}).first().waitFor();
  await page.locator('#run-history [data-run-id="demo-run-design"]').waitFor();
  await page.evaluate(() => document.fonts.ready);
  await mkdir(output, {recursive: true});
  const capture = async (name, fullPage = true) => {
    await page.evaluate(() => {
      document.activeElement?.blur?.();
      window.scrollTo(0, 0);
    });
    await page.screenshot({path: join(output, name), fullPage, animations: 'disabled'});
    process.stdout.write(`Captured ${name}\n`);
  };
  await capture('dashboard-pl.png');
  await page.locator('#language-picker').selectOption('en');
  await capture('dashboard-en.png');
  await page.locator('#run-history [data-run-id="demo-run-discovery"]').click();
  await page.locator('[data-tab="changes"]').click();
  await page.locator('#changes details').first().locator('summary').click();
  await capture('history-en.png');
  await page.locator('#settings-button').click();
  await page.locator('[data-settings-tab="prompts"]').click();
  await page.locator('#save-settings:enabled').waitFor();
  await capture('settings-prompts-en.png', false);
  await page.locator('[data-settings-tab="limits"]').click();
  await page.locator('#save-settings:enabled').waitFor();
  await capture('settings-en.png', false);
  await page.keyboard.press('Escape');
  await page.locator('[data-provider="demo1"] > summary').click();
  await page.locator('[data-edit-api="demo1"]').click();
  await page.locator('#api-tpm').waitFor();
  if (await page.locator('#api-key').inputValue()) throw new Error('Documentation captures must not contain API keys.');
  await capture('provider-limits-en.png', false);
  await page.keyboard.press('Escape');
  await page.locator('[data-provider="demo1"] > summary').click();
  await page.locator('[data-action="add-project"]').first().click();
  await page.locator('[name="workspace-kind"][value="chat"]').check();
  await page.locator('#workspace-name').fill('Research notes');
  await capture('workspace-chat-en.png', false);
  await page.keyboard.press('Escape');
  await page.locator('#history-live-button').click();
  await page.locator('[data-tab="conversation"]').click();
  await page.locator('#language-picker').selectOption('pl');
  await page.setViewportSize({width: 390, height: 1100});
  await capture('mobile-pl.png', false);
  if (errors.length) throw new Error(`Uncaught browser errors: ${errors.join('; ')}`);
} finally {
  await browser?.close();
  child.kill('SIGTERM');
}
