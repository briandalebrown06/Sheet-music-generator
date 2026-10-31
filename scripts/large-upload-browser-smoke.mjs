/** Real resumable browser transfer with a generated 12-minute >25 MiB WAV.
 * Disposable API/storage only; model-inference coverage is a separate audit.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import { mkdir, mkdtemp, open, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url),
  { chromium } = require("playwright");
const { createServer } = await import(
  path.join(root, "frontend/node_modules/vite/dist/node/index.js")
);
const python =
  process.env.AUDIO_TEST_PYTHON || path.join(root, ".venv/bin/python");
const output = await mkdtemp(path.join(tmpdir(), "scorecraft-large-upload-")),
  data = path.join(output, "data");
await mkdir(data);
const env = {
  ...process.env,
  DATA_DIR: data,
  AUDIO_TEST_DATA_DIR: data,
  FFMPEG_BINARY:
    process.env.FFMPEG_BINARY || path.join(root, ".runtime/bin/ffmpeg"),
  BASIC_PITCH_RUNTIME: "onnx",
  MAX_UPLOAD_BYTES: "500000000",
  MAX_DURATION_SECONDS: "3600",
};
const apiUrl = "http://127.0.0.1:8001",
  appUrl = "http://127.0.0.1:5174";
const children = [],
  logs = [],
  errors = [],
  chunkRequests = [];
let assertions = 0,
  server,
  browser,
  page;
function check(value, message) {
  assertions++;
  assert.ok(value, message);
}
function equal(value, expected, message) {
  assertions++;
  assert.deepEqual(value, expected, message);
}
async function until(fn, description, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const result = await fn().catch(() => false);
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out: " + description);
}
function start(args) {
  const child = spawn(python, args, {
    cwd: root,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push(child);
  child.stdout.on("data", (x) => logs.push(x.toString()));
  child.stderr.on("data", (x) => logs.push(x.toString()));
  return child;
}
async function get(pathname) {
  const response = await fetch(apiUrl + "/api" + pathname);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function hashFile(filename) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(filename)) hash.update(chunk);
  return hash.digest("hex");
}
const rate = 22050,
  duration = 720,
  size = 44 + rate * duration * 2,
  chunkSize = 8 * 1024 * 1024;
const file = path.join(output, "DEVELOPMENT-12-minute-upload.wav");
async function fixture() {
  const writer = await open(file, "w");
  try {
    const head = Buffer.alloc(44);
    head.write("RIFF");
    head.writeUInt32LE(size - 8, 4);
    head.write("WAVEfmt ", 8);
    head.writeUInt32LE(16, 16);
    head.writeUInt16LE(1, 20);
    head.writeUInt16LE(1, 22);
    head.writeUInt32LE(rate, 24);
    head.writeUInt32LE(rate * 2, 28);
    head.writeUInt16LE(2, 32);
    head.writeUInt16LE(16, 34);
    head.write("data", 36);
    head.writeUInt32LE(rate * duration * 2, 40);
    await writer.write(head);
    const block = Buffer.alloc(rate * 2);
    for (let second = 0; second < duration; second++) {
      for (let i = 0; i < rate; i++) {
        const local = i / rate,
          amp = Math.max(0, Math.min(1, local / 0.03, (0.9 - local) / 0.05));
        const active = second % 30 < 2 || second >= duration - 2;
        block.writeInt16LE(
          active
            ? Math.round(6000 * amp * Math.sin(2 * Math.PI * 440 * local))
            : 0,
          i * 2,
        );
      }
      await writer.write(block);
    }
  } finally {
    await writer.close();
  }
}
try {
  await fixture();
  const originalHash = await hashFile(file);
  check(size > 25 * 1024 * 1024, "real WAV exceeds old25MiB limit");
  equal(duration, 720, "fixture has12minutes of full PCM");
  start([
    "-m",
    "uvicorn",
    "scripts.audio_test_server:app",
    "--host",
    "127.0.0.1",
    "--port",
    "8001",
    "--no-access-log",
  ]);
  await until(
    async () => (await fetch(apiUrl + "/api/health")).ok,
    "isolatedAPI",
  );
  const health = await get("/health");
  equal(
    health.limits.max_upload_bytes,
    500000000,
    "serveradvertisesdecimal500MB",
  );
  equal(health.limits.max_duration_seconds, 3600, "serveradvertises60minutes");
  server = await createServer({
    configFile: path.join(root, "frontend/vite.config.ts"),
    root: path.join(root, "frontend"),
    server: {
      host: "127.0.0.1",
      port: 5174,
      strictPort: true,
      proxy: { "/api": { target: apiUrl, changeOrigin: false } },
    },
  });
  await server.listen();
  browser = await chromium.launch({
    headless: true,
    channel: process.env.CHROME_CHANNEL || "chrome",
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1050 },
  });
  await context.addInitScript(() => {
    window.__bufferSizes = [];
    const original = Blob.prototype.arrayBuffer;
    Blob.prototype.arrayBuffer = function () {
      window.__bufferSizes.push(this.size);
      return original.call(this);
    };
  });
  page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  let loseSecond = true,
    holdThird = true,
    thirdSeen = false,
    release;
  const held = new Promise((resolve) => (release = resolve));
  await page.route("**/api/uploads/*/chunks/*", async (route) => {
    const offset = Number(
      new URL(route.request().url()).pathname.split("/").pop(),
    );
    chunkRequests.push(offset);
    if (offset === chunkSize && loseSecond) {
      loseSecond = false;
      await route.fetch();
      await route.abort("failed");
      return;
    }
    if (offset === 2 * chunkSize && holdThird) {
      thirdSeen = true;
      await held;
      await route.abort("aborted").catch(() => {});
      return;
    }
    await route.continue();
  });
  await page.goto(appUrl, { waitUntil: "networkidle" });
  check(
    (await page.locator(".dropzone").innerText()).includes("500 MB"),
    "limitvisiblebeforeselection",
  );
  await page.locator("input[type=file]").setInputFiles(file);
  await until(
    async () =>
      await page
        .getByRole("button", { name: "Create transcription", exact: true })
        .isEnabled(),
    "nativepreviewmetadata",
  );
  equal(
    await page.getByLabel("Clip end (seconds)", { exact: true }).inputValue(),
    "720",
    "browserpreviewretainsfull12minduration",
  );
  equal(
    await page.locator("audio").first().getAttribute("preload"),
    "metadata",
    "longpreviewdoesnoteagerlypreloadentireaudio",
  );
  await page
    .getByRole("button", { name: "Play selected clip", exact: true })
    .click();
  await until(
    async () =>
      await page
        .locator("audio")
        .first()
        .evaluate((a) => a.currentTime > 0.15),
    "nativeplayback",
  );
  await page
    .getByRole("button", { name: "Pause selected clip", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Create transcription", exact: true })
    .click();
  await until(async () => thirdSeen, "thirdchunkafterlostsecondresponse");
  const progress = page.getByRole("region", {
    name: "Audio upload progress",
    exact: true,
  });
  check(await progress.isVisible(), "actualtransferprogressvisible");
  check(
    (await progress.innerText()).includes("verified and saved"),
    "confirmedbytesdistinctfromsentbytes",
  );
  await progress
    .getByRole("button", { name: "Pause upload", exact: true })
    .click();
  release();
  await until(
    async () =>
      await progress
        .getByRole("button", { name: "Resume upload", exact: true })
        .isVisible(),
    "pausedtransfer",
  );
  const sessionId = await page.evaluate(() => {
    const key = Object.keys(sessionStorage).find((k) =>
      k.startsWith("scorecraft-upload-v1:"),
    );
    return JSON.parse(sessionStorage.getItem(key)).id;
  });
  const partial = await get("/uploads/" + sessionId);
  equal(
    partial.offset,
    2 * chunkSize,
    "lostresponseGETfindsacceptedsecondchunk",
  );
  equal(partial.chunks.length, 2, "twochunksstoredonce");
  equal(
    (await get("/projects")).length,
    0,
    "partialtransferdoesnotstartanalysis",
  );
  holdThird = false;
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("input[type=file]").setInputFiles(file);
  await until(
    async () =>
      await page
        .getByRole("button", { name: "Create transcription", exact: true })
        .isEnabled(),
    "reselectedpreview",
  );
  const begin = chunkRequests.length;
  const completedResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith("/uploads/" + sessionId + "/complete") &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create transcription", exact: true })
    .click();
  const completed = await completedResponse;
  equal(completed.status(), 201, await completed.text());
  const project = await completed.json();
  await page.waitForURL("**#/projects/" + project.id);
  equal(
    chunkRequests.slice(begin),
    [2 * chunkSize, 3 * chunkSize],
    "reloadresumessavedoffsetwithoutretransmittingprefix",
  );
  equal(project.status, "queued", "uploadcompletioncreatesdurablequeuedjob");
  equal(
    (await get("/projects")).length,
    1,
    "oneprojectafterlostresponseandresume",
  );
  const accepted = await get("/uploads/" + sessionId);
  equal(accepted.offset, size, "allsourcebytesaccepted");
  equal(
    accepted.sha256,
    originalHash,
    "serverwholefilehashmatcheslocaloriginal",
  );
  equal(
    await hashFile(path.join(data, "projects", project.id, "source")),
    originalHash,
    "preservedsourceisbyteidentical",
  );
  check(
    (await page.evaluate(() => window.__bufferSizes)).every(
      (n) => n <= 8 * 1024 * 1024,
    ),
    "browserhashingneverreadswholelargefileintoArrayBuffer",
  );
  // Reuse the same valid file to exercise server fallback without duplicating
  // the upload on later submission. Browser decode failure is deliberately simulated.
  await page.goto(appUrl, { waitUntil: "networkidle" });
  await page.locator("input[type=file]").setInputFiles(file);
  await until(
    async () =>
      await page
        .getByRole("button", { name: "Create transcription", exact: true })
        .isEnabled(),
    "previewfixtureagain",
  );
  await page
    .locator("audio")
    .first()
    .evaluate((a) => a.dispatchEvent(new Event("error")));
  await page
    .getByRole("button", { name: "Prepare playable preview", exact: true })
    .click();
  await until(
    async () =>
      await page
        .locator("audio")
        .first()
        .evaluate(
          (a) => a.currentSrc.includes("/api/uploads/") && a.readyState >= 1,
        ),
    "Streamed private full-length preview",
    90000,
  );
  equal(
    await page
      .locator("audio")
      .first()
      .evaluate((a) => a.duration),
    720,
    "serverpreviewisfull12minutes",
  );
  check(
    (await page.locator("audio").first().getAttribute("src")).startsWith(
      "/api/uploads/",
    ),
    "largepreviewusesprotectedstreamnotentiredecodedBlob",
  );
  equal((await get("/projects")).length, 1, "previewonlydoesnotqueueanalysis");
  await page
    .locator("audio")
    .first()
    .evaluate(async (a) => {
      a.currentTime = 719;
      await a.play();
    });
  await until(
    async () =>
      await page
        .locator("audio")
        .first()
        .evaluate((a) => a.currentTime > 719.1),
    "lastsecondpreviewplayback",
  );
  await page
    .locator("audio")
    .first()
    .evaluate((a) => a.pause());
  await page.getByLabel("Clip start (seconds)", { exact: true }).fill("719");
  await page.getByLabel("Clip end (seconds)", { exact: true }).fill("719.8");
  const secondId = await page.evaluate(() => {
    const key = Object.keys(sessionStorage).find((k) =>
      k.startsWith("scorecraft-upload-v1:"),
    );
    return JSON.parse(sessionStorage.getItem(key)).id;
  });
  const beforeSubmit = chunkRequests.length;
  const finalResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith("/uploads/" + secondId + "/complete") &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create transcription", exact: true })
    .click();
  const final = await finalResponse;
  equal(final.status(), 201, await final.text());
  const selected = await final.json();
  equal(selected.clip_start_seconds, 719, "previewclipupdatespersist");
  equal(selected.clip_end_seconds, 719.8, "finalsecondselectionpersists");
  equal(
    chunkRequests.length,
    beforeSubmit,
    "previewsourceisnotuploadedagainforsubmission",
  );
  equal(errors, [], "nounhandledbrowserexceptions");
  await page.screenshot({
    path: path.join(output, "large-upload-project.png"),
    fullPage: true,
  });
  const report = {
    passed: true,
    assertions,
    output,
    fixture: {
      filename: path.basename(file),
      size_bytes: size,
      duration_seconds: duration,
      sha256: originalHash,
    },
    checks: [
      "Real 12-minute WAV over 25 MiB",
      "Actual XHR transfer progress",
      "Lost chunk response after server commit",
      "Pause, reload, and resume from verified offset",
      "Per-chunk hashes and whole-file integrity",
      "Bounded browser buffer reads",
      "Streamed private full-length preview",
      "Playback near the original end",
      "Clip after preview without reupload",
      "Queued persistent project",
    ],
    limitations: [
      "This browser transport audit does not run model inference; the separate long-audio audit covers inference.",
      "Generated audio test input; no accuracy claim.",
      "Native preview failure was injected to exercise the fallback; the fallback decoded the actual full file.",
    ],
  };
  await writeFile(
    path.join(output, "report.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  if (page) {
    await page
      .screenshot({ path: path.join(output, "failure.png"), fullPage: true })
      .catch(() => {});
    await writeFile(
      path.join(output, "failure.txt"),
      await page
        .locator("body")
        .innerText()
        .catch(() => ""),
    );
  }
  console.error("Failure artifacts:", output);
  throw error;
} finally {
  await browser?.close();
  await server?.close();
  for (const child of children) child.kill("SIGTERM");
  await Promise.all(
    children.map(
      (child) =>
        new Promise((resolve) => {
          if (child.exitCode !== null) resolve();
          else {
            child.once("exit", resolve);
            setTimeout(() => {
              child.kill("SIGKILL");
              resolve();
            }, 5000).unref();
          }
        }),
    ),
  );
  await writeFile(path.join(output, "service.log"), logs.join(""));
}
