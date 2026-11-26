"""Everything on disk: projects, meetings, project files, search and file previews. No agents here.

Layout (the data folder is settings.ROOT: ~/Meetings (or an existing ~/KC Meetings) unless settings.json or FLOCK_MEETINGS_ROOT / KC_MEETINGS_ROOT says otherwise):
  <data folder>\\<Project>\\
    Files\\                      the user's project documents, shown in the file selector
    Files\\Recordings\\           recordings of meetings the user imported (the meeting folder still has only its three files)
    State\\status.md             deadlines, upcoming meetings, problems, key dates, people
    State\\changelog.md          dated log of every change to the project
    Meetings\\<date time title>\\  exactly three files: transcript.md, live-summary.md, summary.md
    .copilot\\                   app bookkeeping (chat, session ids, imports\\<id>\\ work for each import), never shown in the app
"""
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time

import settings

ROOT = settings.ROOT
APP_DIR = os.path.join(ROOT, '.app')
CHAT_ROLES = {'user', 'assistant', 'system', 'nudge'}
MEETING_FILES = ('summary.md', 'live-summary.md', 'transcript.md')      # the only files a meeting ever has
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
TEXT_EXT = {'.md', '.markdown', '.txt', '.log', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.py', '.js', '.ts',
            '.tsx', '.jsx', '.css', '.sh', '.ps1', '.bat', '.cmd', '.sql', '.r', '.rmd', '.tex', '.bib', '.xml', '.fasta',
            '.fa', '.vcf', '.c', '.cpp', '.h', '.java', '.go', '.rs', '.csv', '.tsv', '.html', '.htm', '.svg'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.avif'}
MEDIA_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.opus', '.aac', '.mp4', '.m4v', '.webm', '.mov', '.mkv'}
VIDEO_EXT = {'.mp4', '.m4v', '.webm', '.mov', '.mkv'}
RECORDING_EXT = MEDIA_EXT | {'.avi', '.wmv', '.wma', '.3gp'}      # what can be imported as a meeting
MAX_INLINE = 3_000_000                  # bigger text files are cut in the preview
MAX_ROWS = 2000
_cache = {}                             # path -> (mtime, text), so search stays instant
_cache_lock = threading.Lock()


def clean_name(name, limit=70):
    """A name that is safe as a Windows folder or file name, and still readable in Explorer."""
    name = re.sub(r'\s+', ' ', BAD_CHARS.sub(' ', name or '')).strip().rstrip('. ')
    if len(name) > limit:
        head = name[:limit + 1]
        cut = head.rsplit(' ', 1)[0] if ' ' in head else ''     # end on a whole word, not half of one
        name = cut if len(cut) > limit // 2 else name[:limit]
    name = name.strip().rstrip('.,;- ')
    return '' if name.lower() in RESERVED else name


def unique_path(folder, name):
    """A path in folder for a file called name, adding (2), (3)... rather than overwriting anything."""
    name = clean_name(name, 120) or 'file'
    base, ext = os.path.splitext(name)
    os.makedirs(folder, exist_ok=True)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path, n = os.path.join(folder, f'{base} ({n}){ext}'), n + 1
    return path


def atomic_write(path, text):
    tmp = f'{path}.{threading.get_ident()}.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path, limit=None):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ''


def now_stamp():
    return time.strftime('%Y-%m-%d %H:%M')


class Project:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(ROOT, name)
        self.files_dir = os.path.join(self.dir, 'Files')
        self.state_dir = os.path.join(self.dir, 'State')
        self.meetings_dir = os.path.join(self.dir, 'Meetings')
        self.app_dir = os.path.join(self.dir, '.copilot')
        self.meta_path = os.path.join(self.app_dir, 'project.json')
        self.chat_path = os.path.join(self.app_dir, 'chat.jsonl')
        self.status_path = os.path.join(self.state_dir, 'status.md')
        self.changelog_path = os.path.join(self.state_dir, 'changelog.md')
        self.lock = threading.RLock()

    # ---- metadata ----
    def meta(self):
        with self.lock:
            try:
                with open(self.meta_path, encoding='utf-8') as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}

    def update_meta(self, **changes):
        with self.lock:
            meta = self.meta()
            meta.update(changes)
            atomic_write(self.meta_path, json.dumps(meta, indent=1, ensure_ascii=False))
            return meta

    def meeting_meta(self, folder):
        return (self.meta().get('meetings') or {}).get(folder, {})

    def update_meeting(self, folder, **changes):
        with self.lock:
            meta = self.meta()
            meetings = meta.setdefault('meetings', {})
            meetings.setdefault(folder, {}).update(changes)
            atomic_write(self.meta_path, json.dumps(meta, indent=1, ensure_ascii=False))

    def rename_meeting_key(self, old, new):
        with self.lock:
            meta = self.meta()
            meetings = meta.setdefault('meetings', {})
            meetings[new] = meetings.pop(old, {})
            atomic_write(self.meta_path, json.dumps(meta, indent=1, ensure_ascii=False))

    # ---- project state ----
    def read_status(self):
        return read_text(self.status_path)

    def write_status(self, text):
        atomic_write(self.status_path, text.rstrip() + '\n')

    def log_change(self, text):
        """One dated line in State/changelog.md, newest last."""
        text = re.sub(r'\s+', ' ', str(text)).strip()
        if text:
            with self.lock:
                with open(self.changelog_path, 'a', encoding='utf-8', newline='\n') as f:
                    f.write(f'- {now_stamp()}: {text}\n')

    # ---- meetings ----
    def new_meeting_folder(self, title, stamp=None):
        """stamp is 'YYYY-MM-DD HHMM', for a meeting that happened earlier (an imported recording); default now."""
        stamp = stamp or time.strftime('%Y-%m-%d %H%M')
        base = f'{stamp} {clean_name(title, 50) or "Meeting"}'
        folder, n = base, 2
        while os.path.exists(os.path.join(self.meetings_dir, folder)):
            folder, n = f'{base} {n}', n + 1
        os.makedirs(os.path.join(self.meetings_dir, folder))
        return folder

    def meeting_dir(self, folder):
        return os.path.join(self.meetings_dir, folder)

    def meetings(self):
        """Newest first. The folders are the truth; project.json only adds details."""
        known = self.meta().get('meetings') or {}
        rows = []
        try:
            folders = [e.name for e in os.scandir(self.meetings_dir) if e.is_dir()]
        except OSError:
            folders = []
        for folder in folders:
            info = known.get(folder, {})
            match = re.match(r'(\d{4})-(\d\d)-(\d\d) (\d\d)(\d\d) ?(.*)', folder)
            when = f'{match[1]}-{match[2]}-{match[3]} {match[4]}:{match[5]}' if match else ''
            base = self.meeting_dir(folder)
            rows.append({
                'id': folder,
                'title': info.get('title') or (match[6] if match else folder) or 'Meeting',
                'when': info.get('when') or when,
                'duration_s': info.get('duration_s') or 0,
                'people': info.get('people') or [],
                'files': [f for f in MEETING_FILES if os.path.exists(os.path.join(base, f))],
                'summary_status': info.get('summary_status') or '',
                'recording': info.get('recording') or '',
                'import': info.get('import') if (info.get('import') or {}).get('status') not in (None, 'done') else None,
            })
        rows.sort(key=lambda r: r['when'] or r['id'], reverse=True)
        return rows

    # ---- files ----
    def safe_path(self, rel):
        """Resolve a project-relative path, refusing anything outside the project or inside .copilot."""
        rel = (rel or '').replace('\\', '/').lstrip('/')
        full = os.path.realpath(os.path.join(self.dir, rel))
        base = os.path.realpath(self.dir)
        inner = os.path.relpath(full, base).replace('\\', '/') if full.startswith(base) else '..'
        if inner.startswith('..') or inner.split('/')[0] == '.copilot':
            raise ValueError('That file is outside this project.')
        return full

    def rel(self, full):
        return os.path.relpath(full, self.dir).replace('\\', '/')

    def project_files(self):
        out = []
        for folder, dirs, names in os.walk(self.files_dir):
            dirs.sort(key=str.lower)
            for name in sorted(names, key=str.lower):
                if name.endswith(('.tmp', '.part')) or name.startswith('~$'):
                    continue
                full = os.path.join(folder, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                out.append({'path': self.rel(full), 'name': name, 'size': st.st_size, 'mtime': int(st.st_mtime),
                            'folder': os.path.relpath(folder, self.files_dir).replace('\\', '/').replace('.', '', 1)})
        return out

    def file_tree(self):
        return {
            'state': [{'path': 'State/status.md', 'name': 'Project state'},
                      {'path': 'State/changelog.md', 'name': 'Changelog'}],
            'files': self.project_files(),
            'meetings': [{'id': m['id'], 'title': m['title'], 'when': m['when'],
                          'files': [{'path': f'Meetings/{m["id"]}/{f}', 'name': NICE_NAMES[f]} for f in m['files']]}
                         for m in self.meetings()],
        }

    def add_file(self, name, data):
        path = unique_path(self.files_dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return self.rel(path)

    @property
    def recordings_dir(self):
        return os.path.join(self.files_dir, 'Recordings')

    def signature(self):
        """Changes when files appear or change, so the app notices files dropped in with Explorer."""
        return hash(tuple((f['path'], f['mtime'], f['size']) for f in self.project_files()))

    # ---- chat ----
    def append_chat(self, entry):
        with self.lock:
            with open(self.chat_path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def read_chat(self):
        rows = []
        for line in read_text(self.chat_path).splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('role') not in CHAT_ROLES:   # older chats named the user's own messages after them
                row['role'] = 'user'
            rows.append(row)
        return rows


NICE_NAMES = {'summary.md': 'Summary', 'live-summary.md': 'Live notes', 'transcript.md': 'Transcript'}


def list_projects():
    try:
        rows = [e for e in os.scandir(ROOT) if e.is_dir() and not e.name.startswith('.')]
    except OSError:
        return []
    out = []
    for e in rows:
        project = Project(e.name)
        if os.path.isdir(project.app_dir):
            meta = project.meta()
            out.append({'name': e.name, 'description': meta.get('description', ''), 'created': meta.get('created', ''),
                        'used': meta.get('used', '') or meta.get('created', ''),
                        'meetings': len(meta.get('meetings') or {})})
    out.sort(key=lambda r: r['used'], reverse=True)
    return out


STATUS_TEMPLATE = """# {name}

{description}

## Deadlines
None yet.

## Upcoming meetings
None yet.

## Problems
None yet.

## Key dates
- {today}: Project created.

## People
None yet.
"""


def create_project(name, description=''):
    name = clean_name(name, 60)
    if not name or name.startswith('.'):
        raise ValueError('Give the project a name.')
    project = Project(name)
    if os.path.isdir(project.app_dir):
        raise ValueError('A project with that name already exists.')
    for folder in (project.files_dir, project.state_dir, project.meetings_dir, project.app_dir):
        os.makedirs(folder, exist_ok=True)
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(project.app_dir, 2)       # hidden in Explorer
        except OSError:
            pass
    today = time.strftime('%Y-%m-%d')
    project.update_meta(name=name, description=description.strip(), created=now_stamp(), used=now_stamp(), engine='opus', meetings={})
    project.write_status(STATUS_TEMPLATE.format(name=name, description=description.strip() or 'No description yet.', today=today))
    atomic_write(project.changelog_path, f'# Changelog\n\n- {now_stamp()}: Project created.\n')
    return project


# ---- turning files into text, for search and for the viewer ----

def docx_to_markdown(path):
    import docx
    document = docx.Document(path)
    parts = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            para = docx.text.paragraph.Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or '').lower() if para.style is not None else ''
            if style == 'title':
                parts.append('# ' + text)
            elif style.startswith('heading'):
                level = int(re.sub(r'\D', '', style) or '1')
                parts.append('#' * min(level + 1, 4) + ' ' + text)
            elif 'list' in style:
                parts.append('- ' + text)
            else:
                parts.append(text)
        elif tag == 'tbl':
            table = docx.table.Table(child, document)
            rows = [[cell.text.strip().replace('\n', ' ').replace('|', '/') for cell in row.cells] for row in table.rows]
            if rows:
                table_lines = ['| ' + ' | '.join(rows[0]) + ' |', '|' + ' --- |' * len(rows[0])]
                table_lines.extend('| ' + ' | '.join(r) + ' |' for r in rows[1:])
                parts.append('\n'.join(table_lines))
    return '\n\n'.join(parts)


def pptx_to_markdown(path):
    import pptx
    parts = []
    for i, slide in enumerate(pptx.Presentation(path).slides, 1):
        parts.append(f'## Slide {i}')
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
    return '\n\n'.join(parts)


def xlsx_sheets(path):
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = []
    for sheet in book.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = ['' if v is None else str(v) for v in row]
            if any(cells):
                rows.append(cells)
            if len(rows) >= MAX_ROWS:
                break
        sheets.append({'name': sheet.title, 'rows': rows})
    book.close()
    return sheets


def csv_sheet(path, name):
    text = read_text(path, MAX_INLINE)
    delimiter = '\t' if path.lower().endswith('.tsv') else ','
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return [{'name': name, 'rows': rows}]


def pdf_text(path):
    import pypdf
    reader = pypdf.PdfReader(path)
    return '\n'.join((page.extract_text() or '') for page in reader.pages[:300])


def searchable_text(path):
    """Plain text of a file for search; '' when it has none. Cached until the file changes."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ''
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.docx':
            text = docx_to_markdown(path)
        elif ext == '.pptx':
            text = pptx_to_markdown(path)
        elif ext == '.xlsx':
            text = '\n'.join(' '.join(row) for sheet in xlsx_sheets(path) for row in sheet['rows'])
        elif ext == '.pdf':
            text = pdf_text(path)
        elif ext in TEXT_EXT:
            text = read_text(path, MAX_INLINE)
        else:
            text = ''
    except Exception:
        text = ''
    with _cache_lock:
        _cache[path] = (mtime, text)
    return text


def readable_text(path):
    """A document as text for the helper to read, keeping spreadsheet sheets and columns apart."""
    if path.lower().endswith('.xlsx'):
        return '\n\n'.join('## ' + sheet['name'] + '\n' + '\n'.join(' | '.join(row) for row in sheet['rows']) for sheet in xlsx_sheets(path))
    return searchable_text(path)


def _snippet(line, terms, width=170):
    line = line.replace('**', '')       # highlight marks in the notes, which mean nothing in a search result
    low = line.lower()
    at = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, at - 50)
    piece = line[start:start + width].strip()
    return ('... ' if start else '') + piece + (' ...' if start + width < len(line) else '')


def search(project, query, limit=40):
    """Every term must appear in the file or its title. Meetings and state rank first, newest meetings first."""
    terms = [t.lower() for t in re.findall(r'\S+', query or '')]
    if not terms:
        return []
    candidates = []
    for meeting in project.meetings():
        for name in meeting['files']:
            candidates.append(('meeting', f'Meetings/{meeting["id"]}/{name}',
                               f'{meeting["title"]}: {NICE_NAMES[name]}', meeting['when'], meeting['id']))
    candidates.append(('state', 'State/status.md', 'Project state', '', ''))
    candidates.append(('state', 'State/changelog.md', 'Changelog', '', ''))
    for item in project.project_files():
        candidates.append(('file', item['path'], item['name'], '', ''))
    results = []
    for group, rel, title, when, meeting_id in candidates:
        full = os.path.join(project.dir, rel)
        text = searchable_text(full)
        low = text.lower()
        head = f'{title} {when}'.lower()
        if not all(t in low or t in head for t in terms):
            continue
        hits = []
        count = 0
        for number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()
            if any(t in lowered for t in terms):
                count += 1
                if len(hits) < 3:
                    hits.append({'line': number, 'text': _snippet(line, terms)})
        results.append({'group': group, 'path': rel, 'title': title, 'when': when, 'meeting': meeting_id,
                        'count': count, 'snippets': hits,
                        'title_hit': all(t in head for t in terms)})
    rank = {'meeting': 0, 'state': 1, 'file': 2}
    results.sort(key=lambda r: r['when'], reverse=True)
    results.sort(key=lambda r: (not r['title_hit'], rank[r['group']]))
    return results[:limit]


def view_file(project, rel):
    """What the viewer panel needs to show a file, without leaving the app."""
    full = project.safe_path(rel)
    if not os.path.isfile(full):
        raise ValueError('That file no longer exists.')
    name = os.path.basename(full)
    ext = os.path.splitext(name)[1].lower()
    base = {'name': name, 'path': project.rel(full), 'size': os.path.getsize(full)}
    try:
        if ext in ('.md', '.markdown'):
            return {**base, 'kind': 'markdown', 'text': read_text(full, MAX_INLINE)}
        if ext in IMAGE_EXT:
            return {**base, 'kind': 'image'}
        if ext in MEDIA_EXT:
            return {**base, 'kind': 'video' if ext in VIDEO_EXT else 'audio'}
        if ext == '.pdf':
            return {**base, 'kind': 'pdf'}
        if ext in ('.html', '.htm', '.svg'):
            return {**base, 'kind': 'frame'}
        if ext in ('.csv', '.tsv'):
            return {**base, 'kind': 'table', 'sheets': csv_sheet(full, name)}
        if ext == '.xlsx':
            return {**base, 'kind': 'table', 'sheets': xlsx_sheets(full)}
        if ext == '.docx':
            return {**base, 'kind': 'markdown', 'text': docx_to_markdown(full)}
        if ext == '.pptx':
            return {**base, 'kind': 'markdown', 'text': pptx_to_markdown(full)}
    except Exception as exc:
        return {**base, 'kind': 'other', 'note': f'Could not read this file: {type(exc).__name__}'}
    with open(full, 'rb') as f:
        sample = f.read(4096)
    if ext in TEXT_EXT or b'\x00' not in sample:
        text = read_text(full, MAX_INLINE)
        return {**base, 'kind': 'text', 'text': text, 'cut': os.path.getsize(full) > MAX_INLINE}
    return {**base, 'kind': 'other', 'note': 'This type of file cannot be previewed here.'}


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT and ext not in ('.html', '.htm', '.svg'):
        return 'text/plain; charset=utf-8'
    return mimetypes.guess_type(path)[0] or 'application/octet-stream'


def ensure_root():
    os.makedirs(APP_DIR, exist_ok=True)


def remove_project_tree_guard(path):
    """Never delete anything outside the data root."""
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(ROOT) + os.sep):
        raise ValueError('Refusing to touch a folder outside the data root.')
    shutil.rmtree(real, ignore_errors=True)


if __name__ == '__main__':
    # How the helper reads a document: python store.py text "Files/name.docx"
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != 'text':
        sys.exit('usage: store.py text <file>')
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(readable_text(sys.argv[2]) or '(no text found in this file)')
