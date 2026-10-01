// Bundles web/src into edgelab/web/static (served by the Python backend).
// Uses a local `npm install` when present; otherwise falls back to globally installed
// packages (the offline build environment). Writes build-info.json with a hash of the
// sources so a Python test can detect a stale bundle without Node.
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(here, "../edgelab/web/static");
const require = createRequire(import.meta.url);
let globalRoot = "";
try { globalRoot = execSync("npm root -g", { encoding: "utf8" }).trim(); } catch { /* no npm */ }
const searchPaths = [path.join(here, "node_modules"), globalRoot, path.join(globalRoot, "tsx", "node_modules")].filter(Boolean);
const esbuild = require(require.resolve("esbuild", { paths: searchPaths }));

export function sourceHash() {
  const h = createHash("sha256");
  const walk = (d) => fs.readdirSync(d, { withFileTypes: true })
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))   // code-point order (matches Python sorted())
    .forEach((e) => {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else { h.update(path.relative(here, p).split(path.sep).join("/")); h.update(fs.readFileSync(p)); }
    });
  walk(path.join(here, "src"));
  for (const f of ["index.html", "build.mjs", "package.json", "tsconfig.json"]) {
    h.update(f); h.update(fs.readFileSync(path.join(here, f)));
  }
  return h.digest("hex");
}

// THE application version lives in edgelab/__init__.py; the UI is stamped with it and the build
// fails if web/package.json disagrees (so a UI, backend and updater can never ship mismatched).
export function appVersion() {
  const src = fs.readFileSync(path.resolve(here, "../edgelab/__init__.py"), "utf8");
  const m = src.match(/^__version__\s*=\s*"(\d+\.\d+\.\d+)"/m);
  if (!m) throw new Error("edgelab/__init__.py has no MAJOR.MINOR.PATCH __version__");
  const pkg = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8")).version;
  if (pkg !== m[1]) throw new Error(`web/package.json version ${pkg} != edgelab.__version__ ${m[1]}; update both`);
  return m[1];
}
const APP_VERSION = appVersion();

const options = {
  entryPoints: [path.join(here, "src/main.tsx")],
  bundle: true, minify: true, sourcemap: false, format: "esm", target: ["es2020"],
  jsx: "automatic", outfile: path.join(out, "app.js"), nodePaths: searchPaths,
  define: { "process.env.NODE_ENV": '"production"', __EDGELAB_VERSION__: JSON.stringify(APP_VERSION) }, logLevel: "info", legalComments: "none",
};

fs.mkdirSync(out, { recursive: true });
const finish = () => {
  fs.copyFileSync(path.join(here, "index.html"), path.join(out, "index.html"));
  fs.copyFileSync(path.join(here, "src/styles.css"), path.join(out, "styles.css"));
  fs.cpSync(path.join(here, "src/fonts"), path.join(out, "fonts"), { recursive: true });   // self-hosted fonts + OFL texts
  const react = require(require.resolve("react/package.json", { paths: searchPaths })).version;
  fs.writeFileSync(path.join(out, "build-info.json"), JSON.stringify({
    source_sha256: sourceHash(), built_at: new Date().toISOString(), esbuild: esbuild.version, react,
    app_version: APP_VERSION,
  }, null, 1) + "\n");
};
if (process.argv.includes("--watch")) {
  const ctx = await esbuild.context({ ...options, plugins: [{ name: "finish", setup(b) { b.onEnd(finish); } }] });
  await ctx.watch();
} else {
  await esbuild.build(options);
  finish();
}
