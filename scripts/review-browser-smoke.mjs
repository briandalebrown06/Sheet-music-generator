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
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-review-'));
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
try {
  const fixture=audioFixture(), file=path.join(output,'development-fixture-review.wav');
  await writeFile(file,fixture.bytes);
  start(['-m','uvicorn','scripts.audio_test_server:app','--host','127.0.0.1','--port','8001']);
  await until(async()=>(await fetch(`${apiUrl}/api/health`)).ok,'test API startup');
  start(['-m','backend.app.worker']);
  await until(async()=>(await getJson(`${apiUrl}/api/health`)).worker.online,'worker heartbeat');
  server=await createServer({configFile:path.join(root,'frontend/vite.config.ts'),root:path.join(root,'frontend'),server:{host:'127.0.0.1',port:5174,strictPort:true,proxy:{'/api':{target:apiUrl,changeOrigin:false}}}});
  await server.listen();
  browser=await chromium.launch({headless:true,channel:process.env.CHROME_CHANNEL||'chrome'});
  const context=await browser.newContext({viewport:{width:1440,height:1100}});
  await context.addInitScript(()=>{
    window.__reviewTones=[];
    const create=AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator=function(...args){
      const oscillator=create.apply(this,args), start=oscillator.start.bind(oscillator), stop=oscillator.stop.bind(oscillator);
      oscillator.start=(time)=>{window.__reviewTones.push({pitchHz:oscillator.frequency.value,start:time,contextTime:this.currentTime});return start(time);};
      oscillator.stop=(time)=>{window.__reviewLastStop=time??'immediate';return stop(time);};
      return oscillator;
    };
  });
  const page=await context.newPage();
  page.on('pageerror',error=>pageErrors.push(error.message));
  page.on('console',captureConsole);
  console.log('Uploading generated review fixture through real model…');
  await page.goto(appUrl,{waitUntil:'networkidle'});
  await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name',{exact:true}).fill('[Development fixture] transcription review');
  await page.getByLabel('Clip start (seconds)',{exact:true}).fill('0.1');
  await page.getByLabel('Clip end (seconds)',{exact:true}).fill('5.1');
  const createdResponse=page.waitForResponse(r=>r.url().endsWith('/api/projects')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Create transcription',exact:true}).click();
  const created=await createdResponse;
  equal(created.status(),201,await created.text());
  const project=await created.json(), base=`${apiUrl}/api/projects/${project.id}`;
  const completed=await until(async()=>{const p=await getProject(project.id);return ['completed','failed','canceled'].includes(p.status)?p:false;},'real inference',180000);
  equal(completed.status,'completed',JSON.stringify(completed.jobs));
  check(completed.notes.length>2,'actual inference produced reviewable notes');
  const firstId=completed.notes[0].id, firstPitch=completed.notes[0].pitch, targetPitch=firstPitch+2;
  const rawMidi=Buffer.from(await(await fetch(base+'/exports/raw-midi')).arrayBuffer());
  const rawEvents=Buffer.from(await(await fetch(base+'/artifacts/raw-note-events')).arrayBuffer());
  await page.reload({waitUntil:'networkidle'}); await rendered(page);
  const editor=page.getByRole('region',{name:'Note event editor',exact:true});
  await editor.waitFor();
  const count=()=>editor.locator('tbody tr').count();
  const source=page.getByLabel('Playback source',{exact:true});
  const play=()=>page.getByRole('button',{name:'Play review playback',exact:true}).click();
  const pause=()=>page.getByRole('button',{name:'Pause review playback',exact:true}).click();
  const pos=async()=>Number(await page.getByTestId('playback-position').getAttribute('data-seconds'));
  const rewind=()=>page.getByRole('button',{name:'Rewind review playback',exact:true}).click();
  await until(async()=>await page.getByTestId('review-waveform').locator('line').count()>10,'real waveform peaks');
  const wave=await getJson(base+'/waveform');
  check(wave.peaks.some(x=>x>0.01),'waveform contains decoded non-silent samples');
  check(Math.abs(wave.duration-5)<0.05,'waveform duration matches audio fixture');
  await play(); await until(async()=>await pos()>0.15,'original audio clock advances'); await pause();
  const paused=await pos();
  const mediaTime=await page.locator('section[aria-label="Audio review playback"] audio').evaluate(a=>a.currentTime);
  check(Math.abs(mediaTime-paused-0.1)<0.04,'original playback adds selected clip offset');
  await page.waitForTimeout(120);
  check(Math.abs(await pos()-paused)<0.03,'original audio pause stops cursor');
  await source.selectOption('raw'); await until(async()=>await page.getByRole('button',{name:'Play review playback',exact:true}).isEnabled(),'raw events loaded');
  await rewind(); await play();await until(async()=>await page.evaluate(()=>window.__reviewTones.length)>0,'actual WebAudio raw synthesis');await pause();
  const rawApi=await getJson(base+'/playback/raw');
  check(rawApi.notes.length>0,'raw playback uses real preserved events');
  console.log('Selecting and editing with undo, redo, split, merge, and stable identity…');
  const scoreNote=page.locator(`[data-source-note-ids~="${firstId}"]`).first();
  await until(async()=>await scoreNote.count()>0,'score/source ID binding');
  await scoreNote.click();
  equal(await page.getByLabel('Select note 1',{exact:true}).isChecked(),true,'score note click selects matching event');
  await page.getByLabel('Note 1 MIDI pitch',{exact:true}).fill(String(targetPitch));
  await page.getByRole('button',{name:'Undo edit',exact:true}).click();
  equal(await page.getByLabel('Note 1 MIDI pitch',{exact:true}).inputValue(),String(firstPitch),'undo restores pitch');
  await page.getByRole('button',{name:'Redo edit',exact:true}).click();
  equal(await page.getByLabel('Note 1 MIDI pitch',{exact:true}).inputValue(),String(targetPitch),'redo restores edited pitch');
  const priorDuration=Number(await page.getByLabel('Note 1 duration seconds',{exact:true}).inputValue());
  await page.getByLabel('Note 1 start seconds',{exact:true}).fill('');
  await page.getByLabel('Note 1 start seconds',{exact:true}).fill('0.3');
  check(Math.abs(Number(await page.getByLabel('Note 1 duration seconds',{exact:true}).inputValue())-priorDuration)<1e-5,'clear/retype start preserves duration');
  await page.getByLabel('Note 1 duration seconds',{exact:true}).fill('0.4');
  await page.getByLabel('Note 1 part',{exact:true}).fill('review-part');
  await page.getByLabel('Note 1 voice',{exact:true}).selectOption('2');
  await page.getByLabel('Split at seconds',{exact:true}).fill('0.5');
  const initialCount=await count();
  await page.getByRole('button',{name:'Split note',exact:true}).click();
  equal(await count(),initialCount+1,'split adds one note');
  await page.getByRole('button',{name:'Merge notes',exact:true}).click();
  equal(await count(),initialCount,'valid adjacent same-pitch split fragments merge');
  equal(await editor.locator('tbody tr').first().getAttribute('data-note-id'),firstId,'split/merge retains original stable ID');
  await page.getByRole('button',{name:'Add note',exact:true}).click();equal(await count(),initialCount+1,'add creates note');
  await page.getByRole('button',{name:'Undo edit',exact:true}).click();equal(await count(),initialCount,'undo add');
  await page.getByRole('button',{name:'Redo edit',exact:true}).click();equal(await count(),initialCount+1,'redo add');
  await page.getByRole('button',{name:'Delete selected',exact:true}).click();equal(await count(),initialCount,'delete selected');
  await page.getByRole('button',{name:'Undo edit',exact:true}).click();equal(await count(),initialCount+1,'undo deletion');
  await page.getByRole('button',{name:'Redo edit',exact:true}).click();equal(await count(),initialCount,'redo deletion');
  await page.getByLabel('Select note 1',{exact:true}).check();
  await source.selectOption('edited');await rewind();
  const tonesBefore=await page.evaluate(()=>window.__reviewTones.length);
  await play();await until(async()=>await pos()>0.35,'edited playback reaches selected note');await pause();
  check(await page.evaluate(({from,hz})=>window.__reviewTones.slice(from).some(t=>Math.abs(t.pitchHz-hz)<0.01),{from:tonesBefore,hz:440*2**((targetPitch-69)/12)}),'unsaved pitch edit changes actual oscillator frequency');
  await page.getByLabel('Loop start seconds',{exact:true}).fill('0.3');
  await page.getByLabel('Loop end seconds',{exact:true}).fill('0.65');
  await page.getByLabel('Loop region',{exact:true}).check();await rewind();await play();
  const positions=[];for(let i=0;i<9;i++){await page.waitForTimeout(100);positions.push(await pos());}await pause();
  check(positions.every(p=>p>=0.29&&p<=0.66),'synth loop stays inside selected region');
  check(positions.some((p,i)=>i>0&&p<positions[i-1]),'loop cursor visibly wraps');
  await page.getByLabel('Loop region',{exact:true}).uncheck();
  const previewResponse=page.waitForResponse(r=>r.url().endsWith('/review/preview')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Preview score',exact:true}).click();
  const preview=await previewResponse;equal(preview.status(),200,await preview.text());await rendered(page);
  const previewData=await preview.json();
  check(previewData.score.source_notes.some(n=>n.id===firstId&&n.pitch===targetPitch),'notation preview includes edited note');
  equal((await getProject(project.id)).notes[0].pitch,firstPitch,'preview does not save edits');
  await source.selectOption('score');await rewind();await play();await until(async()=>await pos()>0.35,'quantized score playback advances');await pause();
  console.log('Saving notes and score atomically; verifying exports and reopening…');
  const savedResponse=page.waitForResponse(r=>r.url().endsWith('/review')&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Save review',exact:true}).first().click();
  const response=await savedResponse;equal(response.status(),200,await response.text());
  const saved=await response.json();
  equal(saved.project.notes_revision,completed.notes_revision+1,'note revision advances');
  equal(saved.score_state.source_notes_revision,saved.project.notes_revision,'score and notes saved in same revision transaction');
  const edited=saved.project.notes.find(n=>n.id===firstId);
  equal(edited.pitch,targetPitch,'pitch persisted');equal(edited.start,0.3,'start persisted');
  check(Math.abs(edited.end-0.7)<1e-8,'duration persisted');equal(edited.part_id,'review-part','part persisted');equal(edited.voice_id,'2','voice persisted');
  for(const format of ['musicxml','midi','json','pdf']){
    const artifact=await fetch(base+`/score/exports/${format}?revision=${saved.score_state.revision}`);
    equal(artifact.status,200,`${format}: ${artifact.status===200?'available':await artifact.text()}`);
    equal(artifact.headers.get('X-Score-Revision'),String(saved.score_state.revision),`${format} identifies saved score revision`);
    equal(artifact.headers.get('X-Notes-Revision'),String(saved.project.notes_revision),`${format} identifies source notes revision`);
    await writeFile(path.join(output,`edited-score.${format==='midi'?'mid':format}`),Buffer.from(await artifact.arrayBuffer()));
  }
  const parsed=JSON.parse(await runPython(`import json,sys
from fractions import Fraction
from music21 import converter
import pretty_midi
s=converter.parse(sys.argv[1]);m=pretty_midi.PrettyMIDI(sys.argv[2]);c=json.load(open(sys.argv[3]));source=sys.argv[4]
f=lambda r:Fraction(r['numerator'],r['denominator'])
events=[(bar,e) for p in c['parts'] for staff in p['staves'] for bar in staff['measures'] for voice in bar['voices'] for e in voice['events'] if source in e['source_note_ids']]
start=min(float(f(bar['start'])+f(e['offset'])) for bar,e in events)*60/c['tempo']['bpm'];end=max(float(f(bar['start'])+f(e['offset'])+f(e['duration'])) for bar,e in events)*60/c['tempo']['bpm'];pitch=int(sys.argv[5])
matched=any(n.pitch==pitch and abs(n.start-start)<.002 and abs(n.end-end)<.002 for i in m.instruments for n in i.notes)
print(json.dumps({'xml_notes':len(s.recurse().notes),'edited_pitch_in_xml':pitch in [p.midi for n in s.recurse().notes for p in n.pitches],'midi_edited_pitch_and_timing':matched,'expected_start':start,'expected_end':end}))`,[path.join(output,'edited-score.musicxml'),path.join(output,'edited-score.mid'),path.join(output,'edited-score.json'),firstId,String(targetPitch)]));
  check(parsed.xml_notes>0&&parsed.edited_pitch_in_xml,'MusicXML reparses with edited pitch');check(parsed.midi_edited_pitch_and_timing,'MIDI edited pitch and timing match saved rational score');
  const pdfCheck=JSON.parse(await runPython(`import json,sys
from pypdf import PdfReader
p=PdfReader(sys.argv[1]);print(json.dumps({'pages':len(p.pages),'boxes':[[float(x.mediabox.width),float(x.mediabox.height)] for x in p.pages],'text':''.join(x.extract_text() or '' for x in p.pages)}))`,[path.join(output,'edited-score.pdf')]));
  check(pdfCheck.pages>=1&&pdfCheck.boxes.every(([w,h])=>w>500&&h>700),'PDF has real full-size page objects');
  const staleRevision=await fetch(base+`/score/exports/musicxml?revision=${saved.score_state.revision-1}`);
  equal(staleRevision.status,409,'download from an older displayed score revision is rejected');
  equal(hash(Buffer.from(await(await fetch(base+'/exports/raw-midi')).arrayBuffer())),hash(rawMidi),'raw MIDI immutable under review edits');
  equal(hash(Buffer.from(await(await fetch(base+'/artifacts/raw-note-events')).arrayBuffer())),hash(rawEvents),'raw events immutable under review edits');
  if (!await page.getByText('Piano staves and note placement',{exact:true}).isVisible())
    await page.getByText('Musical settings and piano staves',{exact:true}).click();
  if (!await page.getByLabel('Piano staff split',{exact:true}).isVisible())
    await page.getByText('Piano staves and note placement',{exact:true}).click();
  await page.getByLabel('Piano staff split',{exact:true}).check();
  await page.getByLabel('Treble starts at MIDI pitch',{exact:true}).fill('64');
  const pianoResponse=page.waitForResponse(r=>r.url().endsWith('/review/preview')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Preview score',exact:true}).click();
  equal((await pianoResponse).status(),200,'piano preview generated');await rendered(page);
  for(const target of [saved.project.notes.find(n=>n.id===firstId), saved.project.notes.find(n=>n.pitch>=64)]) {
    const locator=page.locator(`[data-source-note-ids~="${target.id}"]`).first();
    await until(async()=>await locator.count()>0,'piano note/source binding');await locator.click();
    const row=saved.project.notes.findIndex(n=>n.id===target.id)+1;
    equal(await page.getByLabel(`Select note ${row}`,{exact:true}).isChecked(),true,'piano/multiple-part voice selects correct stable ID');
  }
  await page.reload({waitUntil:'networkidle'});await rendered(page);
  equal(await page.getByLabel('Note 1 MIDI pitch',{exact:true}).inputValue(),String(targetPitch),'reopen retains pitch');
  equal(await page.getByLabel('Note 1 start seconds',{exact:true}).inputValue(),'0.3','reopen retains timing');
  equal(await page.getByLabel('Note 1 part',{exact:true}).inputValue(),'review-part','reopen retains part');
  equal(await page.getByLabel('Note 1 voice',{exact:true}).inputValue(),'2','reopen retains voice');
  await page.screenshot({path:path.join(output,'review-desktop.png'),fullPage:true});
  const mobile=await browser.newContext({viewport:{width:390,height:844}}), mobilePage=await mobile.newPage();
  mobilePage.on('pageerror',e=>pageErrors.push(e.message));
  mobilePage.on('console',captureConsole);
  await mobilePage.goto(`${appUrl}/#/projects/${project.id}`,{waitUntil:'networkidle'});await rendered(mobilePage);
  check(await mobilePage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile page no horizontal overflow');
  await mobilePage.screenshot({path:path.join(output,'review-mobile.png'),fullPage:true});
  const current=await getProject(project.id);
  const oldEdit=await putJson(base+'/notes',{revision:current.notes_revision,notes:current.notes.map(n=>({id:n.id,pitch:n.pitch,start:n.start,end:n.end,velocity:n.velocity,part_id:n.part_id,voice_id:n.voice_id}))});
  equal(oldEdit.status,200,'separate performance edit succeeds');
  for(const format of ['musicxml','midi','pdf']) equal((await fetch(base+`/score/exports/${format}`)).status,409,`stale ${format} export invalidated`);
  equal(pageErrors,[],'no unhandled application/browser errors (optional favicon reported separately)');
  const report={passed:true,assertions,output,optionalIconErrors,projectId:project.id,detectedNotes:completed.notes.length,parsed,pdf:{pages:pdfCheck.pages,boxes:pdfCheck.boxes},checks:['real uploaded audio inference','actual waveform','original/raw/edited/quantized playback','original clip offset','WebAudio edited pitch and cursor','selected loop wraps','score note selection including piano split and multiple parts/voices','pitch/start/duration/part/voice editing','add/delete/split/merge undo redo','atomic save and reopen','MusicXML MIDI PDF exports with revision headers','old displayed export revision rejection','raw preservation','stale export invalidation','desktop mobile'],limitations:['Chrome automation with real WebAudio nodes; no physical listening or audio-device testing','Generated fixture input only; no transcription accuracy claim','PDF visual quality is verified separately by the PDF export tests']};
  await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
} catch(error) {
  const failedPage=browser?.contexts()[0]?.pages()[0];
  if(failedPage){await failedPage.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await writeFile(path.join(output,'failure.txt'),await failedPage.locator('body').innerText().catch(()=>String(error)));}
  console.error('Failure artifacts:',output);throw error;
} finally {
  await browser?.close();await server?.close();
  for(const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child=>new Promise(resolve=>{if(child.exitCode!==null)resolve();else{child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},5000).unref();}})));
  await writeFile(path.join(output,'service.log'),logs.join(''));
}
