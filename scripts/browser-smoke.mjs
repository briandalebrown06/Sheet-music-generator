/**
 * Integration smoke test against a running API, worker and Vite dev server.
 * Use a disposable DATA_DIR: this intentionally creates a clearly labelled
 * development fixture project. No test data is injected into transcription.
 *
 * PLAYWRIGHT_MODULE can point at a locally installed playwright module entry.
 * Optional: APP_URL, API_URL, CHROME_CHANNEL, SMOKE_ARTIFACT_DIR.
 */
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const appUrl = process.env.APP_URL || 'http://127.0.0.1:5173';
const apiUrl = process.env.API_URL || 'http://127.0.0.1:8000';
const output = process.env.SMOKE_ARTIFACT_DIR || '/tmp/scorecraft-browser-smoke';
await mkdir(output, { recursive: true });

// A one-second 440 Hz tone, generated only as an upload/decode test fixture.
// This is audio input, not a hardcoded or fabricated transcription result.
function testTone() {
  const rate = 22050;
  const samples = rate;
  const bytes = Buffer.alloc(44 + samples * 2);
  bytes.write('RIFF', 0); bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write('WAVEfmt ', 8); bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(rate, 24); bytes.writeUInt32LE(rate * 2, 28);
  bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36); bytes.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) {
    const envelope = Math.min(1, i / 220, (samples - i) / 220);
    bytes.writeInt16LE(Math.round(10000 * envelope * Math.sin(2 * Math.PI * 440 * i / rate)), 44 + 2 * i);
  }
  return bytes;
}

const healthResponse = await fetch(`${apiUrl}/api/health`);
assert.equal(healthResponse.status, 200, 'API health responds');
const health = await healthResponse.json();
assert.equal(health.status, 'ok');
const rejectedOrigin = await fetch(`${apiUrl}/api/projects`, {
  method: 'POST', headers: { Origin: 'https://untrusted.example' }, body: new FormData(),
});
assert.equal(rejectedOrigin.status, 403, 'untrusted websites cannot mutate local projects');
const invalidForm = new FormData();
invalidForm.set('file', new Blob(['not audio'], { type: 'text/plain' }), 'fixture.txt');
invalidForm.set('title', '[Development fixture] Invalid text');
invalidForm.set('source', 'upload');
const invalidResponse = await fetch(`${apiUrl}/api/projects`, { method: 'POST', body: invalidForm });
assert.ok(invalidResponse.status >= 400 && invalidResponse.status < 500, 'non-audio upload is rejected');

const browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome', args: ['--use-fake-device-for-media-stream'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const pageErrors = [];
let failureRetryChecked = false;
const externalRequests = [];
page.on('request', request => {
  const url = new URL(request.url());
  if (['http:', 'https:'].includes(url.protocol) && ![new URL(appUrl).origin, new URL(apiUrl).origin].includes(url.origin)) externalRequests.push(url.href);
});
page.on('pageerror', error => pageErrors.push(error.message));
try {
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  await page.getByRole('heading', { level: 1 }).waitFor();
  await page.getByRole('tab', { name: 'Upload audio' }).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('tab', { name: 'Record audio' }).getAttribute('aria-selected'), 'true', 'source tabs support ArrowRight');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.getByRole('tab', { name: 'Upload audio' }).getAttribute('aria-selected'), 'true', 'source tabs support ArrowLeft');
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true, animations: 'disabled' });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'desktop has no horizontal overflow');

  // Upload selectors are resolved semantically against the implemented UI.
  const fileInput = page.locator('input[type=file]');
  assert.equal(await fileInput.count(), 1, 'one upload input is present');
  await fileInput.setInputFiles({ name: 'development-fixture-invalid.txt', mimeType: 'text/plain', buffer: Buffer.from('not audio') });
  await page.getByRole('alert').filter({ hasText: /choose.*audio file/i }).waitFor();
  await fileInput.setInputFiles({ name: 'development-fixture-tone.wav', mimeType: 'audio/wav', buffer: testTone() });
  const titleInput = page.getByLabel(/project name|project title|title/i);
  if (await titleInput.count()) await titleInput.fill('[Development fixture] Browser smoke tone');
  const creation = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: /create transcription|create project|start transcription/i }).click();
  const response = await creation;
  assert.equal(response.status(), 201, 'upload creates a project');
  const project = await response.json();
  assert.ok(project.id, 'project has persistent identifier');
  await page.waitForURL(`**#/projects/${project.id}`);
  await page.getByRole('heading', { level: 1, name: '[Development fixture] Browser smoke tone', exact: true }).waitFor();
  await page.reload({ waitUntil: 'networkidle' });
  const persisted = await (await fetch(`${apiUrl}/api/projects/${project.id}`)).json();
  assert.equal(persisted.id, project.id, 'project survives browser reload');
  assert.equal(persisted.original_filename, 'development-fixture-tone.wav');
  assert.ok(Array.isArray(persisted.jobs) && persisted.jobs.length > 0, 'project has a persisted processing job');
  assert.equal(await page.locator('audio[aria-label="Original recording"]').count(), 1, 'project exposes original audio playback');
  const audioResponse = await fetch(`${apiUrl}/api/projects/${project.id}/audio`);
  assert.equal(audioResponse.status, 200, 'original audio is retrievable');
  assert.ok((await audioResponse.arrayBuffer()).byteLength > 44);
  if (health.worker.online && (!health.capabilities.ffmpeg || !health.capabilities.transcription)) {
    await page.getByRole('button', { name: 'Retry transcription', exact: true }).waitFor({ timeout: 15000 });
    await page.getByRole('alert').filter({ hasText: /FFmpeg|Basic Pitch|supported.*environment/i }).waitFor();
    let failed = await (await fetch(`${apiUrl}/api/projects/${project.id}`)).json();
    assert.equal(failed.status, 'failed');
    assert.equal(failed.notes, null, 'failed analysis does not fabricate notes');
    assert.deepEqual(failed.exports, [], 'failed analysis does not fabricate exports');
    await page.getByRole('button', { name: 'Retry transcription', exact: true }).click();
    await page.getByText('Processing history · 2 attempts').waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: 'Retry transcription', exact: true }).waitFor({ timeout: 15000 });
    failed = await (await fetch(`${apiUrl}/api/projects/${project.id}`)).json();
    assert.equal(failed.jobs.length, 2, 'retry retains previous job history');
    failureRetryChecked = true;
  }
  await page.screenshot({ path: path.join(output, 'project-desktop.png'), fullPage: true, animations: 'disabled' });

  const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobilePage = await mobileContext.newPage();
  mobilePage.on('pageerror', error => pageErrors.push(error.message));
  await mobilePage.goto(`${appUrl}/#/projects/${project.id}`, { waitUntil: 'networkidle' });
  assert.equal(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'mobile project page has no horizontal overflow');
  await mobilePage.screenshot({ path: path.join(output, 'project-mobile.png'), fullPage: true, animations: 'disabled' });
  await mobilePage.goto(appUrl, { waitUntil: 'networkidle' });
  assert.equal(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'mobile upload page has no horizontal overflow');
  await mobilePage.screenshot({ path: path.join(output, 'upload-mobile.png'), fullPage: true, animations: 'disabled' });

  // Record through Chromium's explicit test microphone. This exercises the real
  // MediaRecorder capture/stop/preview/upload flow without accessing a user mic.
  const recordingContext = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 900 } });
  const recordingPage = await recordingContext.newPage();
  recordingPage.on('pageerror', error => pageErrors.push(error.message));
  await recordingPage.goto(appUrl, { waitUntil: 'networkidle' });
  await recordingPage.getByRole('tab', { name: 'Record audio' }).click();
  await recordingPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await recordingPage.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
  await recordingPage.waitForTimeout(1200);
  await recordingPage.getByRole('button', { name: 'Stop recording', exact: true }).click();
  await recordingPage.getByLabel('Project name').fill('[Development fixture] Browser test microphone');
  assert.equal(await recordingPage.locator('audio').count(), 1, 'recording has a preview');
  const micCreation = recordingPage.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await recordingPage.getByRole('button', { name: 'Create transcription', exact: true }).click();
  const micResponse = await micCreation;
  assert.equal(micResponse.status(), 201, 'microphone capture uploads successfully');
  assert.equal((await micResponse.json()).source, 'microphone');
  await recordingContext.close();

  // Deterministically exercise denied microphone permission; no recording is fabricated.
  await mobilePage.addInitScript(() => {
    if (navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException('Permission denied by smoke test', 'NotAllowedError');
      };
    }
  });
  await mobilePage.reload({ waitUntil: 'networkidle' });
  await mobilePage.getByRole('tab', { name: 'Record audio' }).click();
  await mobilePage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await mobilePage.getByRole('alert').filter({ hasText: /microphone|permission|denied/i }).waitFor({ timeout: 5000 });
  assert.deepEqual(pageErrors, [], 'browser does not raise unhandled exceptions');
  assert.deepEqual(externalRequests, [], 'app assets and audio requests stay local');
  console.log(JSON.stringify({ passed: true, projectId: project.id, health, artifacts: output, checks: ['health', 'keyboard source tabs', 'local asset requests', 'origin guard', 'invalid upload', 'WAV upload', 'persisted project/job', 'audio playback resource', 'desktop/mobile layout', 'real MediaRecorder with browser test microphone', 'microphone denial', ...(failureRetryChecked ? ['actionable analysis failure and retry history'] : []), 'no uncaught browser errors'] }, null, 2));
} finally {
  await browser.close();
}
