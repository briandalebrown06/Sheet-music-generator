/**
 * Sheet-music discovery and one-click downloads using real uploaded fixture inference.
 * Generated tones are explicitly labeled development input, never fake output.
 * Runs isolated API/worker/storage on 8001 and Vite on 5174; requires Chrome.
 */
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
const python = process.env.NOTATION_TEST_PYTHON || path.join(root, '.venv/bin/python');
const ffmpeg = process.env.FFMPEG_BINARY || path.join(root, '.runtime/bin/ffmpeg');
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-sheet-music-'));
const data = path.join(output, 'data');
await mkdir(output, { recursive: true });
const env = { ...process.env, DATA_DIR: data, AUDIO_TEST_DATA_DIR: data, FFMPEG_BINARY: ffmpeg, BASIC_PITCH_RUNTIME: 'onnx' };
const appUrl = 'http://127.0.0.1:5174', apiUrl = 'http://127.0.0.1:8001';
const children = [], logs = [], pageErrors = [];
let browser, server, assertions = 0;
const optionalIconErrors=[];
function captureConsole(message) {
  if(message.type()!=='error') return;
  const value={message:message.text(),url:message.location().url};
  // Chrome asks for an implicit favicon even though the application declares none.
  // Keep it in the report, while application/network errors still fail the suite.
  if(value.message.startsWith('Failed to load resource:')&&new URL(value.url||appUrl).pathname==='/favicon.ico') optionalIconErrors.push(value);
  else pageErrors.push(value);
}
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
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
async function getJson(url) { const response = await fetch(url); if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`); return response.json(); }
const getProject = id => getJson(`${apiUrl}/api/projects/${id}`);
const getScore = id => getJson(`${apiUrl}/api/projects/${id}/score`);
async function putJson(url, body, method = 'PUT') { return fetch(url, { method, headers: {'Content-Type':'application/json'}, body:JSON.stringify(body) }); }
function audioFixture() {
  const rate = 22050, duration = 6, count = duration * rate;
  const references = [60,62,64,65,67,64,62,60].map((pitch,i)=>({pitch,start:0.25+i*0.5,end:0.65+i*0.5}));
  const bytes = Buffer.alloc(44 + count * 2);
  bytes.write('RIFF',0); bytes.writeUInt32LE(bytes.length-8,4); bytes.write('WAVEfmt ',8);
  bytes.writeUInt32LE(16,16); bytes.writeUInt16LE(1,20); bytes.writeUInt16LE(1,22);
  bytes.writeUInt32LE(rate,24); bytes.writeUInt32LE(rate*2,28); bytes.writeUInt16LE(2,32); bytes.writeUInt16LE(16,34);
  bytes.write('data',36); bytes.writeUInt32LE(count*2,40);
  for(let i=0;i<count;i++) {
    const t=i/rate; let sample=0;
    for(const n of references) {
      if(t<n.start||t>n.end) continue;
      const local=t-n.start, envelope=Math.max(0,Math.min(1,local/.02,(n.end-t)/.04));
      const hz=440*Math.pow(2,(n.pitch-69)/12);
      sample+=envelope*[1,2,3].reduce((sum,h)=>sum+Math.sin(2*Math.PI*hz*h*local)/h,0)*7000;
    }
    bytes.writeInt16LE(Math.max(-32767,Math.min(32767,Math.round(sample))),44+i*2);
  }
  return {bytes,references};
}
async function action(page, button, suffix, method='POST') {
  const response = page.waitForResponse(r=>r.url().endsWith(suffix)&&r.request().method()===method);
  await page.getByRole('button',{name:button,exact:true}).click();
  const result=await response;
  equal(result.status(),200,await result.text());
  return result.json();
}
async function rendered(page) {
  await until(async()=>await page.getByTestId('score-renderer').locator('svg').count()>0,'OSMD SVG rendering');
  check(await page.getByTestId('score-renderer').locator('svg path').count()>0,'score contains actual engraved glyph paths');
}
async function runPython(code,args=[]) {
  return new Promise((resolve,reject)=>{
    const child=spawn(python,['-c',code,...args],{cwd:root,env,stdio:['ignore','pipe','pipe']});
    let out='',err=''; child.stdout.on('data',x=>out+=x);child.stderr.on('data',x=>err+=x);
    child.once('exit',exit=>exit===0?resolve(out):reject(new Error(`Validation Python ${exit}: ${err}`)));
  });
}

const primaryNames = ['Download PDF', 'Download MIDI', 'Download MusicXML (editable sheet music)'];
async function visibleActions(page, label) {
  const sheet = page.getByRole('region', { name: 'Your sheet music', exact: true });
  await sheet.waitFor();
  check(await sheet.getByRole('heading', { name: 'Your sheet music', exact: true }).isVisible(), `${label}: obvious sheet-music heading`);
  for (const name of ['View sheet music', ...primaryNames]) {
    const button = sheet.getByRole('button', { name, exact: true });
    check(await button.isVisible(), `${label}: ${name} visible without menus`);
    const box = await button.boundingBox();
    check(box && box.y >= 0 && box.y + box.height <= page.viewportSize().height, `${label}: ${name} is in the opening viewport`);
    check(box.height >= 44 && box.width >= 44, `${label}: ${name} has a touch-sized target`);
    check(await button.evaluate(el => !el.closest('details')), `${label}: ${name} is outside settings menus`);
  }
  check(await sheet.getByText('Printable sheet music', { exact: true }).isVisible(), `${label}: PDF purpose is clear`);
  check(await sheet.getByText('Editable sheet music', { exact: true }).isVisible(), `${label}: MusicXML purpose is clear`);
  return sheet;
}

async function download(page, button, file) {
  const pending = page.waitForEvent('download');
  await button.click();
  const result = await pending;
  equal(await result.failure(), null, `browser download ${file} succeeds`);
  await result.saveAs(path.join(output, file));
  return result.suggestedFilename();
}

try {
  const fixture = audioFixture(), file = path.join(output, 'DEVELOPMENT-sheet-music-fixture.wav');
  await writeFile(file, fixture.bytes);
  start(['-m', 'uvicorn', 'scripts.audio_test_server:app', '--host', '127.0.0.1', '--port', '8001', '--no-access-log']);
  await until(async () => (await fetch(`${apiUrl}/api/health`)).ok, 'test API startup');
  start(['-m', 'backend.app.worker']);
  await until(async () => (await getJson(`${apiUrl}/api/health`)).worker.online, 'worker heartbeat');
  server = await createServer({ configFile: path.join(root, 'frontend/vite.config.ts'), root: path.join(root, 'frontend'), server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy: { '/api': { target: apiUrl, changeOrigin: false } } } });
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name', { exact: true }).fill('DEVELOPMENT Sheet Music');
  await page.getByLabel('Clip start (seconds)', { exact: true }).fill('0.1');
  await page.getByLabel('Clip end (seconds)', { exact: true }).fill('5.1');
  const createdResponse = page.waitForResponse(r => r.url().endsWith('/api/projects') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create transcription', exact: true }).click();
  const response = await createdResponse;
  equal(response.status(), 201, 'real upload accepted');
  const project = await response.json(), base = `${apiUrl}/api/projects/${project.id}`;
  const completed = await until(async () => {
    const current = await getProject(project.id);
    return ['completed', 'failed', 'canceled'].includes(current.status) ? current : false;
  }, 'real Basic Pitch completion', 180000);
  equal(completed.status, 'completed', JSON.stringify(completed.jobs));
  check(completed.notes.length > 2, 'real inference produced notes');
  // Do not reload: this checks the queued -> completed transition itself.
  let sheet = await visibleActions(page, 'Just completed desktop');
  await rendered(page);
  check(await sheet.getByTestId('score-renderer').locator('svg').count() > 0, 'inline score is inside the primary results section');
  await sheet.getByRole('button', { name: 'View sheet music', exact: true }).click();
  check(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')?.toLowerCase().includes('sheet music')), 'View sheet music moves keyboard focus to inline score');
  const fullScreen = sheet.getByRole('button', { name: 'Full-screen score', exact: true });
  await fullScreen.click();
  const dialog = page.getByRole('dialog');
  await dialog.waitFor();
  await until(async () => await dialog.locator('svg path').count() > 0, 'full-screen actual score engraving');
  check(await dialog.getByRole('button', { name: 'Close full-screen', exact: true }).isVisible(), 'full-screen score has an obvious close control');
  await page.keyboard.press('Escape');
  await until(async () => !await dialog.isVisible(), 'Escape closes full-screen score');
  check(await fullScreen.evaluate(el => el === document.activeElement), 'full-screen close restores trigger focus');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(output, 'sheet-music-desktop.png'), fullPage: true });

  // Delay the genuine PDF response so the generation status is observable.
  let finishPdf;
  const pdfGate = new Promise(resolve => { finishPdf = resolve; });
  await page.route('**/score/exports/pdf?*', async route => {
    await pdfGate;
    await route.continue();
  });
  const pdfPending = download(page, sheet.getByRole('button', { name: 'Download PDF', exact: true }), 'initial-score.pdf');
  await until(async () => /generat|prepar/i.test(await sheet.innerText()), 'visible PDF generation status');
  check(await sheet.getByRole('button', { name: /PDF/ }).first().isDisabled(), 'pending PDF prevents duplicate generation clicks');
  finishPdf();
  const initialPdfName = await pdfPending;
  await page.unroute('**/score/exports/pdf?*');
  check(/DEVELOPMENT.*Sheet.*Music.*score-r\d+\.pdf$/i.test(initialPdfName), 'PDF download has the song title and revision');
  check(await sheet.getByRole('link', { name: /Download PDF again/i }).isVisible(), 'PDF retains a visible direct-download link');

  // Deliberate transport failure, clearly test-only. The next click uses the real export.
  await page.route('**/score/exports/midi?*', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Development test: MIDI exporter temporarily unavailable.' }) }), { times: 1 });
  await sheet.getByRole('button', { name: 'Download MIDI', exact: true }).click();
  await sheet.getByText('Development test: MIDI exporter temporarily unavailable.', { exact: true }).waitFor();
  check(await sheet.getByRole('button', { name: /Retry.*MIDI/i }).isVisible(), 'failed MIDI retains a visible retry action beside its explanation');
  await download(page, sheet.getByRole('button', { name: /Retry.*MIDI/i }), 'initial-score.mid');
  check(await sheet.getByRole('link', { name: /Download MIDI again/i }).isVisible(), 'successful retry retains a direct download link');

  const firstId = completed.notes[0].id, editedPitch = completed.notes[0].pitch + 1;
  await page.getByLabel('Note 1 MIDI pitch', { exact: true }).fill(String(editedPitch));
  await page.getByText('Musical settings and piano staves', { exact: true }).click();
  await page.getByLabel('Transposition (semitones)', { exact: true }).fill('2');
  await page.getByLabel('Key signature', { exact: true }).selectOption('2');
  for (const name of primaryNames) check(await sheet.getByRole('button', { name, exact: true }).isDisabled(), `${name} cannot silently download a prior saved revision while edits are unsaved`);
  check(/save/i.test(await sheet.innerText()), 'unsaved changes have visible save guidance beside exports');
  const saveResponse = page.waitForResponse(r => r.url().endsWith('/review') && r.request().method() === 'PUT');
  await page.getByRole('button', { name: 'Save review', exact: true }).first().click();
  const savedResponse = await saveResponse;
  equal(savedResponse.status(), 200, await savedResponse.text());
  const saved = await savedResponse.json(), revision = saved.score_state.revision;
  equal(saved.score_state.score.settings.transpose_semitones, 2, 'transposition belongs to the saved canonical score');
  await until(async () => await sheet.getByRole('button', { name: 'Download MIDI', exact: true }).isEnabled(), 'current revision downloads ready');
  equal(await sheet.getByRole('link', { name: /Download (PDF|MIDI) again/i }).count(), 0, 'saved edits invalidate previous ready links');
  const filenames = {};
  for (const [format, name, extension] of [['pdf', 'Download PDF', 'pdf'], ['midi', 'Download MIDI', 'mid'], ['musicxml', 'Download MusicXML (editable sheet music)', 'musicxml']]) {
    filenames[format] = await download(page, sheet.getByRole('button', { name, exact: true }), `edited-score.${extension}`);
    check(filenames[format].includes('D-major-B-minor'), `${format} filename identifies saved written key`);
    check(filenames[format].endsWith(`score-r${revision}.${extension}`), `${format} filename identifies current saved revision`);
  }
  const canonical = await getScore(project.id);
  await writeFile(path.join(output, 'edited-score.json'), JSON.stringify(canonical.score));
  const parsed = JSON.parse(await runPython(`import json,sys
from fractions import Fraction
from music21 import converter
import pretty_midi
from pypdf import PdfReader
s=converter.parse(sys.argv[1]);m=pretty_midi.PrettyMIDI(sys.argv[2]);c=json.load(open(sys.argv[3]));source=sys.argv[4];pitch=int(sys.argv[5]);p=PdfReader(sys.argv[6])
f=lambda r:Fraction(r['numerator'],r['denominator'])
events=[(bar,e) for part in c['parts'] for staff in part['staves'] for bar in staff['measures'] for voice in bar['voices'] for e in voice['events'] if source in e['source_note_ids']]
start=min(float(f(bar['start'])+f(e['offset'])) for bar,e in events)*60/c['tempo']['bpm'];end=max(float(f(bar['start'])+f(e['offset'])+f(e['duration'])) for bar,e in events)*60/c['tempo']['bpm']
print(json.dumps({'xml_transposed_pitch':pitch in [p.midi for n in s.recurse().notes for p in n.pitches],'midi_transposed_pitch_and_timing':any(n.pitch==pitch and abs(n.start-start)<.002 and abs(n.end-end)<.002 for i in m.instruments for n in i.notes),'pdf_pages':len(p.pages),'pdf_page_sizes':[[float(x.mediabox.width),float(x.mediabox.height)] for x in p.pages]}))`, [path.join(output, 'edited-score.musicxml'), path.join(output, 'edited-score.mid'), path.join(output, 'edited-score.json'), firstId, String(editedPitch + 2), path.join(output, 'edited-score.pdf')]));
  check(parsed.xml_transposed_pitch, 'downloaded MusicXML reparses with corrected and transposed pitch');
  check(parsed.midi_transposed_pitch_and_timing, 'main MIDI download contains saved correction, transposition and canonical timing');
  check(parsed.pdf_pages > 0 && parsed.pdf_page_sizes.every(([w, h]) => w > 500 && h > 700), 'PDF is a real paginated score');
  check(await page.getByRole('link', { name: /Raw.performance MIDI/i }).isVisible(), 'raw-performance MIDI remains separately labeled');

  await page.reload({ waitUntil: 'networkidle' });
  sheet = await visibleActions(page, 'Reopened desktop');
  await rendered(page);
  equal(await page.getByLabel('Note 1 MIDI pitch', { exact: true }).inputValue(), String(editedPitch), 'reopened project retains correction');
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, acceptDownloads: true });
  const mobilePage = await mobile.newPage();
  mobilePage.on('pageerror', error => pageErrors.push(error.message));
  await mobilePage.goto(`${appUrl}/#/projects/${project.id}`, { waitUntil: 'networkidle' });
  const mobileSheet = await visibleActions(mobilePage, 'Reopened mobile');
  await rendered(mobilePage);
  check(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile results do not overflow horizontally');
  const mobileName = await download(mobilePage, mobileSheet.getByRole('button', { name: 'Download MIDI', exact: true }), 'mobile-score.mid');
  equal(mobileName, filenames.midi, 'mobile one-click download uses the same saved score revision');
  await mobilePage.screenshot({ path: path.join(output, 'sheet-music-mobile.png'), fullPage: true });

  // Initial load failures must remain recoverable from the primary section,
  // without suggesting that the user's saved score does not exist.
  for (const [label, pathname] of [['score settings', `/api/projects/${project.id}/score`], ['saved MusicXML', `/api/projects/${project.id}/score/exports/musicxml`]]) {
    const match = url => url.pathname === pathname;
    const reason = `Development test: ${label} temporarily unavailable.`;
    await page.route(match, route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: reason }) }));
    await page.reload({ waitUntil: 'networkidle' });
    const recovery = page.getByRole('region', { name: 'Your sheet music', exact: true });
    await recovery.getByText(reason, { exact: true }).waitFor();
    check(await recovery.getByRole('button', { name: 'Retry loading sheet music', exact: true }).isVisible(), `${label} failure has a primary-section retry`);
    equal(await recovery.getByRole('button', { name: 'Create sheet music', exact: true }).count(), 0, `${label} failure does not misrepresent an existing score as absent`);
    for (const name of primaryNames) check(await recovery.getByRole('button', { name, exact: true }).isDisabled(), `${label} failure explains disabled ${name}`);
    await page.unroute(match);
    await recovery.getByRole('button', { name: 'Retry loading sheet music', exact: true }).click();
    await rendered(page);
    await until(async () => await recovery.getByRole('button', { name: 'Download MIDI', exact: true }).isEnabled(), `${label} retry restores downloads`);
    equal(await recovery.getByRole('button', { name: 'Retry loading sheet music', exact: true }).count(), 0, `${label} recovery clears retry state`);
  }

  const unavailablePdf = 'Development test: PDF engraving is unavailable in this environment.';
  await page.route('**/api/health', async route => {
    const response = await route.fetch(), health = await response.json();
    health.capabilities.pdf = false;
    health.pdf_export = { available: false, detail: unavailablePdf };
    await route.fulfill({ response, json: health });
  });
  await page.reload({ waitUntil: 'networkidle' });
  sheet = page.getByRole('region', { name: 'Your sheet music', exact: true });
  await rendered(page);
  await until(async () => await sheet.getByRole('button', { name: 'Download PDF', exact: true }).isDisabled(), 'unavailable PDF is disabled');
  check((await sheet.locator('#export-pdf-help').innerText()).includes(unavailablePdf), 'PDF unavailability is explained directly beside its button');
  check(await sheet.getByRole('button', { name: 'Download MIDI', exact: true }).isEnabled(), 'PDF unavailability does not block available MIDI');
  check(await sheet.getByRole('button', { name: 'Download MusicXML (editable sheet music)', exact: true }).isEnabled(), 'PDF unavailability does not block available MusicXML');
  await page.unroute('**/api/health');
  await page.reload({ waitUntil: 'networkidle' });
  await rendered(page);
  sheet = page.getByRole('region', { name: 'Your sheet music', exact: true });
  await download(page, sheet.getByRole('button', { name: 'Download MIDI', exact: true }), 'before-other-tab.mid');

  // Simulate another tab saving a score through the real API. A ready link may
  // not bypass revision validation by reusing its already downloaded blob.
  const beforeExternalSave = await getScore(project.id);
  const externallySaved = await putJson(`${base}/score`, { notes_revision: beforeExternalSave.source_notes_revision, revision: beforeExternalSave.revision, settings: beforeExternalSave.score.settings });
  equal(externallySaved.status, 200, 'another tab can save a new canonical revision');
  const newer = await externallySaved.json();
  equal(newer.revision, beforeExternalSave.revision + 1, 'external save advances score revision');
  const unwantedDownloads = [];
  const captureDownload = result => unwantedDownloads.push(result.suggestedFilename());
  page.on('download', captureDownload);
  const revalidation = page.waitForResponse(response => new URL(response.url()).pathname === `/api/projects/${project.id}/score/exports/midi`);
  await sheet.getByRole('link', { name: 'Download MIDI again', exact: true }).click();
  const rejected = await revalidation;
  equal(rejected.status(), 409, 'ready link rechecks the actual saved revision');
  await sheet.getByText('The saved score changed. Reload before downloading this revision.', { exact: true }).waitFor();
  check(await sheet.getByRole('button', { name: 'Retry MIDI download', exact: true }).isVisible(), 'stale ready link surfaces a recoverable error beside MIDI');
  await page.waitForTimeout(150);
  equal(unwantedDownloads, [], 'stale ready link does not download old bytes');
  page.off('download', captureDownload);

  await page.reload({ waitUntil: 'networkidle' });
  await rendered(page);
  const reopenedManual = await getScore(project.id);
  equal(reopenedManual.revision, newer.revision, 'reopening an existing score does not create another revision');
  equal(reopenedManual.score.settings, beforeExternalSave.score.settings, 'reopening preserves existing manual musical settings');
  equal(await page.getByLabel('Transposition (semitones)', { exact: true }).inputValue(), '2', 'reopened transposition remains manually selected');
  equal(await page.getByLabel('Key signature', { exact: true }).inputValue(), '2', 'reopened key signature remains manually selected');

  // Development-only migration fixture: remove only this isolated project's
  // canonical score row, preserving the actual model output and user edits.
  // This reproduces completed projects created before notation was installed.
  const legacyBefore = await getProject(project.id);
  equal(await runPython(`import os,sqlite3,sys
from pathlib import Path
directory=Path(os.environ['AUDIO_TEST_DATA_DIR']).resolve()
assert directory.name=='data' and directory.parent.name.startswith('scorecraft-sheet-music-')
with sqlite3.connect(directory/'projects.sqlite3') as connection:
 cursor=connection.execute('DELETE FROM score_records WHERE project_id=?',(sys.argv[1],))
 print(cursor.rowcount)`, [project.id]), '1\n', 'legacy fixture removes only its saved score row');
  equal((await getScore(project.id)).score, null, 'legacy fixture has real completed notes but no canonical score');
  const preparations = [];
  const capturePreparation = request => {
    if (new URL(request.url()).pathname === `/api/projects/${project.id}/score` && request.method() === 'PUT') preparations.push(request.postDataJSON());
  };
  page.on('request', capturePreparation);
  await page.reload({ waitUntil: 'networkidle' });
  await rendered(page);
  sheet = page.getByRole('region', { name: 'Your sheet music', exact: true });
  for (const name of primaryNames) {
    await until(async () => await sheet.getByRole('button', { name, exact: true }).isEnabled(), `legacy project automatically enables ${name}`);
    check(await sheet.getByRole('button', { name, exact: true }).isEnabled(), `legacy project ${name} ready without manual score setup`);
  }
  page.off('request', capturePreparation);
  check(preparations.length >= 1, 'legacy reopening actually saves its initial canonical score');
  const legacyAfter = await getProject(project.id), legacyScore = await getScore(project.id);
  equal(legacyAfter.notes_revision, legacyBefore.notes_revision, 'automatic initial score preserves performance-note revision');
  equal(legacyAfter.notes, legacyBefore.notes, 'automatic initial score preserves every detected/corrected note value');
  equal(legacyScore.source_notes_revision, legacyBefore.notes_revision, 'initial score identifies existing saved performance revision');
  equal(legacyScore.revision, 1, 'initial canonical score is saved as revision one');
  check(legacyScore.score !== null && !legacyScore.stale, 'legacy project now has current canonical notation');
  const legacyFilename = await download(page, sheet.getByRole('button', { name: 'Download MIDI', exact: true }), 'legacy-prepared-score.mid');
  check(legacyFilename.endsWith('score-r1.mid'), 'legacy initial score downloads without opening settings');
  equal((await readFile(path.join(output, 'legacy-prepared-score.mid'))).subarray(0, 4).toString(), 'MThd', 'legacy download is an actual MIDI file');
  equal(pageErrors, [], 'no unhandled browser errors');
  const report = { passed: true, assertions, output, projectId: project.id, detectedNotes: completed.notes.length, revision, filenames, parsed, checks: ['real uploaded audio and inference', 'immediate completed/reopened primary actions without menus', 'desktop and mobile touch-sized actions', 'inline score and keyboard full-screen dismissal/focus', 'real one-click PDF generation and download', 'visible retry after injected export failure', 'persistent ready download links', 'unsaved edits block old exports', 'saved corrections/transposition in XML and MIDI', 'real PDF page objects', 'raw MIDI separate', 'initial score and XML failures recover in primary section', 'PDF unavailability explained beside button', 'ready links revalidate external saves and reject stale bytes', 'existing manual settings survive reopening', 'legacy completed project automatically receives real notation without changing notes'], limitations: ['Generated audio only; no accuracy improvement claim', 'Chrome desktop and emulated mobile viewport; no physical mobile device', 'Transport failures and unavailable-PDF health response are deliberately injected; successful retries and cross-tab revision rejection use the actual backend', 'Legacy fixture deletes only its score row in isolated test storage; normal project data is not touched'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  const failedPage = browser?.contexts()[0]?.pages()[0];
  if (failedPage) {
    await failedPage.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    await writeFile(path.join(output, 'failure.txt'), await failedPage.locator('body').innerText().catch(() => String(error)));
  }
  console.error('Failure artifacts:', output);
  throw error;
} finally {
  await browser?.close();
  await server?.close();
  for (const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child => new Promise(resolve => {
    if (child.exitCode !== null) resolve();
    else { child.once('exit', resolve); setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000).unref(); }
  })));
  await writeFile(path.join(output, 'service.log'), logs.join(''));
}
