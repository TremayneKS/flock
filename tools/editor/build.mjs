// node build.mjs: bundle entry.js into ../../web/editor/cm.js (ES module, minified)
import { build } from 'esbuild';
await build({ entryPoints: ['entry.js'], bundle: true, format: 'esm', minify: true, target: 'es2022',
  outfile: '../../web/editor/cm.js', legalComments: 'eof', banner: { js: '/* Flock editor: CodeMirror 6 (MIT). Built from tools/editor/entry.js. */' } });
console.log('built web/editor/cm.js');
