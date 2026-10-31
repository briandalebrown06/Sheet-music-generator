/**
 * Piano arrangement review using real inference followed by explicit adversarial note edits.
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
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-piano-review-'));
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
    const result = await Promise.resolve().then(fn).catch(() => false);
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

const scorePitches = score => score.parts.filter(part=>!part.is_cue).flatMap(part=>part.staves.flatMap(staff=>staff.measures.flatMap(measure=>measure.voices.flatMap(voice=>voice.events.filter(event=>event.kind!=='rest'&&!['continue','stop'].includes(event.tie)).flatMap(event=>event.pitches))))).sort((a,b)=>a-b);
async function exportSet(id, revision, prefix) {
  for(const format of ['musicxml','midi','pdf']) {
    const response=await fetch(`${apiUrl}/api/projects/${id}/score/exports/${format}?revision=${revision}`);
    equal(response.status,200,`${prefix} ${format} export`);
    equal(response.headers.get('x-score-revision'),String(revision),'export revision agrees');
    const bytes=Buffer.from(await response.arrayBuffer());
    check(bytes.length>100,`${format} is not empty`);
    if(format==='pdf') equal(bytes.subarray(0,5).toString(),'%PDF-','real PDF header');
    await writeFile(path.join(output,`${prefix}.${format==='midi'?'mid':format}`),bytes);
  }
}
async function saveReview(page,id) {
  const response=page.waitForResponse(r=>r.url().endsWith(`/projects/${id}/review`)&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Save review',exact:true}).first().click();
  const result=await response;equal(result.status(),200,await result.text());return result.json();
}
try {
  const fixture=audioFixture(),file=path.join(output,'DEVELOPMENT-piano-review.wav');
  await writeFile(file,fixture.bytes);
  start(['-m','uvicorn','scripts.audio_test_server:app','--host','127.0.0.1','--port','8001','--no-access-log']);
  await until(async()=>(await fetch(`${apiUrl}/api/health`)).ok,'test API startup');
  start(['-m','backend.app.worker']);
  await until(async()=>(await getJson(`${apiUrl}/api/health`)).worker.online,'worker heartbeat');
  server=await createServer({configFile:path.join(root,'frontend/vite.config.ts'),root:path.join(root,'frontend'),server:{host:'127.0.0.1',port:5174,strictPort:true,proxy:{'/api':{target:apiUrl,changeOrigin:false}}}});await server.listen();
  browser=await chromium.launch({headless:true,channel:process.env.CHROME_CHANNEL||'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  const page=await context.newPage();page.on('pageerror',error=>pageErrors.push(error.message));
  await page.goto(appUrl,{waitUntil:'networkidle'});
  await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name',{exact:true}).fill('DEVELOPMENT Piano Review');
  const upload=page.waitForResponse(r=>r.url().endsWith('/api/projects')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Create transcription',exact:true}).click();
  const response=await upload;equal(response.status(),201,'real source accepted');const created=await response.json();
  const original=await until(async()=>{const p=await getProject(created.id);return ['completed','failed'].includes(p.status)?p:false},'actual model inference',180000);
  equal(original.status,'completed',JSON.stringify(original.jobs));check(original.notes.length>=8,'real model supplied original note events');
  const originalScore=await getScore(original.id);
  // This is an explicit review edit fixture, not substituted model inference:
  // a pianist's oversized right-hand chord with a retained essential bass.
  const pitches=[48,60,64,67,72,76,79,84];
  const edited=original.notes.slice(0,8).map((n,i)=>({id:n.id,pitch:pitches[i],start:0,end:.5,velocity:80,part_id:'piano',voice_id:null,hand:'right',locked:false,role:i===0?'bass':'harmony',key_release_seconds:null,pedal_sustained:false}));
  let result=await putJson(`${apiUrl}/api/projects/${original.id}/review`,{notes_revision:original.notes_revision,score_revision:originalScore.revision,notes:edited,settings:{...originalScore.score.settings,tempo_bpm:120,first_downbeat_seconds:0,pickup_beats:'0',quantization:'eighth',piano_split:true,transpose_semitones:0,staff_overrides:{}}});
  equal(result.status,200,await result.text());const baseline=await getProject(original.id),before=await getScore(original.id);
  await exportSet(original.id,before.revision,'before');
  await page.goto(`${appUrl}/#/projects/${original.id}`,{waitUntil:'networkidle'});
  await page.getByRole('button',{name:'Create playable accompaniment',exact:true}).waitFor();
  const beforeChecks=await getJson(`${apiUrl}/api/projects/${original.id}/playability`);
  check(beforeChecks.report.summary.error_count>0,'oversized single-hand chord fails constraints');
  equal(await page.getByLabel('Left-hand reach',{exact:true}).inputValue(),'9','default left reach');
  equal(await page.getByLabel('Right-hand reach',{exact:true}).inputValue(),'9','default right reach');
  check(await page.getByText('This is an automated assessment, not pianist validation.',{exact:false}).isVisible(),'human-review distinction visible');
  const derivation=page.waitForResponse(r=>r.url().endsWith(`/projects/${original.id}/accompaniment`)&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Create playable accompaniment',exact:true}).click();
  const derivedResponse=await derivation;check(derivedResponse.ok(),await derivedResponse.text());const derived=await derivedResponse.json();
  check(derived.id!==original.id,'arrangement is separate persisted project');equal(derived.output_mode,'accompaniment','arrangement provenance explicit');
  equal(derived.arrangement_source_id,original.id,'source version linked');
  await until(()=>page.url().endsWith(derived.id),'navigation to derived version');
  await rendered(page);
  await until(async()=>await page.getByRole('button',{name:'Revoice for comfort',exact:true}).isEnabled(),'separate revoice action available');
  const afterChecks=await getJson(`${apiUrl}/api/projects/${derived.id}/playability`);
  check(afterChecks.report.summary.error_count<beforeChecks.report.summary.error_count,'actual repairs reduce constraint errors');
  equal((await getProject(original.id)).notes,baseline.notes,'original saved notes unchanged');
  const after=await getScore(derived.id);
  await exportSet(derived.id,after.revision,'after');
  check(scorePitches(after.score).length<=scorePitches(before.score).length,'redundant texture does not expand');
  for(const name of ['View sheet music','Download PDF','Download MIDI','Download MusicXML (editable sheet music)'])
    check(await page.getByRole('region',{name:'Your sheet music',exact:true}).getByRole('button',{name,exact:true}).isVisible(),`primary action ${name} remains visible`);
  await page.getByLabel('Select note 1',{exact:true}).check();
  await page.getByLabel('Selected note manual finger number',{exact:true}).selectOption('3');
  await page.getByLabel('Selected note sustained by pedal',{exact:true}).check();
  const firstNote=derived.notes[0];
  await page.getByLabel('Selected note physical key release seconds',{exact:true}).fill(String(firstNote.start+(firstNote.end-firstNote.start)/2));
  check(await page.getByLabel('Lock selected note for arrangement',{exact:true}).isChecked(),'manual changes lock selected note');
  const metadataSaved=await saveReview(page,derived.id);
  const savedNote=metadataSaved.project.notes.find(n=>n.id===firstNote.id);
  equal(savedNote.fingering,3,'manual finger persists');check(savedNote.pedal_sustained,'manual pedal persists');check(savedNote.locked,'lock persists');
  await page.reload({waitUntil:'networkidle'});await rendered(page);
  await page.getByLabel('Select note 1',{exact:true}).check();
  equal(await page.getByLabel('Selected note manual finger number',{exact:true}).inputValue(),'3','manual finger reopens');
  check(await page.getByLabel('Selected note sustained by pedal',{exact:true}).isChecked(),'pedal reopens');
  const preTranspose=await getScore(derived.id);
  await page.getByText('Musical settings and piano staves',{exact:true}).click();
  await page.getByLabel('Transposition (semitones)',{exact:true}).fill('2');
  check(await page.getByRole('button',{name:'Revoice for comfort',exact:true}).isDisabled(),'revoice waits for saved exact transposition');
  await saveReview(page,derived.id);
  const transposed=await getScore(derived.id);
  equal(scorePitches(transposed.score),scorePitches(preTranspose.score).map(p=>p+2),'exact transposition preserves every interval');
  await until(async()=>await page.getByRole('button',{name:'Revoice for comfort',exact:true}).isEnabled(),'checks refresh after transposition');
  const transposedChecks=await getJson(`${apiUrl}/api/projects/${derived.id}/playability`);
  equal(transposedChecks.score_revision,transposed.revision,'playability follows exact transposed saved revision');
  const parentSnapshot=await getProject(derived.id);
  const secondCreate=page.waitForResponse(r=>r.url().endsWith(`/projects/${derived.id}/accompaniment`)&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Revoice for comfort',exact:true}).click();
  const secondResponse=await secondCreate;check(secondResponse.ok(),await secondResponse.text());const second=await secondResponse.json();
  equal(second.arrangement_source_id,derived.id,'revoice creates separately reversible version');
  equal((await getProject(derived.id)).notes,parentSnapshot.notes,'revoice preserves saved source edits');
  await until(()=>page.url().endsWith(second.id),'second arrangement navigation');await rendered(page);
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>window.scrollTo(0,0));
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'mobile has no page-width overflow');
  for(const name of ['Download PDF','Download MIDI','Download MusicXML (editable sheet music)']) {
    const button=page.getByRole('region',{name:'Your sheet music',exact:true}).getByRole('button',{name,exact:true});
    const box=await button.boundingBox();check(box&&box.width>=44&&box.height>=44,`${name} touch target`);
  }
  await page.screenshot({path:path.join(output,'piano-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:path.join(output,'piano-desktop.png')});
  await page.getByRole('region',{name:'Piano accompaniment and playability',exact:true}).screenshot({path:path.join(output,'piano-controls.png')});
  await page.getByLabel('Left-hand reach',{exact:true}).focus();check(await page.getByLabel('Left-hand reach',{exact:true}).evaluate(el=>el===document.activeElement),'reach control keyboard focus');
  await page.keyboard.press('ArrowUp');await page.keyboard.press('Tab');
  await runPython("import sys,json;from pathlib import Path;from music21 import converter;import pretty_midi;from pypdf import PdfReader\np=Path(sys.argv[1]); expected=json.loads(sys.argv[2]);\nfor label in ('before','after'):\n xml=converter.parse(p/(label+'.musicxml')); midi=pretty_midi.PrettyMIDI(str(p/(label+'.mid'))); got=sorted(n.pitch for i in midi.instruments for n in i.notes); assert got==expected[label],(label,got,expected[label]); assert list(xml.recurse().notes); assert len(PdfReader(p/(label+'.pdf')).pages)>0\nprint('MusicXML reparsed, MIDI pitches match canonical saved score, real PDFs have pages')",[output,JSON.stringify({before:scorePitches(before.score),after:scorePitches(after.score)})]);
  equal(pageErrors,[],'no browser application errors');
  const report={assertions,passed:true,scope:'Real generated audio inference followed by labeled adversarial review-note fixture, separate arrangement/revoice, metadata persistence, exact transpose checks and saved revision exports. No pianist or physical-device validation.',before:beforeChecks.report,after:afterChecks.report,projects:{original:original.id,arrangement:derived.id,revoiced:second.id}};
  await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({assertions,passed:true,output},null,2));
} catch(error) {
  await writeFile(path.join(output,'failure.txt'),String(error.stack||error));console.error(error);console.error('Artifacts:',output);process.exitCode=1;
} finally {
  await browser?.close();await server?.close();for(const child of children.reverse())child.kill('SIGTERM');
  await writeFile(path.join(output,'services.log'),logs.join(''));
}
