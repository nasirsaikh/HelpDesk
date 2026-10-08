const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

const base = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const output = process.env.TEST_OUTPUT || path.join(__dirname, '../test-results');
if (!process.env.DEMO_PASSWORD) throw new Error('Set DEMO_PASSWORD to the password printed by seed_demo.');
fs.mkdirSync(output, { recursive: true });
const report = { pages: [], checks: [], errors: [] };
let developmentServer;
process.on('exit', () => developmentServer?.kill());
(async () => {
  if (process.env.TEST_START_SERVER === '1') {
    const root = path.resolve(__dirname, '..');
    const log = fs.createWriteStream(path.join(output, 'server.log'));
    developmentServer = spawn(process.env.DJANGO_PYTHON || 'python', ['-u', 'manage.py', 'runserver', '127.0.0.1:8000', '--noreload'], {cwd: root, env: {...process.env}});
    developmentServer.stdout.pipe(log); developmentServer.stderr.pipe(log);
    let ready = false;
    for (let attempt = 0; attempt < 40; attempt++) {
      try { await fetch(base + '/login/'); ready = true; break; } catch (_) { await new Promise(resolve => setTimeout(resolve, 250)); }
    }
    assert.ok(ready, 'Development server started');
  }
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {}),
    args: ['--no-sandbox', '--no-zygote', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  const context = await browser.newContext({viewport: {width: 1440, height: 960}});
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.message));
  await page.goto(base + '/login/');
  await page.locator('[name=username]').fill('demo.admin@takaful.example');
  await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD);
  await Promise.all([page.waitForURL(base + '/'), page.getByRole('button', {name: 'Sign in', exact: false}).click()]);
  async function visit(url, label, screenshot = true) {
    const response = await page.goto(base + url, {waitUntil: 'networkidle'});
    assert.equal(response.status(), 200, label + ': response');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), label + ': horizontal overflow');
    report.pages.push({label, url, width: await page.evaluate(() => innerWidth)});
    if (screenshot) await page.screenshot({path: path.join(output, label + '.png'), fullPage: true});
  }
  await visit('/', 'overview-desktop');
  await visit('/tickets/', 'tickets-desktop');
  const search = page.locator('.filter-bar [name=q]');
  const fragment = page.waitForResponse(r => r.url().includes('/tickets/?q=Medical') && r.request().headers()['hx-request'] === 'true');
  await search.fill('Medical'); await fragment;
  assert.ok((await page.locator('#ticket-results').innerText()).includes('Medical card replacement'));
  assert.ok(!(await page.locator('#ticket-results').innerText()).includes('New employee enrollment'));
  report.checks.push('HTMX search returned and rendered a scoped fragment');
  await visit('/tickets/new/', 'create-request');
  await page.locator('[name=request_type]').selectOption('TICKET');
  await page.locator('[name=title]').fill('Browser verification request');
  await page.locator('[name=description]').fill('Created through the real browser form for company A.');
  await page.locator('[name=project]').selectOption({label: 'GLIS · HelpDesk'});
  const category = await page.locator('[name=category] option').evaluateAll(options => options.find(option => option.textContent === 'HelpDesk').value);
  await page.locator('[name=category]').selectOption(category);
  await Promise.all([page.waitForURL(/\/tickets\/[a-f0-9-]+\/$/), page.getByRole('button', {name: 'Save changes'}).click()]);
  const privateTicketPath = new URL(page.url()).pathname;
  await page.locator('#comment-body').fill('Browser verification comment.');
  await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Post comment'}).click()]);
  assert.ok((await page.locator('.comment-list').innerText()).includes('Browser verification comment.'));
  await page.screenshot({path: path.join(output, 'ticket-detail.png'), fullPage: true});
  report.checks.push('Ticket creation and comment submission passed CSRF/company checks');
  await visit('/policies/', 'policies');
  await page.getByRole('link', {name: 'Open →', exact: true}).first().click();
  await page.waitForURL(/\/policies\/[a-f0-9-]+\/$/);
  await page.screenshot({path: path.join(output, 'policy-members.png'), fullPage: true});
  assert.equal(await page.locator('.member-card').count(), 5);
  await page.getByRole('button', {name: 'Full screen'}).click();
  assert.ok(await page.locator('.fullscreen-panel').isVisible());
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.fullscreen-panel').count(), 0);
  await page.getByRole('link', {name: 'Inactive', exact: false}).click();
  await page.waitForURL(/tab=inactive/);
  assert.equal(await page.locator('.member-card').count(), 1);
  report.checks.push('Member cards, active/inactive tabs and fullscreen controls');
  for (const [url, label] of [['/settings/', 'company-settings'], ['/reports/', 'reports'], ['/ai/search/?q=card', 'knowledge-retrieval']]) await visit(url, label);
  await visit('/reports/', 'reports-form', false);
  await page.getByRole('button', {name: 'Run query'}).click();
  await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('.alert.error').count(), 0);
  assert.ok((await page.locator('table').first().innerText()).includes('OPEN'));
  const companyA = await page.locator('body').getAttribute('data-company');
  const companyB = await page.locator('[data-company-switch] option').evaluateAll(options => options.find(option => option.textContent === 'ABC Insurance').value);
  await Promise.all([page.waitForNavigation(), page.locator('[data-company-switch]').selectOption(companyB)]);
  assert.notEqual(await page.locator('body').getAttribute('data-company'), companyA);
  assert.equal(await page.getByRole('link', {name: 'Create request', exact: false}).count(), 0);
  const denied = await page.goto(base + privateTicketPath);
  assert.equal(denied.status(), 404);
  report.checks.push('Company switch refreshed context, enforced Auditor role, and denied old-company UUID');
  await page.goto(base + '/');
  await Promise.all([page.waitForNavigation(), page.locator('[data-company-switch]').selectOption(companyA)]);
  await page.locator('[data-theme-toggle]').click();
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  await page.screenshot({path: path.join(output, 'overview-dark.png'), fullPage: true});
  await page.locator('[data-theme-toggle]').click();
  await page.setViewportSize({width: 390, height: 844});
  await visit('/', 'overview-mobile');
  await page.locator('[data-toggle-sidebar]').click();
  assert.ok(await page.locator('body').evaluate(node => node.classList.contains('sidebar-open')));
  await page.waitForFunction(() => Math.abs(document.getElementById('sidebar').getBoundingClientRect().left) < 1);
  assert.equal(await page.locator('.app-shell').evaluate(node => node.inert), true);
  await page.screenshot({path: path.join(output, 'navigation-mobile.png'), fullPage: true, animations: 'disabled'});
  await page.locator('[data-close-sidebar]').click();
  assert.ok(!(await page.locator('body').evaluate(node => node.classList.contains('sidebar-open'))));
  assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), true);
  await visit('/tickets/new/', 'create-request-mobile');
  await visit('/tickets/', 'tickets-mobile');
  report.checks.push('Responsive layouts at 1440px and 390px; theme and mobile navigation');
  assert.deepEqual(report.errors, [], 'Browser JavaScript errors');
  await browser.close();
  fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  developmentServer?.kill();
})().catch(error => { console.error(error); process.exit(1); });
