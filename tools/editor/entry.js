// Flock editor: CodeMirror 6, bundled by build.mjs into web/editor/cm.js (loaded on first use by files.js).
// Colours come from CSS variables (--hl-*) in style.css, so the editor follows the light and dark themes.
import { Compartment, EditorState, StateEffect, StateField } from '@codemirror/state';
import { Decoration, EditorView, crosshairCursor, drawSelection, dropCursor, highlightActiveLine, highlightActiveLineGutter,
  highlightSpecialChars, keymap, lineNumbers, rectangularSelection } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { HighlightStyle, StreamLanguage, bracketMatching, foldGutter, foldKeymap, indentOnInput, indentUnit, syntaxHighlighting } from '@codemirror/language';
import { highlightSelectionMatches, searchKeymap } from '@codemirror/search';
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { javascript } from '@codemirror/lang-javascript';
import { json } from '@codemirror/lang-json';
import { markdown } from '@codemirror/lang-markdown';
import { python } from '@codemirror/lang-python';
import { yaml } from '@codemirror/lang-yaml';
import { shell } from '@codemirror/legacy-modes/mode/shell';
import { toml } from '@codemirror/legacy-modes/mode/toml';
import { tags as t } from '@lezer/highlight';

const hl = HighlightStyle.define([
  { tag: [t.keyword, t.controlKeyword, t.operatorKeyword, t.definitionKeyword, t.moduleKeyword, t.modifier], color: 'var(--hl-kw)' },
  { tag: [t.string, t.special(t.string), t.regexp, t.escape, t.character], color: 'var(--hl-str)' },
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: 'var(--hl-com)', fontStyle: 'italic' },
  { tag: [t.number, t.integer, t.float, t.bool, t.null, t.atom, t.unit], color: 'var(--hl-num)' },
  { tag: [t.function(t.variableName), t.function(t.propertyName), t.function(t.definition(t.variableName))], color: 'var(--hl-fn)' },
  { tag: [t.definition(t.className), t.className, t.typeName, t.namespace], color: 'var(--hl-type)' },
  { tag: [t.tagName, t.angleBracket], color: 'var(--hl-tag)' },
  { tag: [t.attributeName, t.propertyName], color: 'var(--hl-attr)' },
  { tag: [t.heading, t.strong], color: 'var(--hl-kw)', fontWeight: '600' },
  { tag: [t.emphasis], fontStyle: 'italic' },
  { tag: [t.link, t.url], color: 'var(--hl-str)', textDecoration: 'underline' },
  { tag: [t.meta, t.annotation, t.processingInstruction], color: 'var(--hl-com)' },
  { tag: t.invalid, color: 'var(--red)' },
]);

const theme = EditorView.theme({
  '&': { height: '100%', backgroundColor: 'var(--bg)', color: 'var(--ink)' },
  '.cm-content': { caretColor: 'var(--accent)', padding: '8px 0' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)', borderLeftWidth: '2px' },
  '.cm-gutters': { backgroundColor: 'var(--bg)', color: 'var(--faint)', border: 'none', paddingRight: '4px' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--ink-2)' },
  '.cm-activeLine': { backgroundColor: 'var(--surface)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection': { backgroundColor: 'var(--accent-mid) !important' },
  '.cm-selectionMatch': { backgroundColor: 'var(--accent-soft)' },
  '.cm-matchingBracket': { backgroundColor: 'var(--accent-soft)', outline: '1px solid var(--accent-line)' },
  '.cm-foldGutter span': { color: 'var(--faint)' },
  '.cm-tooltip': { backgroundColor: 'var(--pop)', border: '1px solid var(--line-2)', borderRadius: '6px' },
  '.cm-tooltip-autocomplete ul li[aria-selected]': { backgroundColor: 'var(--hover)', color: 'var(--ink)' },
  '.cm-panels': { backgroundColor: 'var(--surface)', color: 'var(--ink)', borderColor: 'var(--line)' },
  '.cm-searchMatch': { backgroundColor: 'var(--accent-soft)', outline: '1px solid var(--accent-line)' },
  '.cm-changed-line': { backgroundColor: 'var(--add)' },
});

const LANGS = {
  html: () => html(), css: () => css(), javascript: () => javascript(), jsx: () => javascript({ jsx: true }),
  typescript: () => javascript({ typescript: true }), tsx: () => javascript({ jsx: true, typescript: true }),
  json: () => json(), markdown: () => markdown(), python: () => python(), yaml: () => yaml(),
  shell: () => StreamLanguage.define(shell), toml: () => StreamLanguage.define(toml),
};
const langFor = (name) => (LANGS[name] ? LANGS[name]() : []);

// lines the agent just changed are tinted until the next edit
const setMarks = StateEffect.define();
const marks = StateField.define({
  create: () => Decoration.none,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setMarks)) return e.value;
    return tr.docChanged ? Decoration.none : value;
  },
  provide: (f) => EditorView.decorations.from(f),
});
const changedLine = Decoration.line({ class: 'cm-changed-line' });

function smallestChange(doc, text) {
  const old = doc.toString(), max = Math.min(old.length, text.length);
  let a = 0, b = 0;
  while (a < max && old.charCodeAt(a) === text.charCodeAt(a)) a++;
  while (b < max - a && old.charCodeAt(old.length - 1 - b) === text.charCodeAt(text.length - 1 - b)) b++;
  return { from: a, to: old.length - b, insert: text.slice(a, text.length - b) };
}

export function createEditor(parent, { doc = '', language = 'text', state = null, onChange = () => {}, onSave = () => {} } = {}) {
  const lang = new Compartment();
  let quiet = false;
  const extensions = [
    lineNumbers(), foldGutter(), highlightActiveLineGutter(), highlightSpecialChars(), history(), drawSelection(), dropCursor(),
    EditorState.allowMultipleSelections.of(true), indentOnInput(), indentUnit.of('  '), syntaxHighlighting(hl), bracketMatching(),
    closeBrackets(), autocompletion(), rectangularSelection(), crosshairCursor(), highlightActiveLine(), highlightSelectionMatches(),
    keymap.of([{ key: 'Mod-s', preventDefault: true, run: () => { onSave(); return true; } },
      ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, ...foldKeymap, ...completionKeymap, indentWithTab]),
    lang.of(langFor(language)), theme, marks, EditorView.lineWrapping,
    EditorView.updateListener.of((u) => { if (u.docChanged && !quiet) onChange(u.state.doc.toString()); }),
  ];
  const st = state && state.doc && state.doc.toString() === doc ? state : EditorState.create({ doc, extensions });
  const view = new EditorView({ state: st, parent });
  return {
    view,
    getValue: () => view.state.doc.toString(),
    getState: () => view.state,
    focus: () => view.focus(),
    destroy: () => view.destroy(),
    setLanguage: (name) => view.dispatch({ effects: lang.reconfigure(langFor(name)) }),
    // the file changed on disk (the agent edited it): apply the smallest change and tint the lines it touched
    setValue(text, highlight) {
      const ch = smallestChange(view.state.doc, text);
      quiet = true;
      view.dispatch({ changes: ch });
      quiet = false;
      if (highlight && ch.insert.length) {
        const doc2 = view.state.doc, from = doc2.lineAt(ch.from).number, to = doc2.lineAt(Math.min(doc2.length, ch.from + ch.insert.length)).number;
        const ranges = [];
        for (let n = from; n <= to && n - from < 400; n++) ranges.push(changedLine.range(doc2.line(n).from));
        view.dispatch({ effects: setMarks.of(Decoration.set(ranges)), scrollIntoView: true, selection: { anchor: Math.min(doc2.length, ch.from) } });
      }
    },
  };
}
