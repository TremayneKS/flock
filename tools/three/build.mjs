// node build.mjs: bundle entry.js into ../../web/vendor/three/three-viewer.js (ES module, minified)
import { build } from 'esbuild';
await build({ entryPoints: ['entry.js'], bundle: true, format: 'esm', minify: true, target: 'es2022',
  outfile: '../../web/vendor/three/three-viewer.js', legalComments: 'eof',
  banner: { js: '/* Flock 3D viewer: three.js 0.186.1 (MIT). Built from tools/three/entry.js. */' } });
console.log('built web/vendor/three/three-viewer.js');
