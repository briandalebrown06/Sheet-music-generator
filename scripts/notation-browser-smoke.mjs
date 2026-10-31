/**
 * Real uploaded audio -> Basic Pitch -> canonical score -> OSMD integration.
 * Generated tones are explicitly labeled development input, never fake output.
 * Runs isolated API/worker/storage on 8001 and Vite on 5174; requires Chrome.
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
const python = process.env.NOTATION_TEST_PYTHON || path.join(root, '.venv/bin/python');
const ffmpeg = process.env.FFMPEG_BINARY || path.join(root, '.runtime/bin/ffmpeg');
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-notation-'));
const data = path.join(output, 'data');
await mkdir(output, { recursive: true });
const env = { ...process.env, DATA_DIR: data, AUDIO_TEST_DATA_DIR: data, FFMPEG_BINARY: ffmpeg, BASIC_PITCH_RUNTIME: 'onnx' };
const appUrl = 'http://127.0.0.1:5174', apiUrl = 'http://127.0.0.1:8001';
const children = [], logs = [], pageErrors = [];
let browser, server, assertions = 0;
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
const performanceValues = notes => notes.map(note => ({...note, voice_id: note.voice_id ?? null})).sort((a,b)=>a.id.localeCompare(b.id));
const reviewBody = (project, score, settings) => ({
  notes_revision: project.notes_revision,
  score_revision: score.revision,
  notes: project.notes.map(({id,pitch,start,end,velocity,part_id,voice_id}) => ({id,pitch,start,end,velocity,part_id,voice_id:voice_id ?? null})),
  settings,
});
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
  await page.getByRole('button',{name:button,exact:true}).first().click();
  const result=await response;
  equal(result.status(),200,await result.text());
  return result.json();
}
async function openSettings(page) {
  if (!await page.getByLabel('Tempo mode',{exact:true}).isVisible()) {
    await page.getByText('Musical settings and piano staves',{exact:true}).click();
  }
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
try {
  const fixture=audioFixture(), file=path.join(output,'development-fixture-notation-melody.wav');
  await writeFile(file,fixture.bytes);
  await writeFile(path.join(output,'fixture-reference.json'),JSON.stringify({purpose:'Generated development audio, not expected model output',notes:fixture.references},null,2));
  start(['-m','uvicorn','scripts.audio_test_server:app','--host','127.0.0.1','--port','8001']);
  await until(async()=>(await fetch(`${apiUrl}/api/health`)).ok,'test API startup');
  start(['-m','backend.app.worker']);
  await until(async()=>(await getJson(`${apiUrl}/api/health`)).worker.online,'worker heartbeat');
  server=await createServer({configFile:path.join(root,'frontend/vite.config.ts'),root:path.join(root,'frontend'),server:{host:'127.0.0.1',port:5174,strictPort:true,proxy:{'/api':{target:apiUrl,changeOrigin:false}}}});
  await server.listen();
  browser=await chromium.launch({headless:true,channel:process.env.CHROME_CHANNEL||'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1100}}), page=await context.newPage();
  page.on('pageerror',error=>pageErrors.push(error.message));
  console.log('Uploading generated audio and running actual Basic Pitch…');
  await page.goto(appUrl,{waitUntil:'networkidle'});
  await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name',{exact:true}).fill('[Development fixture] melody to readable score');
  const createdResponse=page.waitForResponse(r=>r.url().endsWith('/api/projects')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Create transcription',exact:true}).click();
  const created=await createdResponse;
  equal(created.status(),201,await created.text());
  const project=await created.json();
  const completed=await until(async()=>{
    const p=await getProject(project.id);return ['completed','failed','canceled'].includes(p.status)?p:false;
  },'real transcription completion',180000);
  equal(completed.status,'completed',JSON.stringify(completed.jobs));
  check(completed.notes.length>0,'real model produced source notes');
  const notesBefore=performanceValues(completed.notes);
  const rawMidi=Buffer.from(await(await fetch(`${apiUrl}/api/projects/${project.id}/exports/raw-midi`)).arrayBuffer());
  const rawEvents=Buffer.from(await(await fetch(`${apiUrl}/api/projects/${project.id}/artifacts/raw-note-events`)).arrayBuffer());
  const initial=await getScore(project.id);
  await page.reload({waitUntil:'networkidle'});
  await page.getByRole('region',{name:'Musical score',exact:true}).waitFor();
  console.log('Previewing controls without modifying the performance or saved score…');
  await openSettings(page);
  await page.getByLabel('Tempo mode',{exact:true}).selectOption('manual');
  await page.getByLabel('Tempo (BPM)',{exact:true}).fill('120');
  await page.getByLabel('Time signature',{exact:true}).selectOption('3/4');
  await page.getByLabel('Key signature',{exact:true}).selectOption('1');
  await page.getByLabel('First downbeat (seconds)',{exact:true}).fill('0.5');
  await page.getByLabel('Pickup length (beats)',{exact:true}).fill('1');
  await page.getByLabel('Quantization',{exact:true}).selectOption('eighth_triplet');
  await page.getByLabel('Clef',{exact:true}).selectOption('bass');
  await page.getByLabel('Transposition (semitones)',{exact:true}).fill('2');
  const preview=await action(page,'Preview score','/review/preview');
  check(preview.musicxml.includes('<score-partwise'),'preview is real MusicXML');
  equal(preview.score.settings.tempo_bpm,120,'manual tempo applied');
  equal(preview.score.settings.quantization,'eighth_triplet','triplet quantization applied');
  equal(preview.score.settings.first_downbeat_seconds,0.5,'manual downbeat applied');
  equal(preview.score.settings.pickup_beats,'1','pickup setting applied');
  await rendered(page);
  const afterPreview=await getScore(project.id);
  equal(afterPreview.revision,initial.revision,'preview does not increment saved revision');
  equal(afterPreview.score,initial.score,'preview does not overwrite saved canonical score');
  equal(performanceValues((await getProject(project.id)).notes),notesBefore,'preview leaves raw performance-note timing unchanged');
  const saved=await action(page,'Save review','/review','PUT');
  equal(saved.score_state.revision,initial.revision+1,'save increments score revision');
  equal(saved.score_state.source_notes_revision,saved.project.notes_revision,'atomic save binds notation to saved performance revision');
  await page.reload({waitUntil:'networkidle'}); await rendered(page);
  await openSettings(page);
  equal(await page.getByLabel('Tempo (BPM)',{exact:true}).inputValue(),'120','tempo persists after refresh');
  equal(await page.getByLabel('Quantization',{exact:true}).inputValue(),'eighth_triplet','quantization persists after refresh');
  equal(await page.getByLabel('First downbeat (seconds)',{exact:true}).inputValue(),'0.5','alignment persists after refresh');
  const zoom=page.getByLabel('Score zoom',{exact:true});
  const previousZoom=await zoom.inputValue();
  await zoom.evaluate(el => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, '1.2'); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); });
  await rendered(page);
  check(await zoom.inputValue()!==previousZoom,'zoom control changes display scale');
  console.log('Validating exports from the canonical model and piano correction…');
  for(const format of ['musicxml','midi','json']) {
    const response=await fetch(`${apiUrl}/api/projects/${project.id}/score/exports/${format}?revision=${saved.score_state.revision}`);
    equal(response.status,200,`${format} notation export available`);
    await writeFile(path.join(output,`score.${format==='midi'?'mid':format}`),Buffer.from(await response.arrayBuffer()));
  }
  const parsed=JSON.parse(await runPython(`import json,sys
from fractions import Fraction
from music21 import converter
import pretty_midi
s=converter.parse(sys.argv[1])
m=pretty_midi.PrettyMIDI(sys.argv[2])
c=json.load(open(sys.argv[3]))
f=lambda r:Fraction(r['numerator'],r['denominator'])
valid=all(sum((f(e['duration']) for e in v['events']),Fraction(0))==f(bar['duration']) for part in c['parts'] for staff in part['staves'] for bar in staff['measures'] for v in bar['voices'])
first=s.parts[0].measure(0)
print(json.dumps({'parts':len(s.parts),'notes':len(s.recurse().notes),'midi_notes':sum(len(i.notes) for i in m.instruments),'tuplets':sum(bool(n.duration.tuplets) for n in s.recurse().notes),'pickup':first is not None,'time_signature':first.timeSignature.ratioString,'key_signature':first.keySignature.sharps,'clef':first.clef.__class__.__name__,'first_pitch':s.recurse().notes[0].pitches[0].midi,'measure_durations_valid':valid}))`,[path.join(output,'score.musicxml'),path.join(output,'score.mid'),path.join(output,'score.json')]));
  check(parsed.notes>0&&parsed.midi_notes>0,'MusicXML round-trip and notation MIDI contain notes');
  check(parsed.tuplets>0,'triplet MusicXML round-trip retains tuplets');
  check(parsed.pickup,'MusicXML round-trip retains pickup measure zero');
  equal(parsed.time_signature,'3/4','MusicXML preserves manually selected meter');
  equal(parsed.key_signature,1,'MusicXML preserves written key signature');
  equal(parsed.clef,'BassClef','MusicXML preserves manual clef');
  equal(parsed.first_pitch,completed.notes[0].pitch+2,'MusicXML uses canonical transposition');
  equal(parsed.measure_durations_valid,true,'rational durations fill every canonical measure voice exactly');
  await page.getByText('Piano staves and note placement',{exact:true}).click();
  await page.getByLabel('Piano staff split',{exact:true}).check();
  await page.getByLabel('Treble starts at MIDI pitch',{exact:true}).fill('64');
  const override=page.getByLabel(/^Staff for /).first();
  await override.selectOption('bass');
  const piano=await action(page,'Preview score','/review/preview');
  check(piano.musicxml.includes('<staves>2</staves>'),'heuristic piano split exports two staves in one part');
  equal(piano.score.settings.piano_split,true,'piano heuristic enabled');
  check(Object.values(piano.score.settings.staff_overrides).includes('bass'),'manual staff correction passed to canonical model');
  check((await page.getByRole('region',{name:'Musical score'}).innerText()).includes('hands'),'piano caveat visible');
  await rendered(page);
  const pianoSaved=await action(page,'Save review','/review','PUT');
  equal(pianoSaved.score_state.score.settings.staff_overrides,piano.score.settings.staff_overrides,'staff override persists');
  const invalid=await putJson(`${apiUrl}/api/projects/${project.id}/review/preview`,reviewBody(pianoSaved.project,pianoSaved.score_state,{...piano.score.settings,tempo_bpm:-20}),'POST');
  equal(invalid.status,422,'invalid musical settings rejected');
  const conflict=await putJson(`${apiUrl}/api/projects/${project.id}/review`,{...reviewBody(pianoSaved.project,pianoSaved.score_state,piano.score.settings),score_revision:initial.revision});
  equal(conflict.status,409,'stale score revision rejected');
  equal(hash(Buffer.from(await(await fetch(`${apiUrl}/api/projects/${project.id}/exports/raw-midi`)).arrayBuffer())),hash(rawMidi),'notation changes never modify raw MIDI');
  equal(hash(Buffer.from(await(await fetch(`${apiUrl}/api/projects/${project.id}/artifacts/raw-note-events`)).arrayBuffer())),hash(rawEvents),'notation changes never modify raw events');
  equal(performanceValues((await getProject(project.id)).notes),notesBefore,'notation changes preserve performance notes and original times');
  await page.getByLabel('Score zoom',{exact:true}).evaluate(el => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, '1'); el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); });
  await rendered(page);
  await page.getByRole('region',{name:'Musical score'}).screenshot({path:path.join(output,'notation-score.png')});
  await page.screenshot({path:path.join(output,'notation-desktop.png'),fullPage:true});
  const mobile=await browser.newContext({viewport:{width:390,height:844}}), mobilePage=await mobile.newPage();
  mobilePage.on('pageerror',error=>pageErrors.push(error.message));
  await mobilePage.goto(`${appUrl}/#/projects/${project.id}`,{waitUntil:'networkidle'});await rendered(mobilePage);
  check(await mobilePage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile page has no horizontal overflow');
  await mobilePage.screenshot({path:path.join(output,'notation-mobile.png'),fullPage:true});
  console.log('Checking staleness after performance edits…');
  const current=await getProject(project.id);
  const edit=await putJson(`${apiUrl}/api/projects/${project.id}/notes`,{revision:current.notes_revision,notes:current.notes.map((n,i)=>({id:n.id,pitch:i===0?n.pitch+1:n.pitch,start:n.start,end:n.end,velocity:n.velocity,part_id:n.part_id}))});
  equal(edit.status,200,'source note edit saves');
  equal((await getScore(project.id)).stale,true,'saved notation becomes stale after performance edit');
  equal((await fetch(`${apiUrl}/api/projects/${project.id}/score/exports/musicxml`)).status,409,'stale notation export blocked');
  await page.reload({waitUntil:'networkidle'});
  check(/changed|stale|out of date/i.test(await page.getByRole('region',{name:'Musical score'}).innerText()),'stale notation clearly labeled');
  equal(pageErrors,[],'no unhandled browser errors');
  const report={passed:true,assertions,output,projectId:project.id,detectedNotes:completed.notes.length,roundTrip:parsed,checks:['uploaded generated audio through real Basic Pitch','canonical score SVG rendering','manual musical settings and triplets','preview without persistence','save/reload','MusicXML round-trip and score MIDI','heuristic piano split and manual staff correction','raw/performance preservation','invalid/conflicting settings','staleness','desktop/mobile'],limitations:['Generated fixture input only; no claim of transcription accuracy on physical instruments','Installed Chrome only; no other browser or physical mobile device tested']};
  await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
} finally {
  await browser?.close();await server?.close();
  for(const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child=>new Promise(resolve=>{if(child.exitCode!==null)resolve();else{child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},5000).unref();}})));
  await writeFile(path.join(output,'service.log'),logs.join(''));
}
