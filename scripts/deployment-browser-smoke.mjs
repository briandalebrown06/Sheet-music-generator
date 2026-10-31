/** Deployment-boundary audit against real isolated API/worker and generated audio.
 * Accounts/passwords are ephemeral, random, and never included in reports/logs.
 * Local AUTH_ENABLED exercises cookie authentication; it is not an HTTPS test.
 */
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { access, mkdir, mkdtemp, writeFile } from 'node:fs/promises';
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
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-deployment-'));
const data = path.join(output, 'data');
await mkdir(data, { recursive: true });
const appUrl='http://127.0.0.1:5174',apiUrl='http://127.0.0.1:8001';
const env={...process.env,DATA_DIR:data,AUDIO_TEST_DATA_DIR:data,FFMPEG_BINARY:ffmpeg,BASIC_PITCH_RUNTIME:'onnx',APP_MODE:'local',AUTH_ENABLED:'true',PUBLIC_ORIGIN:appUrl};
const users=[{username:'audit_alice',password:randomBytes(24).toString('base64url')},{username:'audit_bob',password:randomBytes(24).toString('base64url')}];
const children=[],logs=[],pageErrors=[];let assertions=0,browser,server;
function check(value,label){assertions++;assert.ok(value,label);}
function equal(value,expected,label){assertions++;assert.deepEqual(value,expected,label);}
function start(args){const child=spawn(python,args,{cwd:root,env,stdio:['ignore','pipe','pipe']});child.stdout.on('data',x=>logs.push(x.toString()));child.stderr.on('data',x=>logs.push(x.toString()));children.push(child);return child;}
async function runPython(code,args=[],input=''){return new Promise((resolve,reject)=>{const child=spawn(python,['-c',code,...args],{cwd:root,env,stdio:['pipe','pipe','pipe']});let out='',err='';child.stdout.on('data',x=>out+=x);child.stderr.on('data',x=>err+=x);child.stdin.end(input);child.once('exit',exit=>exit===0?resolve(out):reject(new Error(`Audit Python ${exit}: ${err}`)));});}
async function until(fn,label,timeout=30000){const end=Date.now()+timeout;while(Date.now()<end){const value=await fn().catch(()=>false);if(value)return value;await new Promise(r=>setTimeout(r,150));}throw new Error(`Timed out: ${label}`);}
function fixture(){const rate=22050,length=5,bytes=Buffer.alloc(44+rate*length*2);bytes.write('RIFF');bytes.writeUInt32LE(bytes.length-8,4);bytes.write('WAVEfmt ',8);bytes.writeUInt32LE(16,16);bytes.writeUInt16LE(1,20);bytes.writeUInt16LE(1,22);bytes.writeUInt32LE(rate,24);bytes.writeUInt32LE(rate*2,28);bytes.writeUInt16LE(2,32);bytes.writeUInt16LE(16,34);bytes.write('data',36);bytes.writeUInt32LE(bytes.length-44,40);const pitches=[60,62,64,65,67,64];for(let i=0;i<rate*length;i++){const t=i/rate;let y=0;for(let n=0;n<pitches.length;n++){const start=.3+n*.6,end=start+.4;if(t<start||t>end)continue;const a=Math.min(1,(t-start)/.02,(end-t)/.04);const hz=440*2**((pitches[n]-69)/12);y+=a*(Math.sin(2*Math.PI*hz*t)+.3*Math.sin(4*Math.PI*hz*t))*10000;}bytes.writeInt16LE(Math.round(y),44+i*2);}return bytes;}
async function login(page,user){await page.getByLabel('Username',{exact:true}).fill(user.username);await page.keyboard.press('Tab');check(await page.getByLabel('Password',{exact:true}).evaluate(e=>e===document.activeElement),'keyboard reaches password');await page.keyboard.type(user.password);await page.keyboard.press('Enter');await page.getByRole('button',{name:'Sign out',exact:true}).waitFor();}
async function api(page,suffix,options={}){return page.evaluate(async({suffix,options})=>{const s=await fetch('/api/auth/session').then(r=>r.json());const headers={...options.headers};if(options.method&&options.method!=='GET')headers['X-CSRF-Token']=s.csrf_token;const r=await fetch('/api'+suffix,{...options,headers});return {status:r.status,headers:Object.fromEntries(r.headers),text:await r.text()};},{suffix,options});}
async function rendered(page){await until(async()=>await page.getByTestId('score-renderer').locator('svg path').count()>0,'real OSMD score');}
try{
  await runPython(`import json,sys\nfrom backend.app.config import Settings\nfrom backend.app.auth_store import AuthStore\ns=Settings.from_env();s.prepare();a=AuthStore(s.database_path);a.initialize()\nfor u in json.load(sys.stdin):a.create_user(u['username'],u['password'])`,[],JSON.stringify(users));
  start(['-m','uvicorn','scripts.audio_test_server:app','--host','127.0.0.1','--port','8001','--no-access-log']);
  await until(async()=>(await fetch(apiUrl+'/api/health')).ok,'API startup');
  start(['-m','backend.app.worker']);
  server=await createServer({configFile:path.join(root,'frontend/vite.config.ts'),root:path.join(root,'frontend'),server:{host:'127.0.0.1',port:5174,strictPort:true,proxy:{'/api':{target:apiUrl,changeOrigin:false}}}});await server.listen();
  browser=await chromium.launch({headless:true,channel:process.env.CHROME_CHANNEL||'chrome'});
  const alice=await browser.newContext({viewport:{width:1440,height:1000}}),page=await alice.newPage();page.on('pageerror',e=>pageErrors.push(e.message));
  await page.goto(appUrl,{waitUntil:'networkidle'});await page.getByRole('heading',{name:'Sign in to your studio'}).waitFor();
  equal(await page.locator('input[type=file]').count(),0,'unauthenticated private upload workspace never mounts');
  equal((await fetch(apiUrl+'/api/projects')).status,401,'unauthenticated project listing denied');
  await page.screenshot({path:path.join(output,'sign-in-desktop.png'),fullPage:true});
  await login(page,users[0]);
  const cookie=(await alice.cookies()).find(c=>c.name==='scorecraft_session');check(cookie?.httpOnly&&cookie.sameSite==='Lax','local session cookie HttpOnly/SameSite');
  check(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0),'no session credentials stored in browser storage');
  await page.getByRole('link',{name:'Skip to main content'}).focus();await page.keyboard.press('Enter');equal(await page.locator('#main-content').evaluate(e=>document.activeElement===e),true,'skip link moves keyboard focus without hash-route corruption');
  const bytes=fixture(),file=path.join(output,'DEVELOPMENT-private-audit.wav');await writeFile(file,bytes);await page.locator('input[type=file]').setInputFiles(file);
  await page.getByLabel('Project name',{exact:true}).fill('[Development fixture] authenticated audit');await page.getByLabel('Clip start (seconds)',{exact:true}).fill('0.1');await page.getByLabel('Clip end (seconds)',{exact:true}).fill('4.8');
  await page.getByRole('button',{name:'Play selected clip',exact:true}).click();await until(async()=>await page.locator('audio').first().evaluate(a=>a.currentTime>.2),'pre-submission clip audition');await page.getByRole('button',{name:'Pause selected clip',exact:true}).click();
  // Lose only the first response AFTER the real server has accepted it. Retrying
  // the same UI input must reuse its idempotency key and persistent project.
  await page.evaluate(() => {
    window.__auditSubmission = { accepted: null, keys: [] };
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    const originalHeader = XMLHttpRequest.prototype.setRequestHeader;
    const requests = new WeakMap();
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      requests.set(this, { target: method === 'POST' && url.endsWith('/api/projects'), key: null });
      return originalOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
      if (name.toLowerCase() === 'idempotency-key' && requests.has(this)) requests.get(this).key = value;
      return originalHeader.call(this, name, value);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      if (requests.get(this)?.target) {
        window.__auditSubmission.keys.push(requests.get(this).key);
        const originalLoad = this.onload;
        this.onload = function (event) {
          if (!window.__auditSubmission.accepted && this.status >= 200 && this.status < 300) {
            window.__auditSubmission.accepted = JSON.parse(this.responseText);
            this.onerror?.call(this, new ProgressEvent('error'));
          } else originalLoad?.call(this, event);
        };
      }
      return originalSend.apply(this, args);
    };
  });
  await page.getByRole('button',{name:'Create transcription',exact:true}).click();await page.getByRole('alert').filter({hasText:/unavailable|interrupted/}).waitFor();
  const createdResponse=page.waitForResponse(r=>r.url().endsWith('/api/projects')&&r.request().method()==='POST');await page.getByRole('button',{name:'Create transcription',exact:true}).click();const created=await createdResponse;equal(created.status(),201,'duplicate retry returns existing project');const project=await created.json();const submission=await page.evaluate(()=>window.__auditSubmission);equal(project.id,submission.accepted.id,'lost-response retry does not create duplicate');equal(submission.keys[0],submission.keys[1],'same submission reuses idempotency key');check(!!submission.keys[0],'UI supplied idempotency key');
  const completed=await until(async()=>{const r=await api(page,`/projects/${project.id}`);const p=JSON.parse(r.text);return ['completed','failed'].includes(p.status)?p:false;},'real transcription',180000);equal(completed.status,'completed',JSON.stringify(completed.jobs));check(completed.notes.length>0,'real model produced editable notes');equal(completed.clip_start_seconds,.1,'clip offset saved');
  await page.reload({waitUntil:'networkidle'});await rendered(page);
  const scoreNote=page.locator(`[data-source-note-ids~="${completed.notes[0].id}"][tabindex="0"]`).first();
  await scoreNote.focus();await page.keyboard.press('Enter');
  check(await page.getByLabel('Select note 1',{exact:true}).isChecked(),'keyboard activates a rendered note and synchronizes editor selection');
  const seek=page.getByLabel('Review playback seek',{exact:true});const beforeSeek=Number(await seek.inputValue());
  await seek.focus();await page.keyboard.press('ArrowRight');check(Number(await seek.inputValue())>beforeSeek,'playback position is adjustable with keyboard');
  const pitch=completed.notes[0].pitch+1;await page.getByLabel('Note 1 MIDI pitch',{exact:true}).fill(String(pitch));const savedResponse=page.waitForResponse(r=>r.url().endsWith('/review')&&r.request().method()==='PUT');await page.getByRole('button',{name:'Save review',exact:true}).first().click();const savedReply=await savedResponse;equal(savedReply.status(),200,'authenticated review save');const saved=await savedReply.json();await rendered(page);
  for(const format of ['musicxml','midi','pdf']){const result=await alice.request.get(appUrl+`/api/projects/${project.id}/score/exports/${format}?revision=${saved.score_state.revision}`);equal(result.status(),200,`authenticated ${format} export`);await writeFile(path.join(output,`audit.${format==='midi'?'mid':format}`),await result.body());check(result.headers()['cache-control']?.includes('no-store'),`${format} private response not cacheable`);}
  const parsed=JSON.parse(await runPython(`import json,sys\nfrom music21 import converter\nimport pretty_midi\nfrom pypdf import PdfReader\ns=converter.parse(sys.argv[1]);m=pretty_midi.PrettyMIDI(sys.argv[2]);p=PdfReader(sys.argv[3]);pitch=int(sys.argv[4]);print(json.dumps({'xml_pitch':any(pitch==int(p.midi) for n in s.recurse().notes for p in (n.pitches if n.isChord else [n.pitch])),'midi_pitch':any(n.pitch==pitch for i in m.instruments for n in i.notes),'pdf_pages':len(p.pages)}))`,[path.join(output,'audit.musicxml'),path.join(output,'audit.mid'),path.join(output,'audit.pdf'),String(pitch)]));check(parsed.xml_pitch&&parsed.midi_pitch&&parsed.pdf_pages>=1,'edited XML MIDI and real PDF parse');
  await page.reload({waitUntil:'networkidle'});await rendered(page);equal(await page.getByLabel('Note 1 MIDI pitch',{exact:true}).inputValue(),String(pitch),'saved edits survive authenticated reopen');
  const bob=await browser.newContext({viewport:{width:390,height:844}}),bobPage=await bob.newPage();bobPage.on('pageerror',e=>pageErrors.push(e.message));await bobPage.goto(appUrl,{waitUntil:'networkidle'});await login(bobPage,users[1]);equal(JSON.parse((await api(bobPage,'/projects')).text),[],'second account sees no first-account projects');
  const protectedPaths=['', '/audio','/analysis-audio','/waveform','/score','/score/musicxml','/score/exports/pdf','/exports/raw-midi','/artifacts/raw-note-events','/stems','/separation-manifest'];
  for(const suffix of protectedPaths){equal((await api(bobPage,`/projects/${project.id}${suffix}`)).status,404,`other account denied ${suffix||'project'}`);equal((await fetch(apiUrl+`/api/projects/${project.id}${suffix}`)).status,401,`anonymous denied ${suffix||'project'}`);}
  equal((await api(bobPage,`/projects/${project.id}`,{method:'DELETE'})).status,404,'other account cannot delete');
  const missingCsrf=await alice.request.post(appUrl+`/api/projects/${project.id}/cancel`,{headers:{Origin:appUrl}});equal(missingCsrf.status(),403,'unsafe cookie request requires CSRF token');
  await runPython(`import sys\nfrom backend.app.config import Settings\nfrom backend.app.auth_store import AuthStore\ns=Settings.from_env();AuthStore(s.database_path).revoke(sys.stdin.read())`,[],cookie.value);
  await page.getByRole('heading',{name:'Sign in to your studio'}).waitFor({timeout:15000});
  check(await page.getByRole('alert').filter({hasText:'session ended'}).isVisible(),'revoked session triggers clear sign-in guidance');
  equal(await page.getByTestId('score-renderer').count(),0,'401 removes private score from the workspace');
  equal(await page.locator('audio').count(),0,'401 unmounts private audio players');
  await login(page,users[0]);await rendered(page);
  await page.setViewportSize({width:390,height:844});check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'authenticated review fits mobile width');await page.screenshot({path:path.join(output,'authenticated-review-mobile.png'),fullPage:true});
  const deleteButton=page.getByRole('button',{name:'Delete project',exact:true});await deleteButton.focus();await page.keyboard.press('Enter');const dialog=page.getByRole('dialog',{name:'Delete this project?'});await dialog.waitFor();check(await page.getByRole('button',{name:'Keep project',exact:true}).evaluate(e=>document.activeElement===e),'destructive dialog initially focuses safe action');await page.keyboard.press('Shift+Tab');check(await page.getByRole('button',{name:'Delete permanently',exact:true}).evaluate(e=>document.activeElement===e),'native modal traps reverse keyboard focus');await page.keyboard.press('Escape');check(await deleteButton.evaluate(e=>document.activeElement===e),'escape closes dialog and restores trigger focus');
  await deleteButton.click();await page.screenshot({path:path.join(output,'delete-dialog-mobile.png'),fullPage:true});const deletedResponse=page.waitForResponse(r=>r.url().endsWith(`/api/projects/${project.id}`)&&r.request().method()==='DELETE');await page.getByRole('button',{name:'Delete permanently',exact:true}).click();equal((await deletedResponse).status(),202,'deletion is durable asynchronous cleanup');await page.getByRole('heading',{name:'My projects',exact:true}).waitFor();equal((await api(page,`/projects/${project.id}`)).status,404,'deleted project access immediately denied');equal((await api(page,`/projects/${project.id}/audio`)).status,404,'deleted recording URL inaccessible');await until(async()=>{try{await access(path.join(data,'projects',project.id));return false;}catch{return true;}},'artifact directory cleanup',30000);
  const oldCookie=(await alice.cookies()).find(c=>c.name==='scorecraft_session').value;await page.getByRole('button',{name:'Sign out',exact:true}).click();await page.getByRole('heading',{name:'Sign in to your studio'}).waitFor();equal(await page.getByTestId('score-renderer').count(),0,'logout unmounts private score');equal(await page.locator('audio').count(),0,'logout removes private audio players');equal((await fetch(apiUrl+'/api/projects',{headers:{Cookie:`scorecraft_session=${oldCookie}`}})).status,401,'logout revokes the server-side session');
  await page.screenshot({path:path.join(output,'sign-in-mobile.png'),fullPage:true});check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'login fits mobile width');equal(pageErrors,[],'no unhandled browser exceptions');
  const log=logs.join('');check(users.every(u=>!log.includes(u.password)),'credentials absent from service logs');check(!log.includes('[Development fixture] authenticated audit'),'private project title absent from service logs');
  const report={passed:true,assertions,output,parsed,checks:['real authenticated upload/clip/audition/inference/edit/save/reopen/exports','lost-response idempotent retry','two-account authorization and private artifact URLs','CSRF rejection','durable deletion and actual file cleanup','revoked logout sessions','keyboard login/skip-link/score-note selection/playback seek/modal focus trapping/escape','mobile review/login/deletion layout','no credential persistence or log leakage'],limitations:['Local HTTP authentication test; secure cookie/HTTPS deployment must be tested separately','Chrome with emulated mobile viewport; no physical mobile or hardware microphone test','Generated audio, no commercial recording quality claim']};await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
}catch(error){const page=browser?.contexts()[0]?.pages()[0];if(page){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await writeFile(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>String(error)));}console.error('Failure artifacts:',output);throw error;
}finally{await browser?.close();await server?.close();for(const child of children)child.kill('SIGTERM');await Promise.all(children.map(child=>new Promise(resolve=>{if(child.exitCode!==null)resolve();else{child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},5000).unref();}})));await writeFile(path.join(output,'service.log'),logs.join(''));}
