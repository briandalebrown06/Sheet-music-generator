/**
 * Real transcription + editing integration checks. Isolated API/worker/storage.
 * Requires actual Basic Pitch analysis environment, local FFmpeg, and Chrome.
 * Generated tones are test INPUTS; model results are never substituted.
 * TRANSCRIPTION_TEST_PYTHON defaults to the repository .venv/bin/python.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { createServer } = await import(path.join(root, 'frontend/node_modules/vite/dist/node/index.js'));
const python = process.env.TRANSCRIPTION_TEST_PYTHON || path.join(root, '.venv/bin/python');
const ffmpeg = process.env.FFMPEG_BINARY || path.join(root, '.runtime/bin/ffmpeg');
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-transcription-'));
const data = path.join(output, 'data');
await mkdir(output, { recursive: true });
const env = { ...process.env, DATA_DIR: data, AUDIO_TEST_DATA_DIR: data, FFMPEG_BINARY: ffmpeg, BASIC_PITCH_RUNTIME: 'onnx', MAX_JOB_ATTEMPTS: '3' };
const appUrl = 'http://127.0.0.1:5174';
const apiUrl = 'http://127.0.0.1:8001';
const children = [], logs = [], pageErrors = [], observedStages = new Set();
let browser, server, assertions = 0;
function check(value, label) { assertions++; assert.ok(value, label); }
function equal(value, expected, label) { assertions++; assert.deepEqual(value, expected, label); }
function start(args) {
  const child = spawn(python, args, { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', x => logs.push(x.toString())); child.stderr.on('data', x => logs.push(x.toString()));
  children.push(child); return child;
}
async function until(fn, label, timeout = 30000) {
  const expires = Date.now() + timeout;
  while (Date.now() < expires) {
    const result = await fn().catch(() => false);
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out: ${label}`);
}
async function getProject(id) { return (await fetch(`${apiUrl}/api/projects/${id}`)).json(); }
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
function testAudio() {
  const rate = 22050, count = 6 * rate;
  const bytes = Buffer.alloc(44 + count * 2);
  bytes.write('RIFF', 0); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(rate, 24); bytes.writeUInt32LE(rate * 2, 28); bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36); bytes.writeUInt32LE(count * 2, 40);
  for (let i = 0; i < count; i++) {
    const t = i / rate, local = t % 3, hz = t < 3 ? 261.625565 : 329.627557;
    const envelope = Math.max(0, Math.min(1, local / .03, (2.8 - local) / .08));
    const signal = [1, 2, 3].reduce((total, n) => total + Math.sin(2 * Math.PI * hz * n * t) / n, 0);
    bytes.writeInt16LE(Math.round(8000 * envelope * signal), 44 + i * 2);
  }
  return bytes;
}
async function upload(page, file, title) {
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name').fill(title);
  await page.getByText('Advanced note cleanup', { exact: true }).click();
  await page.getByLabel('Minimum note duration (seconds)', { exact: true }).fill('0.08');
  await page.getByLabel('Maximum fragment gap (seconds)', { exact: true }).fill('0.03');
  const response = page.waitForResponse(r => r.url().endsWith('/api/projects') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create transcription', exact: true }).click();
  const created = await response;
  equal(created.status(), 201, await created.text());
  const project = await created.json();
  await page.waitForURL(`**#/projects/${project.id}`);
  return project;
}
async function saveNotes(page) {
  const response = page.waitForResponse(r => r.url().endsWith('/review') && r.request().method() === 'PUT');
  await page.getByRole('button', { name: 'Save review', exact: true }).first().click();
  const result = await response;
  equal(result.status(), 200, await result.text());
  await page.getByRole('status').filter({ hasText: 'Review saved' }).waitFor();
  const saved = await result.json();
  equal(saved.score_state.source_notes_revision, saved.project.notes_revision, 'notes and score save atomically');
  return saved.project;
}

try {
  const file = path.join(output, 'development-fixture-two-tones.wav');
  await writeFile(file, testAudio());
  start(['-m', 'uvicorn', 'scripts.audio_test_server:app', '--host', '127.0.0.1', '--port', '8001']);
  await until(async () => (await fetch(`${apiUrl}/api/health`)).ok, 'test API startup');
  server = await createServer({ configFile: path.join(root, 'frontend/vite.config.ts'), root: path.join(root, 'frontend'), server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy: { '/api': { target: apiUrl, changeOrigin: false } } } });
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  console.log('Checking queued cancellation and bounded retries…');
  const canceled = await upload(page, file, '[Development fixture] canceled transcription');
  for (let attempt = 1; attempt <= 3; attempt++) {
    await page.getByRole('button', { name: 'Cancel transcription', exact: true }).click();
    await until(async () => (await getProject(canceled.id)).status === 'canceled', 'durable canceled status');
    await page.getByRole('heading', { name: 'Transcription canceled', exact: true }).waitFor();
    let value = await getProject(canceled.id);
    equal(value.jobs.length, attempt, 'one job per bounded attempt');
    equal(value.retries_remaining, 3 - attempt, 'retry budget consumed');
    equal(value.notes, null, 'cancel creates no fabricated notes');
    await page.reload({ waitUntil: 'networkidle' });
    equal((await getProject(canceled.id)).status, 'canceled', 'cancellation persists after refresh');
    if (attempt < 3) {
      await page.getByRole('button', { name: 'Retry transcription', exact: true }).click();
      await until(async () => (await getProject(canceled.id)).status === 'queued', 'retry becomes queued');
      await page.getByRole('button', { name: 'Cancel transcription', exact: true }).waitFor();
    }
  }
  check(await page.getByRole('button', { name: 'Retry transcription', exact: true }).isDisabled(), 'retry disabled after limit');
  equal((await fetch(`${apiUrl}/api/projects/${canceled.id}/retry`, { method: 'POST' })).status, 409, 'server enforces retry budget');

  console.log('Checking real transcription, artifacts and raw MIDI…');
  start(['-m', 'backend.app.worker']);
  await until(async () => (await (await fetch(`${apiUrl}/api/health`)).json()).worker.online, 'worker heartbeat');
  const project = await upload(page, file, '[Development fixture] editable real transcription');
  const completed = await until(async () => {
    const value = await getProject(project.id);
    if (value.jobs?.[0]?.stage) observedStages.add(value.jobs[0].stage);
    return ['completed', 'failed', 'canceled'].includes(value.status) ? value : false;
  }, 'real Basic Pitch completion', 180000);
  equal(completed.status, 'completed', JSON.stringify(completed.jobs));
  check(completed.notes.length > 0, 'real model produced notes');
  equal(completed.cleanup.minimum_note_seconds, .08, 'cleanup persisted');
  equal(completed.cleanup.merge_gap_seconds, .03, 'fragment gap persisted');
  for (const note of completed.notes) {
    check(typeof note.id === 'string' && note.id.length > 0, 'stable note ID');
    equal(note.provenance, 'detected', 'model notes provenance detected');
    equal(note.score_timing, null, 'future quantized timing is separate');
    check(note.part_id.length > 0 && note.original_timing !== null, 'note has part and original timing');
  }
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: 'Save review', exact: true }).first().waitFor();
  const rawMidi = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${project.id}/exports/raw-midi`)).arrayBuffer());
  equal(rawMidi.subarray(0, 4).toString(), 'MThd', 'real raw-performance MIDI has MIDI header');
  const rawEvents = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${project.id}/artifacts/raw-note-events`)).arrayBuffer());
  const arrays = await fetch(`${apiUrl}/api/projects/${project.id}/artifacts/raw-model-output`);
  equal(arrays.status, 200, 'raw model arrays available');
  check((await arrays.arrayBuffer()).byteLength > 100, 'raw arrays nonempty');
  await page.getByText('Preserved analysis files', { exact: true }).click();
  check(await page.getByRole('link', { name: 'Raw model arrays (.npz)', exact: true }).isVisible(), 'artifact download UI present');
  check((await page.locator('.artifact-files').innerText()).includes('not calibrated accuracy probabilities'), 'score is not labeled calibrated accuracy');

  console.log('Checking persistent edits, manual notes and raw immutability…');
  const first = completed.notes[0];
  const changedPitch = first.pitch === 127 ? 126 : first.pitch + 1;
  await page.getByLabel('Note 1 MIDI pitch', { exact: true }).fill(String(changedPitch));
  const edited = await saveNotes(page);
  const editedFirst = edited.notes.find(note => note.id === first.id);
  equal(editedFirst.pitch, changedPitch, 'pitch edit saved');
  equal(editedFirst.id, first.id, 'ID retained through edit');
  equal(editedFirst.provenance, 'edited', 'edited provenance saved');
  equal(editedFirst.original_timing, first.original_timing, 'original timing immutable');
  await page.reload({ waitUntil: 'networkidle' });
  equal(await page.getByLabel('Note 1 MIDI pitch', { exact: true }).inputValue(), String(changedPitch), 'edit survives browser refresh');
  await page.getByRole('button', { name: 'Add note', exact: true }).click();
  const newIndex = edited.notes.length + 1;
  await page.getByLabel(`Note ${newIndex} MIDI pitch`, { exact: true }).fill('67');
  await page.getByLabel(`Note ${newIndex} start seconds`, { exact: true }).fill('0');
  await page.getByLabel(`Note ${newIndex} duration seconds`, { exact: true }).fill('0.25');
  await page.getByLabel(`Note ${newIndex} part`, { exact: true }).fill('part-2');
  const added = await saveNotes(page);
  const manual = added.notes.find(note => note.provenance === 'manually_added');
  check(manual && manual.id && manual.part_id === 'part-2', 'manual note has persistent ID and part');
  equal(manual.model_score, null, 'manual note has no invented model score');
  equal(manual.original_timing, null, 'manual note has no fabricated detection timing');
  await page.locator(`[data-note-id="${first.id}"]`).getByRole('button', { name: /^Remove note/ }).click();
  const removed = await saveNotes(page);
  check(!removed.notes.some(note => note.id === first.id), 'removed note no longer in editable representation');
  const afterRaw = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${project.id}/exports/raw-midi`)).arrayBuffer());
  const afterEvents = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${project.id}/artifacts/raw-note-events`)).arrayBuffer());
  equal(hash(afterRaw), hash(rawMidi), 'edits do not change raw-performance MIDI');
  equal(hash(afterEvents), hash(rawEvents), 'edits do not change raw detected events');
  const performance = await fetch(`${apiUrl}/api/projects/${project.id}/exports/midi`);
  equal(performance.status, 200, 'edited-performance MIDI downloadable');
  check(hash(Buffer.from(await performance.arrayBuffer())) !== hash(rawMidi), 'edited MIDI differs from preserved raw output');

  console.log('Checking revision conflict, retained draft and mobile layout…');
  await page.getByLabel('Note 1 MIDI pitch', { exact: true }).fill('66');
  const live = await getProject(project.id);
  const conflictWrite = await fetch(`${apiUrl}/api/projects/${project.id}/notes`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: live.notes_revision, notes: live.notes.map((note, i) => ({
      id: note.id, pitch: i === 0 ? 65 : note.pitch, start: note.start, end: note.end, velocity: note.velocity, part_id: note.part_id,
    })) }),
  });
  equal(conflictWrite.status, 200, 'concurrent revision saved');
  const conflicted = page.waitForResponse(r => r.url().endsWith('/review') && r.request().method() === 'PUT');
  await page.getByRole('button', { name: 'Save review', exact: true }).first().click();
  equal((await conflicted).status(), 409, 'stale revision rejected');
  equal(await page.getByLabel('Note 1 MIDI pitch', { exact: true }).inputValue(), '66', 'unsaved draft retained on conflict');
  await page.reload({ waitUntil: 'networkidle' });
  await until(async () => await page.getByLabel('Note 1 MIDI pitch', { exact: true }).inputValue() === '65', 'reload retrieves current revision');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(output, 'transcription-desktop.png'), fullPage: true });
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${appUrl}/#/projects/${project.id}`, { waitUntil: 'networkidle' });
  check(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout does not overflow viewport');
  await mobilePage.screenshot({ path: path.join(output, 'transcription-mobile.png'), fullPage: true });
  equal(pageErrors, [], 'no unhandled browser errors');
  const report = { passed: true, assertions, output, projectId: project.id, canceledProjectId: canceled.id, observedStages: [...observedStages], checks: ['queued cancellation and bounded retries', 'real model inference and raw artifacts', 'raw-performance MIDI', 'stable IDs and timing/provenance', 'edit/add/remove persistence', 'raw artifacts immutable under editing', 'revision conflict retains draft', 'desktop/mobile layout'], limitations: ['Chrome automation only; no physical devices or other browsers tested', 'synthetic audio is not a commercial-song accuracy benchmark'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser?.close(); await server?.close();
  for (const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child => new Promise(resolve => { if (child.exitCode !== null) resolve(); else { child.once('exit', resolve); setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000).unref(); } })));
  await writeFile(path.join(output, 'service.log'), logs.join(''));
}
