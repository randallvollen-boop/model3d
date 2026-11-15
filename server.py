#!/usr/bin/env python3
"""3D Models: a small page for the picture-to-3D kit.

  python3 server.py            serves http://127.0.0.1:8258/  (MODEL3D_BIND / MODEL3D_PORT change that)

Every action runs a model3d.py command as its own process; this file holds no build logic.
A model is a folder under models/. Its state is read from the files in it:
  refs/N.png  -> draft        candidate.png -> picture to check        manifest.json -> built
  job.json + job.log          the picture or build process now running, or the last one
"""
import functools, io, json, os, re, signal, subprocess, sys, time
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
import model3d as kit

KIT, OUT = kit.KIT, kit.OUT
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,47}$')
PAGE_FILE = re.compile(r'^model(-tex\d)?\.(json|jpg)$')
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
procs = {}     # slug -> Popen, kept so finished jobs are reaped

def folder(slug):
    d = os.path.join(OUT, slug)
    if not SLUG.match(slug) or not os.path.isdir(d): raise HTTPException(404, 'No such model.')
    return d

def read(path, default=None):
    try: return json.load(open(path))
    except (OSError, ValueError): return default

def running(slug, job):
    p = procs.get(slug)
    if p is not None and p.poll() is not None: procs.pop(slug)
    try: cmd = open(f"/proc/{job['pid']}/cmdline").read()
    except (OSError, KeyError, TypeError): return False
    return 'model3d.py' in cmd and slug in cmd

def state(slug):
    d = folder(slug); info = read(os.path.join(d, 'info.json'), {}); job = read(os.path.join(d, 'job.json')); man = read(os.path.join(d, 'manifest.json'))
    live = bool(job) and running(slug, job); has = lambda f: os.path.exists(os.path.join(d, f))
    st = 'built' if man else ('picturing' if job['kind'] == 'picture' else 'building') if live else 'review' if has('candidate.png') else 'draft'
    out = {'slug': slug, 'title': (man or info).get('title', slug), 'kind': info.get('kind'), 'state': st, 'created': info.get('created') or (man or {}).get('built', ''),
           'refs': len([f for f in os.listdir(os.path.join(d, 'refs'))]) if has('refs') else 0, 'stamp': int(max(os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)))}
    if job:
        lines = [l.strip() for l in open(os.path.join(d, 'job.log'), errors='replace').read().splitlines() if l.strip()] if has('job.log') else []
        if live: out['progress'] = lines[-1] if lines else 'starting'; out['seconds'] = int(time.time() - job['started'])
        elif st != 'built' and not any(l.startswith(('PICTURE', 'BUILT')) for l in lines):
            why = [l[5:].strip() for l in lines if l.startswith('STOP:')]
            out['error'] = (why[-1] if why else (lines[-1] if lines else 'It stopped without saying why.'))[:400]; out['failed'] = job['kind']
    out['made'] = has('picture.json')
    if live and job['kind'] == 'build':
        hit = [re.match(r'(full model|page copy):\s+\d+s\s+(.+?)\s+\(', l) for l in lines]; hit = [h for h in hit if h]
        out['step'] = f'{hit[-1][1]}: {hit[-1][2]}' if hit else 'starting the engine'
    if man: out['triangles'] = man['full']['triangles']; out['minutes'] = max(1, round(man['seconds'] / 60))
    return out

def launch(slug, kind, args):
    d = folder(slug)
    if running(slug, read(os.path.join(d, 'job.json'), {})): raise HTTPException(409, 'This model is already working.')
    log = open(os.path.join(d, 'job.log'), 'w')
    p = subprocess.Popen([sys.executable, '-u', os.path.join(KIT, 'model3d.py')] + args, stdout=log, stderr=subprocess.STDOUT, cwd=KIT, start_new_session=True)
    procs[slug] = p; json.dump({'kind': kind, 'pid': p.pid, 'started': time.time()}, open(os.path.join(d, 'job.json'), 'w'))
    return state(slug)

def building():
    return [s for s in os.listdir(OUT) if SLUG.match(s) and os.path.isdir(os.path.join(OUT, s)) and state(s)['state'] == 'building']

def blocked():
    """Why a build cannot start now, in the page's words; None when it can."""
    busy = building()
    if busy: return f'Another 3D build is running ({state(busy[0])["title"]}). One at a time.'
    return kit.blockers()

@app.get('/', response_class=HTMLResponse)
def home(): return open(os.path.join(KIT, 'ui.html')).read()

@app.get('/api/models')
def models():
    slugs = [s for s in os.listdir(OUT) if SLUG.match(s) and os.path.isdir(os.path.join(OUT, s))] if os.path.isdir(OUT) else []
    return sorted((state(s) for s in slugs), key=lambda m: m['created'], reverse=True)

@app.get('/api/models/{slug}')
def model(slug: str): return state(slug)

@app.get('/api/check')
def check():
    why = blocked()
    return {'ready': not why, 'reason': why}

@app.get('/api/kinds')
def kinds(): return [{'id': k, 'label': v['label']} for k, v in read(os.path.join(KIT, 'prompts.json'))['kinds'].items()]

@app.post('/api/models')
async def create(title: str = Form(...), kind: str = Form(...), pictures: list[UploadFile] = File(...)):
    from PIL import Image
    title = ' '.join(title.split())[:60]; base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]
    if not base: raise HTTPException(400, 'Give the model a name with letters or digits.')
    if kind not in read(os.path.join(KIT, 'prompts.json'))['kinds']: raise HTTPException(400, 'Pick what the picture shows.')
    ims = []
    for up in pictures[:3]:
        data = await up.read()
        if len(data) > 40 * 1048576: raise HTTPException(400, f'{up.filename} is over 40 MB.')
        try: im = Image.open(io.BytesIO(data)); im.load()
        except Exception: raise HTTPException(400, f'{up.filename} is not a picture this page can read.')
        im = im.convert('RGB'); im.thumbnail((4096, 4096)); ims.append(im)
    if not ims: raise HTTPException(400, 'Add at least one picture.')
    slug, n = base, 1
    while os.path.exists(os.path.join(OUT, slug)): n += 1; slug = f'{base}-{n}'
    os.makedirs(os.path.join(OUT, slug, 'refs'))
    for i, im in enumerate(ims): im.save(os.path.join(OUT, slug, 'refs', f'{i + 1}.png'))
    json.dump({'title': title, 'kind': kind, 'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'tries': 0}, open(os.path.join(OUT, slug, 'info.json'), 'w'))
    return state(slug)

@app.post('/api/models/{slug}/picture')
def make_picture(slug: str):
    d = folder(slug); info = read(os.path.join(d, 'info.json'))
    if not info or os.path.exists(os.path.join(d, 'manifest.json')): raise HTTPException(409, 'This model is already built.')
    if building(): raise HTTPException(409, 'A 3D build is running. Make the picture when it has finished.')
    refs = sorted(os.listdir(os.path.join(d, 'refs'))); seed = read(os.path.join(KIT, 'prompts.json'))['seed'] + info['tries']     # each redo moves to the next seed
    out = launch(slug, 'picture', ['picture', slug, '--kind', info['kind'], '--seed', str(seed)] + [x for r in refs for x in ('--ref', os.path.join(d, 'refs', r))])
    info['tries'] += 1; json.dump(info, open(os.path.join(d, 'info.json'), 'w'))
    return out

@app.post('/api/models/{slug}/as-is')
def as_is(slug: str):
    d = folder(slug)
    if state(slug)['state'] not in ('draft', 'review'): raise HTTPException(409, 'This model is busy or already built.')
    import shutil
    shutil.copy(os.path.join(d, 'refs', '1.png'), os.path.join(d, 'candidate.png'))
    for f in ('job.json', 'job.log', 'picture.json'):
        if os.path.exists(os.path.join(d, f)): os.remove(os.path.join(d, f))
    return state(slug)

@app.post('/api/models/{slug}/build')
def build(slug: str):
    d = folder(slug); s = state(slug)
    if s['state'] != 'review': raise HTTPException(409, 'There is no picture to build from yet.')
    why = blocked()
    if why: raise HTTPException(409, why)
    return launch(slug, 'build', ['build', os.path.join(d, 'candidate.png'), slug, '--title', s['title']])

@app.post('/api/models/{slug}/stop')
def stop(slug: str):
    job = read(os.path.join(folder(slug), 'job.json'), {})
    if running(slug, job): os.kill(job['pid'], signal.SIGTERM)      # the kit cancels the engine job, stops the Sandbox and frees the GPU lease
    return state(slug)

@app.post('/api/models/{slug}/trash')
def trash(slug: str):
    d = folder(slug)
    if running(slug, read(os.path.join(d, 'job.json'), {})): raise HTTPException(409, 'Stop it first.')
    if os.path.exists(os.path.join(d, 'manifest.json')): raise HTTPException(409, 'Built models stay. Remove the folder by hand if you mean it.')
    r = subprocess.run(['gio', 'trash', d], capture_output=True, text=True)
    if r.returncode: raise HTTPException(500, 'Could not move it to the trash: ' + r.stderr.strip()[:200])
    return {'ok': True}

@functools.lru_cache(maxsize=256)
def thumb_bytes(path, mtime, px):
    from PIL import Image
    im = Image.open(path).convert('RGB'); im.thumbnail((px, px)); b = io.BytesIO(); im.save(b, 'JPEG', quality=82); return b.getvalue()

@app.get('/pic/{slug}/{name}')
def pic(slug: str, name: str, px: int = 0):
    """name: candidate, source, ref1..ref3, or best (the best picture there is for a card)."""
    d = folder(slug); paths = {'candidate': 'candidate.png', 'source': 'source.png', 'ref1': 'refs/1.png', 'ref2': 'refs/2.png', 'ref3': 'refs/3.png'}
    pick = [paths[name]] if name in paths else ['source.png', 'candidate.png', 'refs/1.png'] if name == 'best' else []
    path = next((os.path.join(d, p) for p in pick if os.path.exists(os.path.join(d, p))), None)
    if not path: raise HTTPException(404, 'No such picture.')
    if px: return Response(thumb_bytes(path, os.path.getmtime(path), min(max(px, 64), 1600)), media_type='image/jpeg', headers={'Cache-Control': 'no-cache'})
    return FileResponse(path, headers={'Cache-Control': 'no-cache'})

@app.get('/files/{slug}/model.glb')
def glb(slug: str):
    path = os.path.join(folder(slug), 'model.glb')
    if not os.path.exists(path): raise HTTPException(404, 'Not built yet.')
    return FileResponse(path, media_type='model/gltf-binary', filename=f'{slug}.glb')

BAR = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}
.m3bar{display:flex;justify-content:space-between;align-items:center;gap:12px;max-width:980px;margin:0 auto 14px}
.m3bar a{font:500 14px/1 var(--body);color:var(--ink);text-decoration:none;border:1px solid var(--line);background:var(--panel);padding:11px 14px;border-radius:4px}
.m3bar a:hover{border-color:var(--moss)}</style></head><body>
<nav class="m3bar" aria-label="Model"><a href="/">&#8249; Models</a><a href="/files/SLUG/model.glb" download>Download model</a></nav>
'''

@app.get('/m/{slug}/', response_class=HTMLResponse)
def spin(slug: str):
    path = os.path.join(folder(slug), 'page', 'index.html')
    if not os.path.exists(path): raise HTTPException(404, 'Not built yet.')
    return BAR.replace('SLUG', slug) + open(path).read() + '</body></html>'

@app.get('/m/{slug}/{name}')
def spin_file(slug: str, name: str):
    path = os.path.join(folder(slug), 'page', name)
    if not PAGE_FILE.match(name) or not os.path.exists(path): raise HTTPException(404, 'No such file.')
    return FileResponse(path)

if __name__ == '__main__':
    import uvicorn
    os.makedirs(OUT, exist_ok=True)
    uvicorn.run(app, host=os.environ.get('MODEL3D_BIND', '127.0.0.1'), port=int(os.environ.get('MODEL3D_PORT', '8258')), log_level='warning')
