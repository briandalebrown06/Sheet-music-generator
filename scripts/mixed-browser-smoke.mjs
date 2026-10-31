/** Generated multitrack audio -> real separation -> selected part -> real pitch
 * inference -> review/edit/export. Uses disposable storage, never user projects. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { createServer } = await import(path.join(root, 'frontend/node_modules/vite/dist/node/index.js'));
const python = process.env.MIXED_TEST_PYTHON || path.join(root, '.venv/bin/python');
const ffmpeg = process.env.FFMPEG_BINARY || path.join(root, '.runtime/bin/ffmpeg');
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-mixed-'));
const data = path.join(output, 'data');
const fixtureDirectory = path.join(output, 'fixture');
await mkdir(output, { recursive: true });
const env = { ...process.env, DATA_DIR: data, AUDIO_TEST_DATA_DIR: data, FFMPEG_BINARY: ffmpeg, BASIC_PITCH_RUNTIME: 'onnx' };
const appUrl = 'http://127.0.0.1:5174', apiUrl = 'http://127.0.0.1:8001';
const children = [], logs = [], errors = [], iconErrors = [], seenStages = new Set();
let browser, server, assertions = 0;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const check = (value, label) => { assertions++; assert.ok(value, label); };
const equal = (value, expected, label) => { assertions++; assert.deepEqual(value, expected, label); };
function captureConsole(message) {
  if (message.type() !== 'error') return;
  const value = { message: message.text(), url: message.location().url };
  if (value.message.startsWith('Failed to load resource:') && new URL(value.url || appUrl).pathname === '/favicon.ico') iconErrors.push(value);
  else errors.push(value);
}
function start(args) {
  const child = spawn(python, args, { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', bytes => logs.push(bytes.toString()));
  child.stderr.on('data', bytes => logs.push(bytes.toString()));
  children.push(child); return child;
}
async function runPython(code, args = []) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-c', code, ...args], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '', err = '';
    child.stdout.on('data', bytes => out += bytes); child.stderr.on('data', bytes => err += bytes);
    child.once('exit', value => value === 0 ? resolve(out) : reject(new Error(`Python validation ${value}: ${err}`)));
  });
}
async function until(fn, label, timeout = 30000) {
  const expires = Date.now() + timeout;
  while (Date.now() < expires) {
    if (await fn()) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out: ${label}`);
}
async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json();
}
const getProject = id => getJSON(`${apiUrl}/api/projects/${id}`);
async function completed(id) {
  let project;
  await until(async () => {
    project = await getProject(id);
    if (project.jobs[0]?.stage) seenStages.add(project.jobs[0].stage);
    if (['failed', 'canceled'].includes(project.status)) throw new Error(`${project.status}: ${project.jobs[0]?.error}`);
    return project.status === 'completed';
  }, 'bounded real background processing', Number(process.env.MIXED_TEST_TIMEOUT_MS || 600000));
  return project;
}
async function rendered(page) {
  await until(async () => await page.getByTestId('score-renderer').locator('svg path').count() > 0, 'rendered canonical notation');
}

try {
  console.log('Generating a labeled multitrack fixture with known source parts…');
  await runPython("from pathlib import Path; from scripts.generate_multitrack_fixture import generate; generate(Path(__import__('sys').argv[1]))", [fixtureDirectory]);
  const fixture = path.join(fixtureDirectory, 'DEVELOPMENT-MULTITRACK.wav');
  const input = await readFile(fixture);
  start(['-m', 'uvicorn', 'scripts.audio_test_server:app', '--host', '127.0.0.1', '--port', '8001']);
  await until(async () => { try { return (await fetch(`${apiUrl}/api/health`)).ok; } catch { return false; } }, 'test API');
  start(['-m', 'backend.app.worker']);
  await until(async () => (await getJSON(`${apiUrl}/api/health`)).worker.online, 'worker heartbeat');
  const health = await getJSON(`${apiUrl}/api/health`);
  check(health.capabilities.separation, 'real separator and installed model available');
  server = await createServer({ configFile: path.join(root, 'frontend/vite.config.ts'), root: path.join(root, 'frontend'), server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy: { '/api': { target: apiUrl, changeOrigin: false } } } });
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message)); page.on('console', captureConsole);
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  equal(await page.getByRole('radio', { name: 'Solo voice or instrument', exact: true }).isChecked(), true, 'solo remains default');
  await page.getByRole('radio', { name: 'Full song / mixed recording', exact: true }).check();
  await page.locator('input[type=file]').setInputFiles(fixture);
  await page.getByLabel('Project name', { exact: true }).fill('[Development fixture] mixed-song review');
  await page.getByLabel('Clip start (seconds)', { exact: true }).fill('0.1');
  await page.getByLabel('Clip end (seconds)', { exact: true }).fill('11.9');
  const creation = page.waitForResponse(response => response.url().endsWith('/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Separate recording', exact: true }).click();
  const createdResponse = await creation;
  equal(createdResponse.status(), 201, await createdResponse.text());
  const created = await createdResponse.json();
  equal(created.input_mode, 'mixed', 'explicit mixed input persisted');
  console.log('Running actual source separation in the isolated worker…');
  const parent = await completed(created.id);
  equal(parent.notes, null, 'separation does not fabricate note events');
  equal(parent.clip_start_seconds, 0.1, 'selected original offset persisted');
  const source = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${parent.id}/audio`)).arrayBuffer());
  equal(hash(source), hash(input), 'original mixed recording is unchanged');
  await page.getByRole('region', { name: 'Separated parts', exact: true }).waitFor({ timeout: 30000 });
  const separated = await getJSON(`${apiUrl}/api/projects/${parent.id}/stems`);
  equal(separated.stems.map(stem => stem.category).sort(), ['bass', 'drums', 'other', 'vocals'], 'actual four model categories');
  check(separated.manifest && Object.keys(separated.manifest).length > 0, 'engine/settings manifest is persisted');
  const stemHashes = {};
  for (const stem of separated.stems) {
    check(Math.abs(stem.duration_seconds - 11.8) < 0.01, `${stem.category} retains aligned clip length`);
    const audio = page.getByLabel(`Audition ${stem.category} stem`, { exact: true });
    await audio.evaluate(element => element.play());
    await until(async () => await audio.evaluate(element => element.currentTime) > 0.15, `${stem.category} stem actually plays`);
    await audio.evaluate(element => element.pause());
    stemHashes[stem.category] = hash(Buffer.from(await (await fetch(`${apiUrl}/api/projects/${parent.id}/stems/${stem.category}/audio`)).arrayBuffer()));
  }
  check(await page.getByRole('button', { name: 'Transcribe drums stem', exact: true }).isDisabled(), 'drum transcription is disabled');
  const drums = await fetch(`${apiUrl}/api/projects/${parent.id}/stems/drums/transcribe`, { method: 'POST' });
  equal(drums.status, 422, 'backend refuses percussion-to-pitched transcription');
  check((await page.locator('[data-stem-category="other"]').innerText()).includes('not an isolated guitar or piano'), 'other category is not an instrument claim');
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByLabel('Audition bass stem', { exact: true }).waitFor();
  equal((await getJSON(`${apiUrl}/api/projects/${parent.id}/stems`)).stems.length, 4, 'stems survive reopening');
  await page.screenshot({ path: path.join(output, 'mixed-stems-desktop.png'), fullPage: true });
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobilePage = await mobile.newPage(); mobilePage.on('pageerror', error => errors.push(error.message)); mobilePage.on('console', captureConsole);
  await mobilePage.goto(`${appUrl}/#/projects/${parent.id}`, { waitUntil: 'networkidle' });
  await mobilePage.getByLabel('Audition bass stem', { exact: true }).waitFor();
  check(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mixed mobile layout has no page overflow');
  await mobilePage.screenshot({ path: path.join(output, 'mixed-stems-mobile.png'), fullPage: true });
  await mobile.close();
  console.log('Selecting the bass stem and running real pitched-line inference…');
  const selection = page.waitForResponse(response => response.url().endsWith('/stems/bass/transcribe') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Transcribe bass stem', exact: true }).click();
  const selectedResponse = await selection;
  equal(selectedResponse.status(), 201, await selectedResponse.text());
  const selected = await selectedResponse.json();
  const part = await completed(selected.id);
  equal(part.parent_project_id, parent.id, 'child records parent project');
  equal(part.stem_category, 'bass', 'child identifies actual selected stem');
  equal(part.source_origin_seconds, 0.1, 'child keeps source-to-song mapping');
  equal(part.transcription_profile, 'bass', 'bass-specific adapter selected');
  check(part.notes.length > 0, 'real bass inference produced reviewable notes');
  await rendered(page);
  check(await page.getByRole('link', { name: 'Back to mixed recording', exact: true }).isVisible(), 'child links to preserved mixed recording');
  const originalRaw = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${part.id}/exports/raw-midi`)).arrayBuffer());
  const originalNote = part.notes[0], pitch = Math.min(127, originalNote.pitch + 2);
  await page.getByLabel('Note 1 MIDI pitch', { exact: true }).fill(String(pitch));
  const saving = page.waitForResponse(response => response.url().endsWith('/review') && response.request().method() === 'PUT');
  await page.getByRole('button', { name: 'Save review', exact: true }).first().click();
  const savedResponse = await saving;
  equal(savedResponse.status(), 200, await savedResponse.text());
  const saved = await savedResponse.json();
  equal(saved.project.notes.find(note => note.id === originalNote.id).pitch, pitch, 'part pitch correction persisted');
  equal(saved.score_state.source_notes_revision, saved.project.notes_revision, 'part score matches edited notes revision');
  const base = `${apiUrl}/api/projects/${part.id}`;
  for (const format of ['musicxml', 'midi', 'pdf', 'json']) {
    const response = await fetch(`${base}/score/exports/${format}?revision=${saved.score_state.revision}`);
    equal(response.status, 200, `${format} saved part export succeeds`);
    equal(response.headers.get('x-score-revision'), String(saved.score_state.revision), `${format} identifies correct score revision`);
    await writeFile(path.join(output, `part-score.${format === 'midi' ? 'mid' : format}`), Buffer.from(await response.arrayBuffer()));
  }
  const parsed = JSON.parse(await runPython(`import json,sys
from music21 import converter
from pypdf import PdfReader
import pretty_midi
from fractions import Fraction
xml,midi,pdf,canonical,note_id,pitch=sys.argv[1:];pitch=int(pitch)
s=converter.parse(xml);m=pretty_midi.PrettyMIDI(midi);p=PdfReader(pdf);c=json.load(open(canonical));seconds=60/c['tempo']['bpm']
def f(x):return float(Fraction(x['numerator'],x['denominator']))
events=[(f(measure['start'])+f(event['offset']),f(event['duration'])) for part in c['parts'] for staff in part['staves'] for measure in staff['measures'] for voice in measure['voices'] for event in voice['events'] if note_id in event['source_note_ids'] and pitch in event['pitches']]
start=min(at for at,duration in events)*seconds;end=max(at+duration for at,duration in events)*seconds
print(json.dumps({'xml_edited_pitch':pitch in [q.midi for n in s.recurse().notes for q in n.pitches],'midi_edited_note':any(n.pitch==pitch and abs(n.start-start)<.01 and abs(n.end-end)<.01 for part in m.instruments for n in part.notes),'pdf_pages':len(p.pages),'pdf_width':float(p.pages[0].mediabox.width),'pdf_height':float(p.pages[0].mediabox.height)}))`, [path.join(output, 'part-score.musicxml'), path.join(output, 'part-score.mid'), path.join(output, 'part-score.pdf'), path.join(output, 'part-score.json'), originalNote.id, String(pitch)]));
  check(parsed.xml_edited_pitch, 'MusicXML reparses with edited pitch');
  check(parsed.midi_edited_note, 'MIDI contains edited pitch at canonical timing');
  check(parsed.pdf_pages > 0 && Math.abs(parsed.pdf_width - 595.276) < 1 && Math.abs(parsed.pdf_height - 841.89) < 1, 'part PDF parses as paginated A4');
  equal(hash(Buffer.from(await (await fetch(base + '/exports/raw-midi')).arrayBuffer())), hash(originalRaw), 'raw part MIDI remains immutable');
  await page.reload({ waitUntil: 'networkidle' }); await rendered(page);
  equal(await page.getByLabel('Note 1 MIDI pitch', { exact: true }).inputValue(), String(pitch), 'part reopens with correction intact');
  await page.screenshot({ path: path.join(output, 'mixed-part-review.png'), fullPage: true });
  await page.getByRole('link', { name: 'Back to mixed recording', exact: true }).click();
  await page.getByRole('link', { name: 'Open bass part project', exact: true }).waitFor();
  const repeated = await fetch(`${apiUrl}/api/projects/${parent.id}/stems/bass/transcribe`, { method: 'POST' });
  equal(repeated.status, 200, 'repeated selection reuses saved child');
  equal((await repeated.json()).id, part.id, 'repeated selection cannot overwrite edited part');
  for (const category of Object.keys(stemHashes)) equal(hash(Buffer.from(await (await fetch(`${apiUrl}/api/projects/${parent.id}/stems/${category}/audio`)).arrayBuffer())), stemHashes[category], `${category} source stem unchanged by editing`);
  equal(errors, [], 'no application console or uncaught errors');
  const report = { passed: true, assertions, output, parentProjectId: parent.id, partProjectId: part.id, detectedNotes: part.notes.length, actualStages: [...seenStages], parsed, optionalIconErrors: iconErrors,
    checks: ['solo remains default', 'explicit mixed mode', 'real multitrack separation', 'audition all actual stems', 'aligned nonzero clip offset', 'drums unsupported', 'persistent stem/part selection', 'real bass adapter', 'part editing and reopen', 'MusicXML MIDI PDF exports', 'raw source preservation', 'desktop and mobile layout'],
    limitations: ['Generated multitrack input; separation quality and pitch errors are evaluated separately against its references', 'Installed Chrome automation only; no physical audio-device listening or other browsers tested'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2));
} catch (error) {
  const page = browser?.contexts()[0]?.pages()[0];
  if (page) { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); await writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText().catch(() => String(error))); }
  console.error('Failure artifacts:', output); throw error;
} finally {
  await browser?.close(); await server?.close();
  for (const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child => new Promise(resolve => { if (child.exitCode !== null) resolve(); else { child.once('exit', resolve); setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000).unref(); } })));
  await writeFile(path.join(output, 'service.log'), logs.join(''));
}
