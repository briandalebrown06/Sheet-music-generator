/** Real production frontend/Caddy policy over loopback HTTPS, using disposable data.
 * Caddy's internal CA is never installed; Chrome explicitly ignores this test
 * certificate. This does not qualify Docker, a public certificate, or a real domain.
 */
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require=createRequire(import.meta.url),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const python=process.env.NOTATION_TEST_PYTHON||path.join(root,'.venv/bin/python');
const caddy=process.env.CADDY_BINARY||path.join(root,'.runtime/audit-tools/caddy');
const output=await mkdtemp(path.join(tmpdir(),'scorecraft-ingress-')),data=path.join(output,'data');await mkdir(data,{recursive:true});
const origin='https://localhost:5191';
const env={...process.env,APP_MODE:'hosted',AUTH_ENABLED:'true',PUBLIC_ORIGIN:origin,DATA_DIR:data,FFMPEG_BINARY:process.env.FFMPEG_BINARY||path.join(root,'.runtime/bin/ffmpeg'),BASIC_PITCH_RUNTIME:'onnx',APP_DOMAIN:'localhost',ACME_EMAIL:'audit@example.invalid',XDG_DATA_HOME:path.join(output,'caddy-data'),XDG_CONFIG_HOME:path.join(output,'caddy-config')};
const user={username:'ingress_audit',password:randomBytes(24).toString('base64url')};
const children=[],logs=[],errors=[],csp=[];let browser,assertions=0;
function check(v,m){assertions++;assert.ok(v,m);}function equal(v,e,m){assertions++;assert.deepEqual(v,e,m);}
function start(binary,args){const p=spawn(binary,args,{cwd:root,env,stdio:['ignore','pipe','pipe']});p.stdout.on('data',x=>logs.push(x.toString()));p.stderr.on('data',x=>logs.push(x.toString()));children.push(p);return p;}
async function command(binary,args,input=''){return new Promise((resolve,reject)=>{const p=spawn(binary,args,{cwd:root,env,stdio:['pipe','pipe','pipe']});let out='',err='';p.stdout.on('data',x=>out+=x);p.stderr.on('data',x=>err+=x);p.stdin.end(input);p.once('exit',code=>code===0?resolve(out):reject(new Error(`Audit command exited ${code}: ${err}`)));});}
async function until(fn,label,ms=30000){const end=Date.now()+ms;while(Date.now()<end){const r=await fn().catch(()=>false);if(r)return r;await new Promise(resolve=>setTimeout(resolve,200));}throw new Error(`Timed out: ${label}`);}
function wave(){const rate=22050,frames=rate*4,b=Buffer.alloc(44+frames*2);b.write('RIFF');b.writeUInt32LE(b.length-8,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);b.writeUInt16LE(1,20);b.writeUInt16LE(1,22);b.writeUInt32LE(rate,24);b.writeUInt32LE(rate*2,28);b.writeUInt16LE(2,32);b.writeUInt16LE(16,34);b.write('data',36);b.writeUInt32LE(frames*2,40);for(let i=0;i<frames;i++){const t=i/rate,n=Math.floor((t-.25)/.7);if(n<0||n>3)continue;const local=t-(.25+n*.7);if(local>.5)continue;const hz=440*2**(([60,64,67,72][n]-69)/12),amp=Math.min(1,local/.02,(.5-local)/.03);b.writeInt16LE(Math.round(9000*amp*(Math.sin(2*Math.PI*hz*t)+.25*Math.sin(4*Math.PI*hz*t))),44+i*2);}return b;}
try{
  const production=await readFile(path.join(root,'deploy/Caddyfile'),'utf8');
  const config=production.replace('admin off','admin off\n\tdefault_bind 127.0.0.1\n\tskip_install_trust').replace('http_port 8080','http_port 5192').replace('https_port 8443','https_port 5191').replace('{$APP_DOMAIN} {','https://localhost:5191 {\n\tbind 127.0.0.1\n\ttls internal').replace('reverse_proxy api:8000','reverse_proxy 127.0.0.1:8002').replace('root * /srv',`root * "${path.join(root,'frontend/dist')}"`);
  const configPath=path.join(output,'Caddyfile');await writeFile(configPath,config);
  await command(caddy,['validate','--config',configPath,'--adapter','caddyfile']);
  const adapted=JSON.parse(await command(caddy,['adapt','--config',configPath,'--adapter','caddyfile']));
  const listens=Object.values(adapted.apps.http.servers).flatMap(s=>s.listen);
  check(listens.every(address=>address.startsWith('127.0.0.1:')),'adapted Caddy listeners bind only loopback');
  const version=(await command(caddy,['version'])).trim();
  await command(python,['-c',`import json,sys\nfrom backend.app.config import Settings\nfrom backend.app.auth_store import AuthStore\ns=Settings.from_env();s.prepare();a=AuthStore(s.database_path);a.initialize();u=json.load(sys.stdin);a.create_user(u['username'],u['password'])`],JSON.stringify(user));
  start(python,['-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8002','--proxy-headers','--forwarded-allow-ips','127.0.0.1','--no-access-log']);
  start(python,['-m','backend.app.worker']);start(caddy,['run','--config',configPath,'--adapter','caddyfile']);
  browser=await chromium.launch({headless:true,channel:process.env.CHROME_CHANNEL||'chrome'});
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}}),page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error'&&/content security policy|refused to/i.test(m.text()))csp.push(m.text());});
  await context.addInitScript(()=>{
    window.__exportBlobs=new Set();
    const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
    URL.createObjectURL=(blob)=>{const url=create(blob);if(/application\/pdf|audio\/midi|recordare/.test(blob.type))window.__exportBlobs.add(url);return url;};
    URL.revokeObjectURL=(url)=>{window.__exportBlobs.delete(url);revoke(url);};
    window.__cspViolations=[];document.addEventListener('securitypolicyviolation',event=>window.__cspViolations.push({directive:event.violatedDirective,blocked:event.blockedURI}));});
  await until(async()=>(await context.request.get(origin+'/api/health')).ok(),'Caddy HTTPS');
  const initial=await page.goto(origin,{waitUntil:'networkidle'});await page.getByRole('heading',{name:'Sign in to your studio'}).waitFor();
  check(initial.headers()['content-security-policy']?.includes("script-src 'self'"),'production CSP delivered');check(initial.headers()['strict-transport-security']?.includes('max-age=31536000'),'production HSTS delivered');check(initial.headers()['permissions-policy']?.includes('microphone=(self)'),'microphone self policy delivered');check(await page.evaluate(()=>window.isSecureContext),'browser recognizes HTTPS secure context');
  await page.getByLabel('Username',{exact:true}).fill(user.username);await page.getByLabel('Password',{exact:true}).fill(user.password);
  // Supply a forged incoming forwarding address; Caddy must replace it, and
  // Uvicorn trusts only this loopback test ingress, just as deployment trusts
  // its one private Caddy address.
  await page.setExtraHTTPHeaders({'X-Forwarded-For':'203.0.113.99'});
  const loginResponse=page.waitForResponse(r=>r.url().endsWith('/api/auth/login')&&r.request().method()==='POST');await page.getByRole('button',{name:'Sign in',exact:true}).click();const login=await loginResponse;equal(login.status(),200,'real hosted login through HTTPS');await page.getByRole('button',{name:'Sign out',exact:true}).waitFor();await page.setExtraHTTPHeaders({});
  const cookie=(await context.cookies()).find(c=>c.name==='__Host-scorecraft_session');check(cookie?.secure&&cookie.httpOnly&&cookie.sameSite==='Lax'&&cookie.path==='/'&&cookie.domain==='localhost','browser accepts Secure HttpOnly host-only session cookie');check(!await page.evaluate(()=>document.cookie.includes('scorecraft_session')),'session cookie unavailable to JavaScript');
  const buckets=JSON.parse(await command(python,['-c',`import json,sqlite3\nfrom backend.app.config import Settings\nc=sqlite3.connect(Settings.from_env().database_path);print(json.dumps([r[0] for r in c.execute('select key_hash from auth_rate_buckets')]))`]));
  check(buckets.includes(createHash('sha256').update('login-client:127.0.0.1').digest('hex')),'login rate bucket uses actual loopback client');check(!buckets.includes(createHash('sha256').update('login-client:203.0.113.99').digest('hex')),'forged forwarding address cannot choose rate bucket');
  const file=path.join(output,'DEVELOPMENT-ingress.wav');await writeFile(file,wave());await page.locator('input[type=file]').setInputFiles(file);await page.getByLabel('Project name',{exact:true}).fill('[Development fixture] HTTPS ingress');await page.getByLabel('Clip start (seconds)',{exact:true}).fill('0.1');await page.getByLabel('Clip end (seconds)',{exact:true}).fill('3.6');await page.getByRole('button',{name:'Play selected clip',exact:true}).click();await until(async()=>await page.locator('audio').first().evaluate(e=>e.currentTime>.2),'blob audio permitted by production CSP');await page.getByRole('button',{name:'Pause selected clip',exact:true}).click();check(await page.locator('audio').first().evaluate(e=>e.src.startsWith('blob:')),'uploaded preview uses blob URL');
  const createdResponse=page.waitForResponse(r=>r.url().endsWith('/api/projects')&&r.request().method()==='POST');await page.getByRole('button',{name:'Create transcription',exact:true}).click();const created=await createdResponse;equal(created.status(),201,'HTTPS authenticated upload passes CSRF/origin');const project=await created.json();
  const completed=await until(async()=>{const response=await context.request.get(origin+`/api/projects/${project.id}`);if(!response.ok())return false;const p=await response.json();return ['completed','failed'].includes(p.status)?p:false;},'real worker inference',180000);equal(completed.status,'completed',JSON.stringify(completed.jobs));check(completed.notes.length>0,'real inference produced notes');
  await page.reload({waitUntil:'networkidle'});await until(async()=>await page.getByTestId('score-renderer').locator('svg path').count()>0,'OSMD under production CSP');check(await page.getByRole('region',{name:'Note event editor',exact:true}).isVisible(),'authenticated editable review visible');
  const firstPitch=completed.notes[0].pitch+1;await page.getByLabel('Note 1 MIDI pitch',{exact:true}).fill(String(firstPitch));const savedResponse=page.waitForResponse(r=>r.url().endsWith('/review')&&r.request().method()==='PUT');await page.getByRole('button',{name:'Save review',exact:true}).first().click();equal((await savedResponse).status(),200,'HTTPS authenticated edit/save passes CSRF');
  const exportUrl=origin+`/api/projects/${project.id}/score/exports/musicxml`;const score=await context.request.get(exportUrl);equal(score.status(),200,'protected score exported through Caddy');check((await score.text()).includes('<score-partwise'),'export contains actual MusicXML');check(score.headers()['cache-control']?.includes('no-store'),'private export carries no-store through ingress');
  const sheet=page.getByRole('region',{name:'Your sheet music',exact:true});
  for(const [label,extension] of [['Download PDF','pdf'],['Download MIDI','mid'],['Download MusicXML (editable sheet music)','musicxml']]){
    const downloaded=page.waitForEvent('download');await sheet.getByRole('button',{name:label,exact:true}).click();
    const file=await downloaded;equal(await file.failure(),null,`${label} succeeds through hosted HTTPS/CSP`);
    check(file.suggestedFilename().endsWith(`.${extension}`)&&file.suggestedFilename().includes('_score-r'),`${label} preserves descriptive revision filename`);
    await file.saveAs(path.join(output,`hosted-score.${extension}`));
  }
  equal(await page.evaluate(()=>window.__exportBlobs.size),3,'three private generated export blobs are tracked before logout');
  await page.evaluate(()=>{window.scrollTo(0,0);document.activeElement?.blur();});
  await page.screenshot({path:path.join(output,'https-review.png'),fullPage:true});const violations=await page.evaluate(()=>window.__cspViolations);equal(violations,[],'no browser CSP violations during preview/OSMD/review');equal(csp,[],'no CSP refusal errors');equal(errors,[],'no application exceptions');
  const anonymous=await browser.newContext({ignoreHTTPSErrors:true});equal((await anonymous.request.get(exportUrl)).status(),401,'anonymous export denied through HTTPS ingress');await page.getByRole('button',{name:'Sign out',exact:true}).click();await page.getByRole('heading',{name:'Sign in to your studio'}).waitFor();equal((await context.request.get(exportUrl)).status(),401,'logout revokes export access through ingress');equal((await context.cookies()).filter(c=>c.name==='__Host-scorecraft_session').length,0,'Secure host-only cookie removed on logout');check(!logs.join('').includes(user.password),'generated credential absent from service logs');equal(await page.evaluate(()=>window.__exportBlobs.size),0,'logout revokes generated private export blobs');
  const report={passed:true,assertions,output,caddy_version:version,config_sha256:createHash('sha256').update(production).digest('hex'),listeners:listens,cookie:{name:cookie.name,secure:cookie.secure,httpOnly:cookie.httpOnly,sameSite:cookie.sameSite,path:cookie.path},checks:['production frontend build and Caddy policy over real loopback HTTPS','hosted login and Secure __Host cookie delivery','untrusted forwarding address overwritten before rate-limit identity','blob preview and real model processing','OSMD render/edit/save under production CSP','one-click PDF/MIDI/MusicXML browser downloads under production CSP','protected export and logout with private blob cleanup','no credential logs or CSP violations'],limitations:['Internal self-signed test certificate with browser ignoreHTTPSErrors; no operating-system trust installation','Native macOS Caddy/API/worker, not Docker or Linux','Loopback only; no real domain, public ACME issuance, firewall, cloud volume or production capacity qualification']};await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
}catch(error){const page=browser?.contexts()[0]?.pages()[0];if(page){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await writeFile(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>String(error)));}console.error('Failure artifacts:',output);throw error;
}finally{await browser?.close();for(const child of children)child.kill('SIGTERM');await Promise.all(children.map(child=>new Promise(resolve=>{if(child.exitCode!==null)resolve();else{child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve();},5000).unref();}})));await writeFile(path.join(output,'service.log'),logs.join(''));}
