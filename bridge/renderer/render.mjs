#!/usr/bin/env node
// Headless three.js/WebGL renderer (serves over localhost http so ES modules
// and importmaps work — file:// blocks module CORS).
//   node render.mjs <scene.html under renderer/> [--frames N] [--fps F] [--size WxH] [--name out]
// Scenes go in renderer/scenes/. The scene MUST read time from the URL query
// param `t` (0..1) and render deterministically for that t (no wall-clock
// animation), e.g.:
//   const t = parseFloat(new URLSearchParams(location.search).get('t') || '0');
// Import three via importmap (scenes live in scenes/, so ../node_modules):
//   {"imports":{"three":"../node_modules/three/build/three.module.js"}}
// Still image (--frames 1) -> renders/<name>.png ; animation -> renders/<name>.mp4
import { execFile } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync, mkdirSync, mkdtempSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename, extname, resolve, normalize } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const ROOT = resolve(import.meta.dirname);
const RENDER_DIR = join(ROOT, "..", "renders");

const args = process.argv.slice(2);
const scene = args[0];
if (!scene) {
  console.error("usage: render.mjs <scene.html> [--frames N] [--fps F] [--size WxH] [--name out]");
  process.exit(2);
}
const scenePath = normalize(join(ROOT, scene));
if (!scenePath.startsWith(ROOT) || !existsSync(scenePath)) {
  console.error("scene not found or outside renderer/: " + scene);
  process.exit(2);
}
const opt = (flag, dflt) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : dflt;
};
const frames = Math.max(1, parseInt(opt("--frames", "1"), 10));
const fps = Math.max(1, parseInt(opt("--fps", "24"), 10));
const [W, H] = opt("--size", "1280x720").split("x").map(Number);
const name = opt("--name", basename(scene, extname(scene)));

const MIME = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
  ".json": "application/json", ".css": "text/css", ".png": "image/png",
  ".jpg": "image/jpeg", ".glb": "model/gltf-binary", ".wasm": "application/wasm" };

// static server rooted at renderer/ (so node_modules/three resolves)
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const p = normalize(join(ROOT, decodeURIComponent(url.pathname)));
    if (!p.startsWith(ROOT)) { res.writeHead(403); return res.end(); }
    const body = readFileSync(p);
    res.writeHead(200, { "content-type": MIME[extname(p)] || "application/octet-stream",
      "access-control-allow-origin": "*" });
    res.end(body);
  } catch { res.writeHead(404); res.end("nf"); }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;
const relUrl = scenePath.slice(ROOT.length + 1).split(/[?#]/)[0].split("\\").join("/");
const base = `http://127.0.0.1:${port}/${relUrl}`;

// MUST be async: Chrome fetches the page (and three.js) from our server, so the
// event loop has to stay responsive while Chrome runs. execFileSync would
// deadlock the server -> blank screenshot.
// Do NOT add --user-data-dir: with a fresh profile headless Chrome hangs after
// writing (never exits); without one it writes and exits cleanly.
// GPU: --disable-gpu forces SwiftShader (CPU). Omitting it lets Chrome use
// Metal via WebKit WebGL — much faster and confirmed working on this Mac.
const shot = (outPng, t) => new Promise((res, rej) => {
  execFile(CHROME, [
    "--headless=new",
    `--screenshot=${outPng}`, `--window-size=${W},${H}`,
    "--hide-scrollbars", "--virtual-time-budget=8000",
    `${base}?t=${t}`,
  ], { timeout: 60_000, maxBuffer: 20_000_000 }, (err, so, se) => {
    if (!existsSync(outPng)) return rej(new Error("chrome wrote no file (t=" + t + ")"
      + (err ? "; " + String(err.message).slice(0, 120) : "")));
    res();
  });
});

const ffmpeg = (dir, out) => new Promise((res, rej) => {
  execFile("ffmpeg", ["-y", "-framerate", String(fps), "-pattern_type", "glob",
    "-i", "frame_*.png", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", out],
    { cwd: dir, timeout: 180_000, maxBuffer: 20_000_000 },
    (err) => err ? rej(err) : res());
});

mkdirSync(RENDER_DIR, { recursive: true });
try {
  if (frames === 1) {
    const out = join(RENDER_DIR, `${name}.png`);
    await shot(out, "0");
    console.log("OK " + out);
  } else {
    const dir = mkdtempSync(join(tmpdir(), "render-"));
    for (let i = 0; i < frames; i++) {
      await shot(join(dir, `frame_${String(i).padStart(4, "0")}.png`),
                 (i / (frames - 1)).toFixed(6));
      process.stderr.write(`frame ${i + 1}/${frames}\n`);
    }
    const out = join(RENDER_DIR, `${name}.mp4`);
    await ffmpeg(dir, out);
    console.log("OK " + out);
    rmSync(dir, { recursive: true, force: true });
  }
} catch (e) {
  console.error("RENDER ERROR: " + String(e.message).slice(0, 300));
  process.exitCode = 1;
} finally {
  server.close();
}
