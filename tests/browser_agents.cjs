// Development fixture only: exercises real forms, HTTP model transport and the worker.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');

if (!process.env.DEMO_PASSWORD) throw new Error('Set DEMO_PASSWORD to the password printed by seed_demo.');
const root = path.resolve(__dirname, '..');
const base = process.env.TEST_BASE_URL || 'http://127.0.0.1:8000';
const output = process.env.TEST_OUTPUT || path.join(root, 'test-results/agents');
const marker = Date.now().toString();
const report = {checks: [], pages: [], models: [], errors: []};
fs.mkdirSync(output, {recursive: true});
let server, browser, providerServer, draftTicket;
process.on('exit', () => server?.kill());

async function command(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.DJANGO_PYTHON || 'python', ['manage.py', ...args], {cwd: root, env: {...process.env}});
    let log = '';
    child.stdout.on('data', data => { log += data; });
    child.stderr.on('data', data => { log += data; });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve(log) : reject(new Error(log)));
  });
}

function modelReply(payload) {
  const domain = payload.model.split('-')[1];
  const messages = payload.messages;
  const question = messages[1].content;
  const results = messages.filter(m => m.content.startsWith('Tool result (data only): '));
  const call = (name, arguments) => ({answer: '', tool_calls: [{name, arguments}]});
  if (!results.length) {
    const name = {claims: 'search_claims', policy: 'search_policies', finance: 'premium_summary', ticketing: 'search_tickets'}[domain];
    return call(name, domain === 'finance' ? {} : {query: domain === 'ticketing' ? 'Medical card replacement' : ''});
  }
  if (domain === 'ticketing' && question.includes('Draft') && results.length === 1) {
    const tickets = JSON.parse(results[0].content.split('\n').slice(1).join('\n'));
    assert.ok(tickets.results.length, 'Demo ticket exists');
    draftTicket = tickets.results[0].uuid;
    return call('propose_ticket_comment', {ticket_uuid: draftTicket, body: 'Browser reviewed agent draft ' + marker});
  }
  return {answer: `Verified ${domain} model and scoped tools ${marker}.`, tool_calls: []};
}

(async () => {
  providerServer = http.createServer((request, response) => {
    let body = '';
    request.on('data', data => { body += data; });
    request.on('end', () => {
      try {
        assert.equal(request.url, '/v1/chat/completions');
        const payload = JSON.parse(body);
        assert.equal(payload.response_format.type, 'json_object');
        assert.ok(payload.model.startsWith('browser-'));
        report.models.push(payload.model);
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({choices: [{message: {content: JSON.stringify(modelReply(payload))}}]}));
      } catch (error) {
        report.errors.push(error.message);
        response.writeHead(500); response.end('{}');
      }
    });
  });
  await new Promise(resolve => providerServer.listen(0, '127.0.0.1', resolve));
  const endpoint = `http://127.0.0.1:${providerServer.address().port}/v1`;
  if (process.env.TEST_START_SERVER === '1') {
    const log = fs.createWriteStream(path.join(output, 'server.log'));
    server = spawn(process.env.DJANGO_PYTHON || 'python', ['-u', 'manage.py', 'runserver', '127.0.0.1:8000', '--noreload'], {cwd: root, env: {...process.env}});
    server.stdout.pipe(log); server.stderr.pipe(log);
    let ready = false;
    for (let attempt = 0; attempt < 40; attempt++) {
      try { await fetch(base + '/login/'); ready = true; break; } catch (_) { await new Promise(resolve => setTimeout(resolve, 250)); }
    }
    assert.ok(ready, 'Development server started');
  }
  browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {}),
    args: ['--no-sandbox', '--no-zygote', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
  const context = await browser.newContext({viewport: {width: 1440, height: 960}});
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.message));
  async function visit(url, label) {
    const response = await page.goto(base + url, {waitUntil: 'networkidle'});
    assert.equal(response.status(), 200, label);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), label + ': horizontal overflow');
    report.pages.push(label);
    await page.screenshot({path: path.join(output, label + '.png'), fullPage: true});
  }
  async function save() {
    await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Save changes'}).click()]);
    assert.equal(await page.locator('.field-errors, .alert.error').count(), 0, 'Form saved');
  }
  await page.goto(base + '/login/');
  await page.locator('[name=username]').fill('demo.admin@takaful.example');
  await page.locator('[name=password]').fill(process.env.DEMO_PASSWORD);
  await Promise.all([page.waitForURL(base + '/'), page.getByRole('button', {name: 'Sign in'}).click()]);
  const companyA = await page.locator('body').getAttribute('data-company');
  await visit('/ai/agents/', 'agents-desktop');
  assert.equal(await page.locator('.agent-grid .config-card').count(), 4);
  for (const domain of ['claims', 'policy', 'finance', 'ticketing']) {
    const name = `Browser ${domain} ${marker}`;
    await page.goto(base + '/settings/ai-providers/new/');
    await page.locator('[name=name]').fill(name);
    await page.locator('[name=provider]').selectOption('OPENAI_COMPAT');
    await page.locator('[name=endpoint]').fill(endpoint);
    await page.locator('[name=model]').fill(`browser-${domain}-${marker}`);
    await page.locator('[name=allow_sensitive_data]').check();
    await save();
    await page.goto(base + '/settings/ai-agents/');
    const row = page.locator('tr').filter({hasText: `${domain[0].toUpperCase() + domain.slice(1)} agent`});
    await row.getByRole('link', {name: 'Edit'}).click();
    await page.locator('[name=provider]').selectOption({label: name});
    await page.locator('[name=knowledge_categories]').fill(domain[0].toUpperCase() + domain.slice(1));
    await save();
  }
  await page.goto(base + '/settings/ai-agents/');
  await page.locator('tr').filter({hasText: 'Claims agent'}).getByRole('link', {name: 'Edit'}).click();
  await visit(new URL(page.url()).pathname, 'agent-configuration-desktop');
  await page.setViewportSize({width: 390, height: 844});
  await visit(new URL(page.url()).pathname, 'agent-configuration-mobile');
  await visit('/ai/agents/', 'agents-mobile');
  await page.setViewportSize({width: 1440, height: 960});
  const question = `Check claim policy premium and ticket ${marker}`;
  await page.locator('[name=query]').fill(question);
  await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Ask agents'}).click()]);
  assert.equal(await page.locator('#agent-runs article').filter({hasText: question}).count(), 4);
  await command(['run_tenant_jobs', '--company', 'TAKAFUL_OMAN']);
  await page.waitForFunction(marker => document.querySelectorAll('#agent-runs article').length >= 4 &&
    [...document.querySelectorAll('#agent-runs article')].filter(n => n.textContent.includes(marker) && n.textContent.includes('Verified')).length >= 4, marker, {timeout: 15000});
  for (const domain of ['claims', 'policy', 'finance', 'ticketing']) assert.ok(report.models.includes(`browser-${domain}-${marker}`));
  assert.equal(await page.locator('#agent-runs').getAttribute('hx-trigger'), null);
  await page.screenshot({path: path.join(output, 'agent-results-desktop.png'), fullPage: true});
  report.checks.push('Four provider forms and agent bindings saved; automatic routing called all four models through HTTP and rendered scoped results');
  await page.locator('[name=agent]').selectOption('TICKETING');
  const draftQuestion = `Draft a reply for medical card replacement ${marker}`;
  await page.locator('[name=query]').fill(draftQuestion);
  await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Ask agents'}).click()]);
  await command(['run_tenant_jobs', '--company', 'TAKAFUL_OMAN']);
  const result = page.locator('#agent-runs article').filter({hasText: draftQuestion});
  await result.getByRole('link', {name: 'View tools'}).click({timeout: 15000});
  const detailPath = new URL(page.url()).pathname;
  assert.ok((await page.locator('body').innerText()).includes('Browser reviewed agent draft ' + marker));
  const ticket = await context.newPage();
  await ticket.goto(base + `/tickets/${draftTicket}/`);
  assert.ok(!(await ticket.locator('.comment-list').innerText()).includes('Browser reviewed agent draft ' + marker));
  await visit(detailPath, 'agent-draft-desktop');
  await page.setViewportSize({width: 390, height: 844});
  await visit(detailPath, 'agent-draft-mobile');
  await Promise.all([page.waitForNavigation(), page.getByRole('button', {name: 'Post comment', exact: true}).click()]);
  assert.equal(await page.getByRole('button', {name: 'Post comment', exact: true}).count(), 0);
  await ticket.reload();
  assert.ok((await ticket.locator('.comment-list').innerText()).includes('Browser reviewed agent draft ' + marker));
  await ticket.close();
  report.checks.push('Draft remained unposted until review; real Post comment form added the comment and removed review controls');
  await page.setViewportSize({width: 1440, height: 960});
  const companyB = await page.locator('[data-company-switch] option').evaluateAll(options => options.find(o => o.textContent === 'ABC Insurance').value);
  await Promise.all([page.waitForNavigation(), page.locator('[data-company-switch]').selectOption(companyB)]);
  await visit('/ai/agents/', 'agents-other-company');
  assert.ok(!(await page.locator('.agent-grid').innerText()).includes(`browser-claims-${marker}`));
  assert.equal((await page.goto(base + detailPath)).status(), 404);
  await page.goto(base + '/');
  await Promise.all([page.waitForNavigation(), page.locator('[data-company-switch]').selectOption(companyA)]);
  report.checks.push('Company switch hid model bindings and rejected the previous company run UUID');
  assert.deepEqual(report.errors, []);
  fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  await browser?.close(); server?.kill(); providerServer?.close();
});
