/**
 * Real audio-input browser integration check.
 * Launches an isolated API/worker on 8001 and Vite on 5174; no user projects or
 * hardware microphones are accessed. Requires Python analysis dependencies,
 * FFmpeg, installed frontend dependencies, Playwright and Google Chrome.
 *
 * AUDIO_TEST_PYTHON defaults to python3.11. FFMPEG_BINARY defaults to ffmpeg.
 * PLAYWRIGHT_MODULE, CHROME_CHANNEL, SMOKE_ARTIFACT_DIR are optional overrides.
 * Media fixtures are generated test inputs; inference is never faked.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { createServer } = await import(path.join(root, 'frontend/node_modules/vite/dist/node/index.js'));
const python = process.env.AUDIO_TEST_PYTHON || 'python3.11';
const ffmpeg = process.env.FFMPEG_BINARY || 'ffmpeg';
const output = process.env.SMOKE_ARTIFACT_DIR || await mkdtemp(path.join(tmpdir(), 'scorecraft-audio-input-'));
const data = path.join(output, 'data');
const fixtures = path.join(output, 'fixtures');
await mkdir(fixtures, { recursive: true });
const env = { ...process.env, DATA_DIR: data, AUDIO_TEST_DATA_DIR: data, FFMPEG_BINARY: ffmpeg, BASIC_PITCH_RUNTIME: 'onnx' };
const appUrl = 'http://127.0.0.1:5174';
const apiUrl = 'http://127.0.0.1:8001';
const children = [];
const logs = [];
const checks = [];
const codecResults = [];
const errors = [];
let server;
let browser;
let currentPage;
let assertions = 0;
function check(condition, message) { assertions++; assert.ok(condition, message); }
function equal(actual, expected, message) { assertions++; assert.deepEqual(actual, expected, message); }
function start(args) {
  const child = spawn(python, args, { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', chunk => logs.push(chunk.toString()));
  child.stderr.on('data', chunk => logs.push(chunk.toString()));
  children.push(child);
  return child;
}
async function until(fn, description, timeout = 20000) {
  const end = Date.now() + timeout;
  let value;
  while (Date.now() < end) {
    try { value = await fn(); if (value) return value; } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out: ${description}`);
}
async function detail(id) { return (await fetch(`${apiUrl}/api/projects/${id}`)).json(); }
async function complete(id) {
  const result = await until(async () => {
    const value = await detail(id);
    return ['completed', 'failed'].includes(value.status) ? value : false;
  }, `project ${id} reaches a terminal status`, 90000);
  equal(result.status, 'completed', JSON.stringify(result.jobs));
  return result;
}
function tone(seconds = 6) {
  const rate = 22050, samples = rate * seconds;
  const bytes = Buffer.alloc(44 + samples * 2);
  bytes.write('RIFF', 0); bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write('WAVEfmt ', 8); bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(rate, 24); bytes.writeUInt32LE(rate * 2, 28);
  bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36); bytes.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) {
    const t = i / rate, local = t % 3, hz = t < 3 ? 261.625565 : 329.627557;
    const envelope = Math.max(0, Math.min(1, local / .03, (2.8 - local) / .08));
    const signal = [1, 2, 3].reduce((sum, n) => sum + Math.sin(2 * Math.PI * hz * n * t) / n, 0);
    bytes.writeInt16LE(Math.round(8000 * envelope * signal), 44 + 2 * i);
  }
  return bytes;
}
async function setupPage(context) {
  const page = await context.newPage();
  currentPage = page;
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  await page.getByRole('heading', { level: 1 }).waitFor();
  return page;
}
async function playAudio(page, selector = 'audio') {
  const audio = page.locator(selector).first();
  await audio.evaluate(async element => { element.currentTime = 0; await element.play(); });
  try {
    await until(() => audio.evaluate(element => element.currentTime > .15 && !element.paused), 'audio time advances');
  } catch (error) {
    const state = await audio.evaluate(element => ({ currentTime: element.currentTime, paused: element.paused, ended: element.ended, readyState: element.readyState, duration: element.duration, error: element.error?.message, src: element.currentSrc }));
    throw new Error(`${error.message}; media state: ${JSON.stringify(state)}`);
  }
  await audio.evaluate(element => element.pause());
  check(true, 'browser playback advances and pauses');
}
async function submit(page) {
  const response = page.waitForResponse(response => response.url().endsWith('/api/projects') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create transcription', exact: true }).click();
  const result = await response;
  equal(result.status(), 201, await result.text());
  const project = await result.json();
  await page.waitForURL(`**#/projects/${project.id}`);
  return project;
}

try {
  // Inputs only: actual codec outputs from the installed FFmpeg.
  const source = path.join(fixtures, 'development-fixture-tones.wav');
  await writeFile(source, tone());
  const files = [{ path: source, format: 'wav' }];
  for (const [format, codec] of [['mp3', 'libmp3lame'], ['m4a', 'aac'], ['flac', 'flac']]) {
    const destination = path.join(fixtures, `development-fixture-tones.${format}`);
    try {
      execFileSync(ffmpeg, ['-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', source, '-c:a', codec, destination], { timeout: 15000 });
      files.push({ path: destination, format });
    } catch (error) { codecResults.push({ format, result: 'not tested: fixture encoder unavailable', error: error.message }); }
  }
  start(['-m', 'uvicorn', 'scripts.audio_test_server:app', '--host', '127.0.0.1', '--port', '8001']);
  await until(async () => (await fetch(`${apiUrl}/api/health`)).ok, 'isolated API starts');
  start(['-m', 'backend.app.worker']);
  await until(async () => (await (await fetch(`${apiUrl}/api/health`)).json()).worker.online, 'isolated worker starts');
  server = await createServer({ configFile: path.join(root, 'frontend/vite.config.ts'), root: path.join(root, 'frontend'), server: { host: '127.0.0.1', port: 5174, strictPort: true, proxy: { '/api': { target: apiUrl, changeOrigin: false } } } });
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome', args: ['--use-fake-device-for-media-stream'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const page = await setupPage(context);

  for (const file of files) {
    console.log(`Checking ${file.format.toUpperCase()} upload, crop and playback…`);
    await page.goto(appUrl, { waitUntil: 'networkidle' });
    await page.locator('input[type=file]').setInputFiles(file.path);
    await page.getByLabel('Project name').fill(`[Development fixture] ${file.format.toUpperCase()} clip`);
    await playAudio(page);
    await page.getByLabel('Clip start (seconds)', { exact: true }).fill('1');
    await page.getByLabel('Clip end (seconds)', { exact: true }).fill('4');
    await page.getByText('Optional musical hints', { exact: true }).click();
    await page.getByLabel('Instrument hint', { exact: true }).selectOption('piano');
    await page.getByLabel('Tempo hint (BPM)', { exact: true }).fill('90');
    await page.getByLabel('Time signature hint', { exact: true }).selectOption('3/4');
    await page.getByRole('button', { name: 'Play selected clip', exact: true }).click();
    await until(() => page.locator('audio').first().evaluate(element => element.currentTime >= 1.15 && !element.paused), 'selected clip begins at the selected offset');
    await until(() => page.locator('audio').first().evaluate(element => element.paused && element.currentTime >= 3.9 && element.currentTime <= 4.2), 'selected playback stops at end', 10000);
    check(true, `${file.format} selected playback respects start/end`);
    if (file.format === 'wav') await page.screenshot({ path: path.join(output, 'clip-desktop.png'), fullPage: true });
    const project = await submit(page);
    const done = await complete(project.id);
    equal(done.clip_start_seconds, 1, 'persisted original offset');
    equal(done.clip_end_seconds, 4, 'persisted selected end');
    check(Math.abs(done.clip_duration_seconds - 3) < .1, 'normalized clip has requested duration');
    check(done.original_duration_seconds >= 5.8, 'original decoded duration retained');
    equal(done.instrument_hint, 'piano', 'instrument hint persisted');
    equal(done.tempo_hint, 90, 'tempo hint persisted');
    equal(done.time_signature_hint, '3/4', 'meter hint persisted');
    const original = Buffer.from(await (await fetch(`${apiUrl}/api/projects/${project.id}/audio`)).arrayBuffer());
    equal(createHash('sha256').update(original).digest('hex'), createHash('sha256').update(await readFile(file.path)).digest('hex'), 'original bytes preserved');
    const analysis = await fetch(`${apiUrl}/api/projects/${project.id}/analysis-audio`);
    equal(analysis.status, 200, 'normalized clip retrievable');
    check((await analysis.arrayBuffer()).byteLength < original.length || file.format !== 'wav', 'normalized copy is cropped');
    await page.reload({ waitUntil: 'networkidle' });
    equal((await detail(project.id)).clip_start_seconds, 1, 'offset survives browser refresh');
    await playAudio(page, 'audio[aria-label="Original recording"]');
    codecResults.push({ format: file.format, result: 'upload, decode, clip, playback and persistence passed' });
  }
  checks.push('real WAV/MP3/M4A/FLAC browser uploads and installed FFmpeg decoding', 'selected-clip preview start/stop and original playback', 'original bytes, clip offset, hints, normalized audio and refresh persistence');

  // Force only the native preview error branch; the fallback request/FFmpeg
  // decode and resulting WAV playback are real. This does not claim a real
  // Chrome codec failure for the fixture format.
  console.log('Checking local preview fallback…');
  const countBeforePreview = (await (await fetch(`${apiUrl}/api/projects`)).json()).length;
  await page.goto(appUrl, { waitUntil: 'networkidle' });
  await page.locator('input[type=file]').setInputFiles(source);
  await page.locator('audio').evaluate(element => element.dispatchEvent(new Event('error')));
  const prepared = page.waitForResponse(response => response.url().endsWith('/api/audio-preview') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Prepare playable preview', exact: true }).click();
  equal((await prepared).status(), 200, 'local preview decode succeeds');
  await playAudio(page);
  equal((await (await fetch(`${apiUrl}/api/projects`)).json()).length, countBeforePreview, 'preview decode creates no project or analysis job');
  checks.push('real local FFmpeg preview fallback after a forced native-preview error');


  console.log('Checking MediaRecorder capture and cleanup…');
  const micContext = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1280, height: 1000 } });
  await micContext.addInitScript(() => {
    window.__audioTest = { calls: 0, tracks: [], mimeChecks: [] };
    const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      window.__audioTest.calls++;
      const stream = await get(constraints);
      window.__audioTest.tracks.push(...stream.getTracks());
      return stream;
    };
    const supports = MediaRecorder.isTypeSupported.bind(MediaRecorder);
    MediaRecorder.isTypeSupported = type => { window.__audioTest.mimeChecks.push(type); return supports(type); };
  });
  const micPage = await setupPage(micContext);
  await micPage.getByRole('tab', { name: 'Record audio' }).click();
  equal(await micPage.evaluate(() => window.__audioTest.calls), 0, 'microphone is not requested on mount or source selection');
  let submissions = 0;
  micPage.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/projects')) submissions++; });
  await micPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await micPage.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
  await micPage.waitForTimeout(2200);
  equal(submissions, 0, 'no live streaming or project submission during recording');
  check(await micPage.getByRole('button', { name: 'Create transcription', exact: true }).isDisabled(), 'cannot submit while recording');
  await micPage.getByRole('button', { name: 'Stop recording', exact: true }).click();
  await micPage.getByLabel('Project name').waitFor();
  equal(await micPage.evaluate(() => window.__audioTest.tracks.map(track => track.readyState)), ['ended'], 'tracks stopped after recording stops');
  check(await micPage.evaluate(() => window.__audioTest.mimeChecks.length > 0), 'runtime probes supported MIME types');
  equal(submissions, 0, 'stopping does not automatically submit');
  await playAudio(micPage);
  await micPage.getByLabel('Project name').fill('[Development fixture] Real MediaRecorder capture');
  const captured = await submit(micPage);
  equal(captured.source, 'microphone', 'real recording source persisted');
  equal(submissions, 1, 'one explicit microphone submission');
  await until(async () => (await detail(captured.id)).analysis_available, 'recorded WebM normalized');
  checks.push('runtime MIME selection and real MediaRecorder start/stop/preview/submit with Chrome test microphone');

  // Discard and active-component unmount must release capture tracks.
  await micPage.goto(appUrl, { waitUntil: 'networkidle' });
  await micPage.getByRole('tab', { name: 'Record audio' }).click();
  await micPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await micPage.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
  await micPage.waitForTimeout(500);
  await micPage.getByRole('button', { name: 'Stop recording', exact: true }).click();
  await micPage.getByLabel('Project name').waitFor();
  await micPage.getByRole('button', { name: /discard recording|discard audio|remove audio/i }).click();
  equal(await micPage.locator('audio').count(), 0, 'discard clears recording preview');
  check(await micPage.getByRole('button', { name: 'Create transcription', exact: true }).isDisabled(), 'discard disables submission');
  await micPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await micPage.getByRole('button', { name: 'Stop recording', exact: true }).waitFor();
  await micPage.getByRole('link', { name: /My projects/ }).click();
  await until(() => micPage.evaluate(() => window.__audioTest.tracks.every(track => track.readyState === 'ended')), 'unmount releases all microphone tracks');
  check(true, 'component unmount releases microphone tracks');
  checks.push('no automatic capture/submission, discard reset and microphone track cleanup');

  console.log('Checking deferred microphone cleanup…');
  const pendingContext = await browser.newContext({ permissions: ['microphone'] });
  await pendingContext.addInitScript(() => {
    window.__pendingTracks = [];
    const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await get(constraints);
      window.__pendingTracks = stream.getTracks();
      await new Promise(resolve => { window.__resolveMicrophone = resolve; });
      return stream;
    };
  });
  const pendingPage = await setupPage(pendingContext);
  await pendingPage.getByRole('tab', { name: 'Record audio' }).click();
  await pendingPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await until(() => pendingPage.evaluate(() => typeof window.__resolveMicrophone === 'function'), 'test microphone stream waits for deferred resolution');
  await pendingPage.getByRole('button', { name: 'Cancel microphone request', exact: true }).click();
  await pendingPage.evaluate(() => window.__resolveMicrophone());
  await until(() => pendingPage.evaluate(() => window.__pendingTracks.every(track => track.readyState === 'ended')), 'late stream tracks end after request cancellation');
  check(true, 'canceling a pending request cleans up its late stream');
  await pendingPage.reload({ waitUntil: 'networkidle' });
  await pendingPage.getByRole('tab', { name: 'Record audio' }).click();
  await pendingPage.getByRole('button', { name: 'Start recording', exact: true }).click();
  await until(() => pendingPage.evaluate(() => typeof window.__resolveMicrophone === 'function'), 'second test microphone stream is pending');
  await pendingPage.getByRole('link', { name: /My projects/ }).click();
  await pendingPage.evaluate(() => window.__resolveMicrophone());
  await until(() => pendingPage.evaluate(() => window.__pendingTracks.every(track => track.readyState === 'ended')), 'late stream tracks end after unmount');
  check(true, 'unmounted component cleans up its late microphone stream');
  await pendingContext.close();
  checks.push('late microphone stream cleanup after cancellation and unmount');


  // Test controlled browser failure branches. These are mocked error responses,
  // not claims about permission prompts or missing hardware on physical devices.
  console.log('Checking controlled browser error branches…');
  for (const [kind, expected] of [
    ['NotAllowedError', /denied|permission/i],
    ['NotFoundError', /no microphone|microphone.*connected|could not find/i],
    ['unsupported', /unsupported|not support|unavailable/i],
    ['insecure', /HTTPS|secure|localhost/i],
  ]) {
    const failure = await browser.newContext();
    await failure.addInitScript(kind => {
      if (kind === 'unsupported') Object.defineProperty(window, 'MediaRecorder', { value: undefined });
      else if (kind === 'insecure') {
        Object.defineProperty(window, 'isSecureContext', { value: false });
        Object.defineProperty(navigator, 'mediaDevices', { value: undefined });
      } else navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Explicit browser test fixture', kind); };
    }, kind);
    const failurePage = await setupPage(failure);
    await failurePage.getByRole('tab', { name: 'Record audio' }).click();
    const startButton = failurePage.getByRole('button', { name: 'Start recording', exact: true });
    if (!(await startButton.isDisabled())) await startButton.click();
    await failurePage.getByRole('alert').filter({ hasText: expected }).waitFor();
    check(true, `${kind} shows an actionable error`);
    await failure.close();
  }
  checks.push('mocked denial, missing microphone, unsupported recorder and insecure-context error handling');

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobilePage = await setupPage(mobile);
  await mobilePage.locator('input[type=file]').setInputFiles(source);
  await mobilePage.getByLabel('Project name').waitFor();
  await mobilePage.getByLabel('Clip end (seconds)', { exact: true }).fill('0');
  check(await mobilePage.getByRole('button', { name: 'Create transcription', exact: true }).isDisabled(), 'zero-length clip cannot submit');
  await mobilePage.getByLabel('Clip end (seconds)', { exact: true }).fill('999');
  check(await mobilePage.getByRole('button', { name: 'Create transcription', exact: true }).isDisabled(), 'out-of-range clip cannot submit');
  await mobilePage.getByLabel('Clip end (seconds)', { exact: true }).fill('4');
  check(await mobilePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile crop form has no horizontal overflow');
  await mobilePage.screenshot({ path: path.join(output, 'clip-mobile.png'), fullPage: true });
  checks.push('invalid clip ranges and mobile layout');
  equal(errors, [], 'no uncaught browser exceptions');
  const result = { passed: true, assertions, checks, codecResults, browser: 'Google Chrome through Playwright; test microphone only, no physical-device validation', artifacts: output };
  await writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  if (currentPage && !currentPage.isClosed()) await currentPage.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  console.error(`Artifacts: ${output}`);
  throw error;
} finally {
  if (browser) await browser.close();
  if (server) await server.close();
  for (const child of children) child.kill('SIGTERM');
  await Promise.all(children.map(child => new Promise(resolve => {
    if (child.exitCode !== null) return resolve();
    child.once('exit', resolve);
    setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 10000).unref();
  })));
  await writeFile(path.join(output, 'server.log'), logs.join(''));
}
