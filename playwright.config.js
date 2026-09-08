import {defineConfig} from '@playwright/test';
const python = process.env.TEST_PYTHON || (process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
export default defineConfig({
  testDir: './tests/browser', fullyParallel: false, workers: 1, timeout: 30000,
  use: {baseURL: 'http://127.0.0.1:44101', browserName: 'chromium', viewport: {width: 1512, height: 1100},
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined, trace: 'retain-on-failure'},
  webServer: {command: `"${python}" scripts/serve_demo.py`, url: 'http://127.0.0.1:44101/health', reuseExistingServer: false, timeout: 40000},
  reporter: [['list']],
});
