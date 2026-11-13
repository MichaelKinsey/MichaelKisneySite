"""Local visual editor for the michaelkinsey.xyz site.

Run "Edit Site.bat" (or: python _editor/server.py) from the site folder.
It serves the site on http://127.0.0.1:8787 with a small editing API and
opens the editor in your browser. Nothing here is deployed: _editor is
listed in .assetsignore.
"""
import datetime
import html.parser
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKUPS = os.path.join(HERE, 'backups')
SKIP_DIRS = {'.git', '_editor', 'assets', 'node_modules'}

mimetypes.add_type('image/webp', '.webp')
mimetypes.add_type('video/mp4', '.mp4')
mimetypes.add_type('video/webm', '.webm')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('application/json', '.jsonc')

DRAFTS = {}          # page path -> html text (with data-ed ids)
DRAFT_BASE = {}      # page path -> disk mtime when the draft started
LOCK = threading.Lock()


# ---------- helpers ----------
def safe_path(rel):
    """Resolve a site-relative path inside ROOT, or None."""
    rel = (rel or '').replace('\\', '/').lstrip('/')
    full = os.path.normpath(os.path.join(ROOT, rel))
    if full != ROOT and not full.startswith(ROOT + os.sep):
        return None
    return full


def rel_of(full):
    return os.path.relpath(full, ROOT).replace('\\', '/')


def list_pages():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.'))
        for f in sorted(filenames):
            if f.endswith('.html'):
                rel = rel_of(os.path.join(dirpath, f))
                url = '/' + rel
                if url.endswith('/index.html'):
                    url = url[:-len('index.html')]
                elif url == '/index.html':
                    url = '/'
                out.append({'path': rel, 'url': '/' if rel == 'index.html' else url})
    order = lambda p: (p['path'] != 'index.html', p['path'].count('/'), p['path'])
    return sorted(out, key=order)


class _Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.found = []

    def handle_starttag(self, tag, attrs):
        self.found.append((self.getpos(), self.get_starttag_text()))

    handle_startendtag = handle_starttag


def inject_ids(text):
    """Give every element after <body> a data-ed="N" attribute (document order)."""
    if 'data-ed="' in text:
        return text
    m = re.search(r'<body[^>]*>', text, re.I)
    start = m.end() if m else 0
    body = text[start:]
    p = _Tags()
    p.feed(body)
    p.close()
    line_starts = [0]
    for i, ch in enumerate(body):
        if ch == '\n':
            line_starts.append(i + 1)
    inserts = []
    for n, ((line, col), raw) in enumerate(p.found, 1):
        if not raw:
            continue
        mm = re.match(r'<([^\s/>]+)', raw)
        if not mm:
            continue
        off = line_starts[line - 1] + col + len(mm.group(0))
        inserts.append((off, f' data-ed="{n}"'))
    for off, s in reversed(inserts):
        body = body[:off] + s + body[off:]
    return text[:start] + body


def strip_ids(text):
    return re.sub(r' data-ed="\d+"', '', text)


def disk_mtime(rel):
    try:
        return os.path.getmtime(safe_path(rel))
    except OSError:
        return 0


def page_text(rel):
    with LOCK:
        if rel in DRAFTS:
            return DRAFTS[rel], True
    full = safe_path(rel)
    with open(full, 'r', encoding='utf-8') as fh:
        return inject_ids(fh.read()), False


def draft_is_stale(rel):
    """True when the file on disk changed after this draft was started (e.g. Claude or another tool edited it)."""
    with LOCK:
        base = DRAFT_BASE.get(rel)
    return base is not None and disk_mtime(rel) > base + 0.5


def backup(full):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    dst = os.path.join(BACKUPS, stamp, rel_of(full))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(full, dst)


def unique_name(full):
    if not os.path.exists(full):
        return full
    base, ext = os.path.splitext(full)
    i = 2
    while os.path.exists(f'{base}-{i}{ext}'):
        i += 1
    return f'{base}-{i}{ext}'


# ---------- request handler ----------
class Handler(BaseHTTPRequestHandler):
    server_version = 'MKSiteEditor/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), 'application/json; charset=utf-8')

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    # --- GET ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = urllib.parse.unquote(u.path)
        if path in ('/__ed', '/__ed/'):
            with open(os.path.join(HERE, 'editor.html'), 'rb') as fh:
                return self._send(200, fh.read(), 'text/html; charset=utf-8')
        if path == '/__ed/api/pages':
            return self._json(list_pages())
        if path == '/__ed/api/page':
            rel = (q.get('path') or [''])[0]
            full = safe_path(rel)
            if not full or not os.path.isfile(full):
                return self._json({'error': 'not found'}, 404)
            text, draft = page_text(rel)
            return self._json({'html': text, 'draft': draft, 'stale': draft and draft_is_stale(rel)})
        if path == '/__ed/api/whoami':
            return self._json({'root': ROOT})
        if path == '/__ed/api/drafts':
            with LOCK:
                return self._json(sorted(DRAFTS))
        return self._static(path)

    def _static(self, path):
        full = safe_path(path)
        if full and os.path.isdir(full):
            if not path.endswith('/'):
                return self._send(301, b'', extra={'Location': path + '/'})
            full = os.path.join(full, 'index.html')
        if not full or not os.path.isfile(full):
            nf = os.path.join(ROOT, '404.html')
            if os.path.isfile(nf):
                text, _ = page_text('404.html')
                return self._send(404, text, 'text/html; charset=utf-8')
            return self._send(404, 'Not found')
        if full.endswith('.html'):
            text, _ = page_text(rel_of(full))
            return self._send(200, text, 'text/html; charset=utf-8')
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        size = os.path.getsize(full)
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                a = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2) or 0))
                b = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
                b = min(b, size - 1)
                if a <= b:
                    with open(full, 'rb') as fh:
                        fh.seek(a)
                        data = fh.read(b - a + 1)
                    return self._send(206, data, ctype, {'Content-Range': f'bytes {a}-{b}/{size}', 'Accept-Ranges': 'bytes'})
        with open(full, 'rb') as fh:
            data = fh.read()
        return self._send(200, data, ctype, {'Accept-Ranges': 'bytes'})

    # --- POST ---
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        rel = (q.get('path') or [''])[0]
        full = safe_path(rel)
        if not full:
            return self._json({'error': 'bad path'}, 400)
        try:
            if u.path == '/__ed/api/draft':
                with LOCK:
                    if rel not in DRAFTS:
                        DRAFT_BASE[rel] = disk_mtime(rel)
                    DRAFTS[rel] = self._body().decode('utf-8')
                return self._json({'ok': True})
            if u.path == '/__ed/api/discard':
                with LOCK:
                    DRAFTS.pop(rel, None)
                    DRAFT_BASE.pop(rel, None)
                return self._json({'ok': True})
            if u.path == '/__ed/api/save':
                text = strip_ids(self._body().decode('utf-8'))
                if not text.strip():
                    return self._json({'error': 'empty page'}, 400)
                if os.path.isfile(full):
                    backup(full)
                with open(full, 'w', encoding='utf-8', newline='\n') as fh:
                    fh.write(text)
                with LOCK:
                    DRAFTS.pop(rel, None)
                    DRAFT_BASE.pop(rel, None)
                return self._json({'ok': True})
            if u.path == '/__ed/api/upload':
                full = unique_name(full)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'wb') as fh:
                    fh.write(self._body())
                return self._json({'ok': True, 'path': rel_of(full)})
        except Exception as e:  # report instead of dropping the connection
            return self._json({'error': str(e)}, 500)
        return self._json({'error': 'unknown endpoint'}, 404)


def running_editor():
    """URL of an editor that is already running for this site folder, if any."""
    import urllib.request
    for p in range(8787, 8800):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{p}/__ed/api/whoami', timeout=0.4) as r:
                if json.loads(r.read().decode()).get('root') == ROOT:
                    return f'http://127.0.0.1:{p}/__ed/'
        except Exception:
            pass
    return None


def main():
    existing = running_editor()
    if existing:
        print('The editor is already open at ' + existing + ' - switching to it.')
        if '--no-browser' not in sys.argv:
            webbrowser.open(existing)
        return
    port = None
    for p in range(8787, 8800):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1', p)) != 0:
                port = p
                break
    if port is None:
        print('No free port between 8787 and 8799.')
        sys.exit(1)
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/__ed/'
    print(f'Site editor running at {url}')
    print('Keep this window open while you edit. Close it to stop the editor.')
    if '--no-browser' not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
