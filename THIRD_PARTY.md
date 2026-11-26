# Third-party software

## Shipped in this repository

| File | Project | License |
|---|---|---|
| `web/vendor/marked.min.js` | [marked](https://github.com/markedjs/marked) 12.0.2 | MIT, (c) Christopher Jeffrey and others |
| `web/vendor/purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) 3.1.6 | Apache-2.0 or MPL-2.0, (c) Cure53 and other contributors |
| `web/editor/cm.js` | [CodeMirror 6](https://codemirror.net) and its `@lezer` parsers, `style-mod`, `w3c-keyname`, `crelt` (versions in `tools/editor/package-lock.json`) | MIT, (c) Marijn Haverbeke and others |
| `web/vendor/three/three-viewer.js` | [three.js](https://threejs.org) 0.186.1 with its OrbitControls, GLTF/STL/OBJ/PLY loaders and RoomEnvironment, built from `tools/three` | MIT, (c) three.js authors |
| `web/vendor/3dmol/3Dmol-min.js` | [3Dmol.js](https://3dmol.org) 2.5.5 | BSD-3-Clause, (c) University of Pittsburgh and contributors |
| `web/vendor/mammoth/mammoth.browser.min.js` | [mammoth](https://github.com/mwilliamson/mammoth.js) 1.13.0 | BSD-2-Clause, (c) Michael Williamson |
| `web/vendor/sheetjs/xlsx.full.min.js` | [SheetJS Community Edition](https://sheetjs.com) 0.20.3 | Apache-2.0, (c) SheetJS LLC |
| `web/vendor/utif/UTIF.js` | [UTIF.js](https://github.com/photopea/UTIF.js) 3.1.0 | MIT, (c) Photopea |
| `web/vendor/pdfjs/pdf.min.js`, `pdf.worker.min.js` | [PDF.js](https://mozilla.github.io/pdf.js/) | Apache-2.0, Mozilla Foundation |
| `web/vendor/pdf-lib/pdf-lib.min.js` | [pdf-lib](https://pdf-lib.js.org/) | MIT, (c) Andrew Dillon |
| `web/fonts/inter.woff2` | [Inter](https://github.com/rsms/inter) | SIL Open Font License 1.1, see `web/fonts/inter-LICENSE.txt` |

`meetings/web/vendor/` and `meetings/web/fonts/` hold the same marked, DOMPurify and Inter files for Meetings.

The license headers inside the minified files are kept as published.

## Installed by `install.py` (not in this repository)

| What | License |
|---|---|
| [@playwright/mcp](https://github.com/microsoft/playwright-mcp) and [Playwright](https://playwright.dev) (Node) | Apache-2.0 |
| Python packages in `requirements-search.txt`: ddgs (MIT), httpx (BSD-3-Clause), mcp (MIT), trafilatura (Apache-2.0), playwright (Apache-2.0), pypdf (BSD-3-Clause), and their dependencies | as stated |
| With `--with-dictation`: faster-whisper (MIT), CTranslate2 (MIT), NVIDIA CUDA libraries (NVIDIA's license, only when an NVIDIA GPU is present); Whisper model weights from Hugging Face (MIT) | as stated |
| Playwright's Chromium, only when neither Chrome nor Edge is installed | Chromium's licenses |
| With `--with-meetings`: the speech engine [OpenWhisper](https://github.com/Knuckles92/OpenWhisper) 2.8.3 (MIT), and its Python packages at its own pinned versions (faster-whisper, CTranslate2, onnxruntime, PyAV, sounddevice, SoundCard, PyQt6, FastAPI, SQLAlchemy and others: MIT, BSD, Apache-2.0, LGPL/GPL for PyQt6 as stated by each); NVIDIA CUDA libraries only with an NVIDIA GPU. Downloaded on first use: Whisper weights from Hugging Face (MIT) and the WeSpeaker ResNet34 voice model (Apache-2.0) | as stated | <!-- publish-scan: ok -->

Models and services you connect (Claude, ChatGPT, DeepSeek, Gemini and so on) are used under your own agreement with
their providers.
