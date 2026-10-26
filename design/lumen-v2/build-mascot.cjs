// Bundle the existing Lumen renderer and clips; this prototype does not invent a second animation system.
const fs = require('node:fs/promises');
const path = require('node:path');
const { createRequire } = require('node:module');
const fromTests = createRequire(require.resolve('../../src/web/node_modules/vitest/package.json'));
const fromVite = createRequire(fromTests.resolve('vite/package.json'));
const esbuild = fromVite('esbuild');
async function build() {
  const project = path.resolve(__dirname, '../..');
  const output = path.join(__dirname, 'assets/mascot');
  await fs.mkdir(output, { recursive: true });
  for (const file of ['fox-idle.alpha.mp4', 'fox-react.alpha.mp4', 'fox-poster.png']) {
    await fs.copyFile(path.join(project, 'src/web/public/mascot', file), path.join(output, file));
  }
  await fs.copyFile(path.join(project, 'src/web/public/lumen-mark.svg'), path.join(__dirname, 'assets/lumen-mark.svg'));
  await fs.copyFile(path.join(project, 'src/web/node_modules/react/LICENSE'), path.join(__dirname, 'assets/React-LICENSE.txt'));
  await esbuild.build({
    absWorkingDir: path.join(project, 'src/web'),
    entryPoints: [path.join(__dirname, 'mascot-entry.tsx')],
    outfile: path.join(__dirname, 'assets/mascot-player.js'),
    bundle: true, minify: true, format: 'iife', target: 'es2022', jsx: 'automatic',
    nodePaths: [path.join(project, 'src/web/node_modules')],
    define: { 'process.env.NODE_ENV': '"production"' },
    plugins: [{ name: 'prototype-asset-location', setup(build) {
      build.onLoad({ filter: /video-mascot-renderer\.tsx$/ }, async args => ({
        contents: (await fs.readFile(args.path, 'utf8')).replaceAll('/mascot/', 'assets/mascot/'),
        loader: 'tsx', resolveDir: path.dirname(args.path),
      }));
    } }],
  });
  console.log('Bundled existing VideoMascotRenderer, motion/visibility hooks, and unchanged authored clips.');
}
build().catch(error => { console.error(error); process.exitCode = 1; });
