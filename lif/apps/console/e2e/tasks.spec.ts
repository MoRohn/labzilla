// §103–§105 as a first-time user would do them, without instructions: ask and see who answered, understand
// a fallback, check the GPU, pause a batch job, check for better models, approve a review,
// connect a phone and continue its conversation on the desktop. §62: an outage reads as a human message.
import { devices, expect, test, type Page } from 'playwright/test';
import { FAKE, STATE } from './env';
import { RAW_TERMS, setScenario, waitReady } from './helpers';

test.use({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
test.afterEach(async () => {
  await setScenario('healthy');
});

/** The visible text of the page never shows raw HTTP codes, k8s states or JS leftovers (§38, §81, §91). */
async function expectHumanText(page: Page, where: string) {
  const text = await page.locator('body').innerText();
  expect(text.match(RAW_TERMS)?.[0] ?? null, `${where}: raw technical text on screen`).toBeNull();
}

async function sendPrompt(page: Page, text: string) {
  const box = page.getByRole('textbox', { name: 'What do you want to do?' });
  await box.fill(text);
  await page.getByRole('button', { name: /^Send/ }).click();
}

test('ask a question: streamed answer, "Handled by", and which model handled it (§9, §13, §21)', async ({ page }) => {
  await page.goto('/');
  await waitReady(page);
  // From Home, the command bar is the fastest path: a question goes to Ask and is sent there.
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Write a haiku about local AI on a quiet evening');
  await bar.press('Enter');
  await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
  const answer = page.locator('.ask-thread article').last();
  // Streaming: text grows while the caret shows, then the receipt appears.
  await expect(page.locator('.lz-caret')).toBeVisible({ timeout: 15_000 });
  const early = (await answer.innerText()).length;
  await expect.poll(async () => (await answer.innerText()).length, { timeout: 15_000 }).toBeGreaterThan(early);
  const receipt = page.getByText(/^Handled by local\//);
  await expect(receipt).toBeVisible({ timeout: 45_000 });
  await expect(page.locator('.lz-caret')).toHaveCount(0);
  await expect(page.getByText('Local only').first()).toBeVisible();

  // Which model handled it: Open Details names the mode, model and route.
  await page.getByRole('button', { name: 'Open Details' }).click();
  const drawer = page.getByRole('dialog', { name: 'Answer details' });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText('Model', { exact: true })).toBeVisible();
  await expect(drawer).toContainText(/Qwen|Llama|Phi/);
  await expect(drawer.getByRole('heading', { name: 'Route' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(page.getByRole('button', { name: 'Open Details' })).toBeFocused();
  await expectHumanText(page, 'ask');
});

test('Ask answers questions about Labzilla from live state, and the model on request (§9, §13, §88)', async ({ page }) => {
  await page.goto('/ask');
  await waitReady(page);
  // A status question typed into Ask: answered by the rules from the snapshot, no conversation is created.
  await sendPrompt(page, 'Why is the GPU busy?');
  const turn = page.getByRole('region', { name: 'Answered by Labzilla' });
  await expect(turn.getByRole('heading', { name: 'What the GPU is doing' })).toBeVisible();
  await expect(turn.getByText('Understood by Labzilla rules')).toBeVisible();
  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByRole('textbox', { name: 'What do you want to do?' })).toHaveValue('');
  // One click sends the same words to the model instead.
  await turn.getByRole('button', { name: 'Ask the model instead' }).click();
  await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
  await expect(page.getByText(/^Handled by local\//)).toBeVisible({ timeout: 45_000 });
  await expect(turn).toHaveCount(0);

  // An operational command proposes its action (nothing runs until pressed); a request to explain goes to the model.
  await sendPrompt(page, 'Pause batch jobs');
  await expect(page.getByRole('region', { name: 'Answered by Labzilla' }).getByRole('button', { name: /Pause all batch jobs/ })).toBeVisible();
  await page.getByRole('region', { name: 'Answered by Labzilla' }).getByRole('button', { name: 'Close' }).click();
  const answers = await page.locator('.ask-thread article').count();
  await sendPrompt(page, 'Explain how GPU memory works');
  await expect(page.getByRole('region', { name: 'Answered by Labzilla' })).toHaveCount(0);
  await expect.poll(() => page.locator('.ask-thread article').count(), { timeout: 15_000 }).toBeGreaterThan(answers);
  await expectHumanText(page, 'ask routing');
});

test('Home cockpit: KPI tiles with trends, recent conversations, services, folded activity (§6)', async ({ page }) => {
  await page.goto('/');
  const kpis = page.getByRole('region', { name: 'Last 24 hours' });
  await expect(kpis).toBeVisible({ timeout: 20_000 });
  for (const label of ['Answers in Ask', 'Answer speed', 'First word (p90)', 'Availability', 'Free memory', 'DGX GPU load', 'AI requests', 'Value (estimate)'])
    await expect(kpis.getByText(label, { exact: true })).toBeVisible();
  // Trends speak in words, not only lines (§57): the free-memory sparkline names the safety margin.
  await expect(kpis.getByRole('img', { name: /Free memory over the last 6 hours: now .* GB.*8 GB safety margin/ })).toBeVisible();
  // Every tile with a destination is a drill-down.
  await expect(kpis.getByRole('link', { name: /Free memory/ })).toHaveAttribute('href', '/system/compute');
  await expect(page.getByRole('region', { name: 'Recent conversations' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Services' })).toContainText(/services healthy|need a look/);
  // Repeats fold into one row with a count instead of a wall of identical lines.
  const titles = await page.getByRole('list', { name: 'Recent activity' }).locator('li').allInnerTexts();
  const firstLines = titles.map((t) => t.split('\n')[0]);
  expect(new Set(firstLines).size, 'adjacent activity rows repeat').toBe(firstLines.length);
  await expectHumanText(page, 'home cockpit');
});

test('understand why AI fell back: Home explains, the role page shows the cause (§81, §82)', async ({ page }) => {
  await setScenario('fallback');
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Running on fallback model' })).toBeVisible({ timeout: 20_000 });
  const attention = page.getByRole('region', { name: 'Needs your attention' });
  await expect(attention).toBeVisible();
  const card = attention.getByRole('alert').first();
  await expect(card).toContainText(/model unavailable/);
  await expect(card).toContainText('Labzilla is using');
  await expect(card).toContainText('Responses may be less capable');
  await expect(card.getByRole('button', { name: 'Retry default' })).toBeVisible();
  await expectHumanText(page, 'home (fallback)');

  await card.getByRole('button', { name: 'View details' }).click();
  await expect(page).toHaveURL(/\/models\/roles\/\w+$/);
  await waitReady(page);
  const main = page.locator('main');
  await expect(main).toContainText(/backup/i);
  await expect(main).toContainText('Qwen3 1.7B');
  await expectHumanText(page, 'role page (fallback)');

  // The command bar answers the same question in words (§8).
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Why did local/default fall back?');
  await bar.press('Enter');
  const result = page.getByRole('region', { name: 'Command result' });
  await expect(result).toBeVisible({ timeout: 10_000 });
  await expect(result).toContainText(/1\.7B|backup|fallback/i);
});

test('check GPU load without an external workload (System › Compute, §36)', async ({ page }) => {
  await setScenario('blerbz-busy');
  await page.goto('/system/compute');
  await waitReady(page);
  const main = page.locator('main');
  await expect(main).toContainText(/GPU load\s*\d+\s*%/, { timeout: 20_000 });
  await expect(main).toContainText(/\/\s*128(\.0)?\s*GB/);
  await expect(main).not.toContainText('BLERBZ');
  await expect(main.getByRole('heading', { name: 'Primary workload' })).toHaveCount(0);
  await expectHumanText(page, 'compute (external workload off)');

  // Batch work says why it waits and that it resumes by itself (§31).
  await page.goto('/jobs');
  await waitReady(page);
  await expect(page.locator('main')).toContainText('GPU reserved for Primary workload');
  await expect(page.locator('main')).toContainText('Resumes automatically');
  await expect(page.locator('main')).not.toContainText('BLERBZ');

  // "Why is the GPU busy?" from the command bar. The integration is off, so the answer is about this machine.
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Why is the GPU busy?');
  await bar.press('Enter');
  const result = page.getByRole('region', { name: 'Command result' });
  await expect(result).toBeVisible({ timeout: 10_000 });
  await expect(result).not.toContainText('BLERBZ');
  await expect(result).toContainText(/GPU/);
});

test('pause a batch job and resume it (Jobs, §30, §31)', async ({ page }) => {
  await page.goto('/jobs');
  await waitReady(page);
  const pause = page.getByRole('button', { name: /^Pause: / }).first();
  await expect(pause).toBeVisible();
  const title = ((await pause.getAttribute('aria-label')) ?? '').replace(/^Pause: /, '');
  await pause.click();
  // Routine, safe: no confirmation; the row updates and offers Resume.
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const resume = page.getByRole('button', { name: `Resume: ${title}` });
  await expect(resume).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/paused/i).first()).toBeVisible();
  await resume.click();
  await expect(page.getByRole('button', { name: `Pause: ${title}` })).toBeVisible({ timeout: 10_000 });
});

test('check for better models and see progress, then a summary (§28)', async ({ page }) => {
  await page.goto('/models');
  await waitReady(page);
  await page.getByRole('button', { name: /Check for better models/i }).click();
  await expect(page.getByText('Checking for better models…')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('list', { name: 'Discovery stages' }).first()).toBeVisible();
  for (const stage of ['Discovering', 'Filtering', 'Evaluating', 'Benchmarking'])
    await expect(page.getByRole('list', { name: 'Discovery stages' }).first()).toContainText(stage);
  // The fake run finishes about 6 s later and reports a summary instead of the running title.
  await expect(page.getByText('Checking for better models…')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.locator('main')).toContainText(/upgrade|candidate|No better/i);
  await expectHumanText(page, 'models after a check');
});

test('approve a review from Home and it leaves the queue (§40)', async ({ page }) => {
  await page.goto('/');
  await waitReady(page);
  const card = page.locator('article.lz-approval').first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  // The card states action, why and impact before any button is pressed.
  for (const label of ['Action', 'Why', 'Impact']) await expect(card.getByText(label, { exact: true })).toBeVisible();
  const title = (await card.locator('.lz-card-title').innerText()).trim();
  await card.getByRole('group', { name: /^Answer:/ }).getByRole('button').first().click();
  await expect(page.getByText('Answer recorded')).toBeVisible({ timeout: 10_000 });
  await page.goto('/agents');
  await waitReady(page);
  await expect(page.locator('article.lz-approval', { hasText: title }).getByRole('group', { name: /^Answer:/ })).toHaveCount(0);
});

test('connect a phone, ask from it, and continue on the desktop (§15–17, §104, §105)', async ({ page, browser, baseURL }) => {
  test.setTimeout(120_000);
  // Desktop: Connect a phone → Start pairing → QR + address.
  await page.goto('/connect');
  await waitReady(page);
  const started = page.waitForResponse((r) => r.url().endsWith('/api/pair/start') && r.request().method() === 'POST');
  await page.getByRole('button', { name: /^Start pairing/ }).click();
  const pairing = await (await started).json();
  await expect(page.getByRole('img', { name: 'QR code to pair a phone with Labzilla' })).toBeVisible();
  expect(pairing.url).toContain('/pair#');
  const fragment = String(pairing.url).slice(String(pairing.url).indexOf('#'));

  // Phone: a separate browser context (no session), phone-sized with an iPhone user agent.
  const { defaultBrowserType: _ignored, ...iphone } = devices['iPhone 13'];
  const phoneCtx = await browser.newContext({ ...iphone, baseURL, colorScheme: 'dark', reducedMotion: 'reduce' });
  try {
    const phone = await phoneCtx.newPage();
    await phone.goto(`/pair${fragment}`);
    await expect(phone.getByRole('heading', { name: 'Name this device' })).toBeVisible();
    await phone.getByLabel('Device name').fill('Test phone');
    await phone.getByRole('button', { name: 'Pair this device' }).click();
    await expect(phone.getByRole('heading', { name: 'Confirm this code on your desktop' })).toBeVisible();
    const phoneCode = (await phone.locator('.lz-pair-code').innerText()).trim();
    expect(phoneCode).toMatch(/^\d{6}$/);

    // Desktop sees the claim live, with the same code, and approves.
    await expect(page.getByRole('heading', { name: /“Test phone” wants to connect/ })).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.lz-pair-code')).toHaveText(phoneCode);
    await page.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByText(/is paired/).first()).toBeVisible();
    await expect(page.getByRole('list', { name: 'Paired devices' })).toContainText('Test phone');

    // Phone lands on the Mobile Gateway home, signed in.
    await expect(phone).toHaveURL(/\/$/, { timeout: 15_000 });
    await expect(phone.getByText('This device is paired')).toBeVisible();
    await expect(phone.getByRole('heading', { name: 'LABZILLA' })).toBeVisible();
    await expect(phone.locator('.lz-bottomnav')).toBeVisible();

    // §104: tap Code → "Review this code snippet" + pasted code → Send → streamed answer.
    await phone.getByRole('link', { name: 'Code' }).click();
    await expect(phone).toHaveURL(/\/ask/);
    const box = phone.getByRole('textbox', { name: 'What do you want to do?' });
    await expect(box).toBeFocused();
    await box.fill('Review this code snippet\n\n');
    const code = 'def add(a, b):\n    return a - b\n';
    await box.evaluate((el, text) => {
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
      if (el.dispatchEvent(ev)) {
        // Not intercepted (text paste): the browser would insert it; do the same.
        const ta = el as HTMLTextAreaElement;
        ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }, code);
    await expect(box).toHaveValue(/return a - b/);
    await phone.getByRole('button', { name: /^Send/ }).tap();
    await expect(phone).toHaveURL(/\/ask\/[^/?]+/, { timeout: 15_000 });
    await expect(phone.locator('.lz-caret')).toBeVisible({ timeout: 15_000 });
    await expect(phone.getByText(/^Handled by local\//)).toBeVisible({ timeout: 45_000 });
    const threadPath = new URL(phone.url()).pathname;

    // §105: the desktop opens the same task — no copy/paste.
    await page.goto(threadPath);
    await waitReady(page);
    const thread = page.getByRole('region', { name: 'Conversation' });
    await expect(thread).toContainText('Review this code snippet');
    await expect(thread).toContainText('return a - b');
    await expect(thread.getByText(/^Handled by local\//)).toBeVisible();
    await expectHumanText(phone, 'phone ask');
  } finally {
    await phoneCtx.close();
  }
});

test('phone Home prompt resolves commands like the command bar, and pasted code keeps its lines (§7, §12, §104)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await waitReady(page);
  const prompt = page.getByRole('textbox', { name: 'Ask Labzilla' });
  await prompt.fill('Why is the GPU busy?');
  await prompt.press('Enter');
  // A snapshot answer in a sheet, not a model-generated guess in Ask.
  const sheet = page.getByRole('dialog', { name: 'Labzilla' });
  await expect(sheet).toBeVisible({ timeout: 10_000 });
  await expect(sheet).not.toContainText('BLERBZ');
  await expect(sheet).toContainText(/GPU/);
  await expect(page).toHaveURL(/\/$/);
  await page.keyboard.press('Escape');

  // Multi-line paste moves the draft into Ask's composer with its line breaks, unsent.
  await prompt.fill('Review this code snippet');
  await prompt.evaluate((el) => {
    const dt = new DataTransfer();
    dt.setData('text/plain', 'def sub(a, b):\n    return a - b\n');
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  });
  await expect(page).toHaveURL(/\/ask/);
  const box = page.getByRole('textbox', { name: 'What do you want to do?' });
  await expect(box).toHaveValue('Review this code snippet\n\ndef sub(a, b):\n    return a - b\n');
});

test('failed work explains itself in words, without protocol codes (§81)', async ({ page }) => {
  // The fake world has a failed discovery run (upstream: "rate limited (HTTP 429)") and a failed batch job.
  for (const p of ['/jobs', '/models/discovery', '/agents']) {
    await page.goto(p);
    await waitReady(page);
    await expect(page.locator('main')).toContainText(/fail/i);
    await expectHumanText(page, p);
  }
  await page.goto('/jobs');
  await waitReady(page);
  await expect(page.locator('main')).toContainText('Hugging Face API rate limited');
});

test('an outage reads as a human message with Retry, never a cryptic error (§62, §81)', async ({ page, context }) => {
  await setScenario('offline');
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: /unreachable|unavailable|offline/i })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: /Retry/ }).first()).toBeVisible();
  await expectHumanText(page, 'home (offline upstreams)');
  for (const p of ['/models', '/jobs', '/system/services', '/ask']) {
    await page.goto(p);
    await expect(page.locator('main h1').first()).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(500);
    await expectHumanText(page, `${p} (offline upstreams)`);
  }
  // No healthy-looking service while every upstream is down.
  await page.goto('/system/services');
  await waitReady(page);
  await expect(page.locator('main')).toContainText(/Not answering/);

  // The browser itself loses the network: the §62 banner with Retry; it clears once Labzilla answers again.
  await setScenario('healthy');
  await page.goto('/');
  await waitReady(page);
  await context.setOffline(true);
  const banner = page.getByRole('status').filter({ hasText: 'Labzilla unavailable' });
  await expect(banner).toBeVisible({ timeout: 20_000 });
  await expect(banner).toContainText('Not connected to your local network');
  await expect(banner.getByRole('button', { name: 'Retry' })).toBeVisible();
  await expectHumanText(page, 'browser offline');
  // Retry while still offline is honest: the banner stays.
  await banner.getByRole('button', { name: 'Retry' }).click();
  await expect(banner).toBeVisible();
  await expect(page.locator('main h1').first()).toBeVisible(); // what was loaded stays on screen (§60)
  // Back online: Retry (or the live connection on its own) clears it.
  await context.setOffline(false);
  await banner.getByRole('button', { name: 'Retry' }).click({ timeout: 3_000 }).catch(() => undefined);
  await expect(banner).toBeHidden({ timeout: 15_000 });
});

/** Prompts the fake gateway answers slowly (~12 s), so another device can watch the answer grow. */
const LONG = '[long] Tell me a story about a lighthouse keeper and her cat';

test('Ask threads are live on every open console: new thread, streaming, rename and delete (§67–§70)', async ({ page, browser, baseURL }) => {
  test.setTimeout(120_000);
  // Context B: the same owner in another browser, already sitting on Ask with History open beside it.
  const other = await browser.newContext({ storageState: STATE, baseURL, viewport: { width: 1440, height: 900 }, colorScheme: 'dark', reducedMotion: 'reduce' });
  try {
    const b = await other.newPage();
    await b.goto('/ask');
    await waitReady(b);
    const historyB = b.getByRole('complementary', { name: 'History' });
    await expect(historyB).toBeVisible();

    // Context A starts a conversation and it streams.
    await page.goto('/ask');
    await waitReady(page);
    await sendPrompt(page, LONG);
    await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
    const threadPath = new URL(page.url()).pathname;
    const id = decodeURIComponent(threadPath.split('/').pop()!);

    // B: the new row appears without a reload, marked as answering.
    const rowB = historyB.locator(`a.ask-hrow-link[data-id="${id}"]`);
    await expect(rowB).toBeVisible({ timeout: 10_000 });
    await expect(rowB).toContainText('Answering…');
    await expect(rowB).toContainText('lighthouse keeper');

    // B opens it mid-answer and watches the partial answer grow live, then the receipt arrives.
    await rowB.click();
    await expect(b).toHaveURL(new RegExp(`${threadPath}$`));
    await expect(rowB).toHaveAttribute('aria-current', 'page');
    const answerB = b.getByRole('region', { name: 'Conversation' }).locator('article').last();
    await expect(answerB).toContainText('Following an answer that started on another device', { timeout: 15_000 });
    await expect(answerB).toContainText('synthetic reply', { timeout: 15_000 });
    const early = (await answerB.innerText()).length;
    await expect.poll(async () => (await answerB.innerText()).length, { timeout: 15_000 }).toBeGreaterThan(early + 40);
    await expect(b.getByText(/^Handled by local\//)).toBeVisible({ timeout: 45_000 });
    await expect(rowB).not.toContainText('Answering…');
    await expect(page.getByText(/^Handled by local\//)).toBeVisible({ timeout: 15_000 });

    // Rename on B (keyboard: F2, type, Enter) shows on A, in the list and the open conversation's title.
    await rowB.focus();
    await b.keyboard.press('F2');
    const field = historyB.getByRole('textbox', { name: 'Conversation name' });
    await expect(field).toBeFocused();
    await field.fill('Lighthouse story');
    await field.press('Enter');
    const historyA = page.getByRole('complementary', { name: 'History' });
    await expect(historyA.locator(`a.ask-hrow-link[data-id="${id}"]`)).toContainText('Lighthouse story', { timeout: 10_000 });
    await expect(page.locator('main h1')).toContainText('Lighthouse story');

    // Delete on A asks first (destructive); B, which has it open, leaves it and its row disappears.
    const liA = historyA.locator('li', { has: page.locator(`a[data-id="${id}"]`) });
    await liA.hover();
    await liA.getByRole('button', { name: 'Delete “Lighthouse story”' }).click();
    const confirm = page.getByRole('alertdialog', { name: 'Delete this conversation?' });
    await expect(confirm).toBeVisible();
    await confirm.getByRole('button', { name: 'Delete' }).click();
    await expect(page).toHaveURL(/\/ask$/);
    await expect(rowB).toHaveCount(0, { timeout: 10_000 });
    await expect(b).toHaveURL(/\/ask$/, { timeout: 10_000 });
    await expect(b.getByText('This conversation was deleted')).toBeVisible();
    await expectHumanText(b, 'ask live history');
  } finally {
    await other.close();
  }
});

test('the phone opens History in one tap and recovers what it missed after a dropped connection (§62, §70)', async ({ page, browser, baseURL }) => {
  test.setTimeout(90_000);
  const phoneCtx = await browser.newContext({ storageState: STATE, baseURL, viewport: { width: 390, height: 844 }, hasTouch: true, colorScheme: 'dark', reducedMotion: 'reduce' });
  try {
    const phone = await phoneCtx.newPage();
    await phone.goto('/ask');
    await waitReady(phone);
    await phone.getByRole('button', { name: 'History' }).tap();
    const sheet = phone.getByRole('dialog', { name: 'History' });
    await expect(sheet).toBeVisible();
    // The phone's live connection drops (with History open); meanwhile the desktop starts a conversation.
    await phoneCtx.setOffline(true);
    await page.goto('/ask');
    await waitReady(page);
    await sendPrompt(page, `${LONG} while the phone is offline`);
    await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
    const id = decodeURIComponent(new URL(page.url()).pathname.split('/').pop()!);
    await expect(sheet.locator(`a.ask-hrow-link[data-id="${id}"]`)).toHaveCount(0);
    // Back online: the reconnect refetches, so the open sheet shows the conversation it never got an event for.
    await phoneCtx.setOffline(false);
    await expect(sheet.locator(`a.ask-hrow-link[data-id="${id}"]`)).toBeVisible({ timeout: 30_000 });
    await sheet.locator(`a.ask-hrow-link[data-id="${id}"]`).tap();
    await expect(sheet).toBeHidden();
    await expect(phone.getByRole('region', { name: 'Conversation' })).toContainText('while the phone is offline');
  } finally {
    await phoneCtx.close();
  }
});

/** A 1600×1200 PNG made in the page (no binary fixture in the repo). */
async function testImage(page: Page): Promise<{ name: string; mimeType: string; buffer: Buffer }> {
  const b64 = await page.evaluate(() => {
    const c = document.createElement('canvas');
    c.width = 1600;
    c.height = 1200;
    const g = c.getContext('2d')!;
    const grad = g.createLinearGradient(0, 0, 1600, 1200);
    grad.addColorStop(0, '#0b6');
    grad.addColorStop(1, '#36f');
    g.fillStyle = grad;
    g.fillRect(0, 0, 1600, 1200);
    g.fillStyle = '#fff';
    g.font = '120px sans-serif';
    g.fillText('TODO: ship vision', 120, 640);
    return c.toDataURL('image/png').split(',')[1]!;
  });
  return { name: 'whiteboard.png', mimeType: 'image/png', buffer: Buffer.from(b64, 'base64') };
}

/** Install or remove the fake vision model, then wait until the console's poller has seen it (≤ one 30 s
 *  routing poll), so the page under test agrees with the world. */
async function setVision(page: Page, installed: boolean) {
  const r = await fetch(`${FAKE}/__vision`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ installed }) });
  if (!r.ok) throw new Error(`vision toggle: ${r.status}`);
  await expect
    .poll(async () => {
      const caps = await (await page.request.get('/api/ai/capabilities')).json();
      return caps.modes.find((m: { mode: string; available: boolean }) => m.mode === 'vision')?.available;
    }, { timeout: 60_000, intervals: [2000], message: `console sees vision installed=${installed}` })
    .toBe(installed);
}

test('vision in Ask: honest without a vision model, then Auto switches to Vision and the image is never kept (§10, §20)', async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto('/ask');
  await waitReady(page);
  await setVision(page, false);
  const img = await testImage(page);
  // No vision model: the image is shown, the note says why it can't be read and links to a vision check.
  await page.locator('.ask-composer input[type="file"]:not([capture])').setInputFiles(img);
  const thumbs = page.getByRole('list', { name: 'Attached images' });
  await expect(thumbs.getByRole('img', { name: 'whiteboard.png' })).toBeVisible();
  await expect(thumbs).toContainText('1024×768');                       // downscaled in the browser
  await expect(page.getByText('No local vision model is installed yet')).toBeVisible();
  await expect(page.getByRole('button', { name: /^Send/ })).toBeDisabled();
  const link = page.getByRole('link', { name: 'Models › Check for better models (Vision)' });
  await expect(link).toHaveAttribute('href', '/models/discovery?category=vision');
  await link.click();
  await waitReady(page);
  await expect(page.getByRole('checkbox', { name: 'Vision' })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'Every kind' })).not.toBeChecked();

  // A vision model is installed; once Labzilla sees it, attaching in Auto switches to Vision (with Undo).
  await setVision(page, true);
  await page.goto('/ask');
  await waitReady(page);
  await page.locator('.ask-composer input[type="file"]:not([capture])').setInputFiles(img);
  await expect(page.getByText('Switched to Vision to read the image.')).toBeVisible();
  const mode = page.getByRole('combobox', { name: 'Mode' });
  await expect(mode).toHaveValue('vision');
  await page.getByRole('button', { name: 'Undo' }).click();
  await expect(mode).toHaveValue('auto');                               // Auto also routes images to vision
  await mode.selectOption('vision');
  await page.getByRole('textbox', { name: 'What do you want to do?' }).fill('What is on this whiteboard?');
  await page.getByRole('button', { name: /^Send/ }).click();
  await expect(page).toHaveURL(/\/ask\/[^/]+$/, { timeout: 15_000 });
  await expect(page.getByText(/^Handled by local\/vision/)).toBeVisible({ timeout: 45_000 });
  const files = page.getByRole('list', { name: 'Files' });
  await expect(files).toContainText('whiteboard.png');
  await expect(files).toContainText('image not kept');
  // After a reload the server copy says the same: the image itself was never stored.
  await page.reload();
  await waitReady(page);
  await expect(page.getByRole('list', { name: 'Files' })).toContainText('1024×768');
  await expect(page.getByRole('list', { name: 'Files' })).toContainText('image not kept');
  await expectHumanText(page, 'ask vision');
});

test('"Check for better vision models" from the command bar, and Find a vision model on Models (§8, §28)', async ({ page }) => {
  test.setTimeout(150_000);
  await page.goto('/models');
  await setVision(page, false);
  await page.reload();
  await waitReady(page);
  const bar = page.locator('.lz-commandbar input');
  await bar.fill('Check for better vision models');
  await bar.press('Enter');
  const result = page.getByRole('region', { name: 'Command result' });
  await expect(result.getByRole('button', { name: /Check for better vision models/ })).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Find a vision model' }).click();
  await expect(page.getByText('Checking for better models…')).toBeVisible({ timeout: 10_000 });
  // The fake run finishes about 6 s later and shortlists a vision model (with its image projector).
  await expect(page.getByText('Checking for better models…')).toHaveCount(0, { timeout: 30_000 });
  // Candidates come from the model registry, which Labzilla re-reads every 30 s.
  await expect(page.getByRole('list', { name: 'Model candidates' })).toContainText(/Gemma/i, { timeout: 45_000 });
  await expectHumanText(page, 'models after a vision check');
});
