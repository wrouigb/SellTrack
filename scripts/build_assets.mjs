// Builds app/static/src into app/static/dist with content-hashed filenames, and writes
// manifest.json so that Flask can find them. Run with `npm run build` or `npm run watch`.
//
// Tailwind turns the CSS source into plain CSS, then esbuild bundles and minifies that and the
// JavaScript. Hashed names mean Nginx can cache these files forever (phase 6).

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import * as esbuild from "esbuild";

const run = promisify(execFile);
const root = path.resolve(import.meta.dirname, "..");
const source = path.join(root, "app/static/src");
const output = path.join(root, "app/static/dist");
const tailwindOutput = path.join(source, ".tailwind-output.css");
const tailwind = path.join(root, "node_modules/.bin/tailwindcss");
// Copied as they are, with a hash in the name.
const staticFiles = ["logo.svg", "favicon.svg"];

async function buildCss() {
  await run(tailwind, ["--input", path.join(source, "app.css"), "--output", tailwindOutput], {
    cwd: root,
  });
}

async function bundle() {
  await rm(output, { recursive: true, force: true });
  await mkdir(output, { recursive: true });

  const result = await esbuild.build({
    entryPoints: {
      app: path.join(source, "app.js"),
      styles: tailwindOutput,
    },
    bundle: true,
    minify: true,
    format: "iife",
    target: ["es2020"],
    outdir: output,
    entryNames: "[name]-[hash]",
    assetNames: "fonts/[name]-[hash]",
    loader: { ".woff2": "file" },
    metafile: true,
    logLevel: "warning",
    // Nothing can be evaluated at runtime under the Content Security Policy.
    legalComments: "none",
  });

  const manifest = {};
  for (const [file, details] of Object.entries(result.metafile.outputs)) {
    if (details.entryPoint?.endsWith("app.js")) manifest["app.js"] = path.basename(file);
    if (details.entryPoint?.endsWith(".tailwind-output.css")) {
      manifest["app.css"] = path.basename(file);
    }
  }

  for (const name of staticFiles) {
    const contents = await readFile(path.join(source, name));
    const hash = createHash("sha256").update(contents).digest("hex").slice(0, 8).toUpperCase();
    const { name: stem, ext } = path.parse(name);
    const hashed = `${stem}-${hash}${ext}`;
    await copyFile(path.join(source, name), path.join(output, hashed));
    manifest[name] = hashed;
  }

  await writeFile(path.join(output, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  await rm(tailwindOutput, { force: true });
  console.log("Built", Object.values(manifest).join(", "));
}

async function build() {
  await buildCss();
  await bundle();
}

if (process.argv.includes("--watch")) {
  // Rebuilds everything on a change. It takes well under a second, so nothing cleverer is needed.
  const { watch } = await import("node:fs");
  let timer;
  const rebuild = () => {
    clearTimeout(timer);
    timer = setTimeout(() => build().catch((error) => console.error(error.message)), 100);
  };
  await build();
  for (const dir of [source, path.join(root, "app/templates")]) {
    watch(dir, { recursive: true }, (_event, file) => {
      if (file && !file.startsWith(".tailwind")) rebuild();
    });
  }
  console.log("Watching for changes...");
} else {
  await build();
}
