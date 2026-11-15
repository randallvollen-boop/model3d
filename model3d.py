#!/usr/bin/env python3
"""Picture-to-3D kit: one picture in, a textured 3D model and a spin page out. Same picture, same result.

  model3d.py picture <slug> --kind person|head|creature --ref <picture> [--ref ...] [--seed N]   make the clean source picture
  model3d.py build <picture> <slug> --title "Name" [--alongside]   full build (about 10 min)
  model3d.py check                                                why a build could not start now, or "ready"
  model3d.py page <slug>                                          rebuild the spin page only
  model3d.py list                                                 models built so far

Read RUNBOOK.md first. The graph (graph.json) and its seeds are frozen; do not edit them per model.
"""
import argparse, asyncio, base64, datetime, glob, hashlib, io, json, os, shutil, signal, subprocess, sys, time, urllib.request, uuid

KIT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(KIT, 'models')
STUDIO = '127.0.0.1:8189'
COMFY_IN, COMFY_OUT = os.path.expanduser('~/comfyui-input'), os.path.expanduser('~/comfyui-output')
SANDBOX, H3 = '127.0.0.1:8202', '127.0.0.1:8199'
AI_STACK = os.path.expanduser('~/ai-stack')
LOCK = [sys.executable, os.path.join(AI_STACK, 'tools', 'team_lock.py')]
NEED_GB = 52                      # free memory a build needs: the measured peak plus headroom
N_IMAGE, N_FACES, N_TEX, N_GLB = '122', '186', '288', '322'   # graph.json node ids
FULL = {'faces': 700000, 'tex': 4096}; WEB = {'faces': 200000, 'tex': 2048}
STAGES = (('RemoveBackground', 'cutting out the subject'), ('MoGeInference', 'estimating the camera'), ('Pixal3D', 'reading the picture'),
          ('KSampler', 'sampling'), ('VaeDecodeShape', 'decoding the shape'), ('Remesh', 'rebuilding the mesh'), ('Decimate', 'thinning the mesh'),
          ('Unwrap', 'unwrapping for textures'), ('BakeAmbient', 'baking shade'), ('BakeNormal', 'baking fine detail'),
          ('VaeDecodeTexture', 'decoding the texture'), ('BakeTexture', 'baking colour'), ('ApplyTexture', 'applying textures'), ('SaveGLB', 'saving'))

def say(*a): print(*a, flush=True)
def stop(msg): say('STOP:', msg); sys.exit(2)
def avail(): return int(next(l for l in open('/proc/meminfo') if l.startswith('MemAvailable')).split()[1]) // 1048576
def http(host, path, body=None, timeout=30):
    r = urllib.request.Request(f'http://{host}{path}', data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read() or b'{}')
def queue(host):
    q = http(host, '/queue', timeout=5); return len(q['queue_running']) + len(q['queue_pending'])
def sha(path): return hashlib.sha256(open(path, 'rb').read()).hexdigest()

def lock(*args): return subprocess.run(LOCK + list(args), capture_output=True, text=True, cwd=AI_STACK)   # the lease tool only works inside the ai-stack repo

def blockers(alongside=False):
    """Why a build cannot start right now, in plain words; None when it can."""
    if avail() < NEED_GB: return f'Only {avail()} GB of memory is free and a build needs {NEED_GB}. Unload H3 on the Studio Services tab.'
    try:
        if queue(H3): return 'H3 is rendering. Wait for it to finish.'
    except OSError: pass          # H3 stopped is fine
    try:
        if queue(SANDBOX): return 'The Sandbox engine is busy with another job.'
    except OSError: pass          # Sandbox stopped is fine; the build starts it
    lease = lock('status')
    if lease.returncode: return 'Could not read the GPU lease: ' + lease.stderr.strip()[-200:]
    held = [' '.join(l.split()) for l in lease.stdout.splitlines() if l.startswith('gpu')]
    if held and not alongside: return 'The GPU is in use: ' + held[0]
    return None

def preflight(alongside):
    why = blockers(alongside)
    if why: stop(why + ('' if 'GPU is in use' not in why else ' Wait for it, or pass --alongside to run beside it.'))
    if [l for l in lock('status').stdout.splitlines() if l.startswith('gpu')]: return ''
    got = lock('acquire', 'gpu', '--team', 'studio', '--session', 'model3d', '--purpose', 'picture to 3D build', '--ttl-min', '40')
    if got.returncode: stop('could not take the GPU lease: ' + (got.stderr or got.stdout).strip()[-200:])
    return got.stdout.strip()     # lease id; empty when running alongside another holder

def sandbox_up():
    """Start the Sandbox engine if it is stopped. Returns True when this run started it."""
    running = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', 'sandbox'], capture_output=True, text=True).stdout.strip() == 'true'
    if not running: say('starting the Sandbox engine'); subprocess.run(['docker', 'start', 'sandbox'], check=True, capture_output=True)
    for _ in range(60):
        try:
            if queue(SANDBOX) == 0: return not running
            stop('the Sandbox engine is busy with another job.')
        except OSError: time.sleep(3)
    stop('the Sandbox engine did not come up in 3 minutes.')

async def run(graph, label):
    import websockets
    cid = uuid.uuid4().hex; t0 = time.time(); cur = ''; stages = []
    async with websockets.connect(f'ws://{SANDBOX}/ws?clientId={cid}', max_size=None) as ws:
        try: pid = http(SANDBOX, '/prompt', {'prompt': graph, 'client_id': cid})['prompt_id']
        except urllib.error.HTTPError as e: stop('the engine refused the graph: ' + e.read().decode()[:1200])
        say(f'{label}: submitted {pid}')
        while True:
            el = time.time() - t0
            if avail() < 15: http(SANDBOX, '/interrupt', {}); stop(f'{label}: memory ran low at {el:.0f}s in {cur}; build cancelled.')
            if el > 1800: http(SANDBOX, '/interrupt', {}); stop(f'{label}: still running after 30 min in {cur}; build cancelled.')
            try: m = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError: continue
            if not isinstance(m, str): continue
            m = json.loads(m); t, d = m.get('type'), m.get('data', {})
            if d.get('prompt_id') not in (None, pid): continue
            if t == 'executing' and d.get('node'):
                c = graph[d['node']]['class_type']; cur = c
                name = next((n for k, n in STAGES if k in c), None)
                if name: stages.append([round(el), name]); say(f'{label}: {el:4.0f}s  {name}  ({avail()} GB free)')
            elif t == 'execution_error': stop(f"{label}: failed at {el:.0f}s in {d.get('node_type')}: {str(d.get('exception_message'))[:500]}")
            elif t == 'execution_interrupted': stop(f'{label}: interrupted at {el:.0f}s in {cur}.')
            elif t == 'execution_success': say(f'{label}: done in {el:.0f}s'); return {'prompt_id': pid, 'seconds': round(el), 'stages': stages}

def graph_for(slug, spec, keep_maps):
    g = json.load(open(os.path.join(KIT, 'graph.json'))); tag = f'model3d_{slug}' + ('' if keep_maps else '_web')
    g[N_IMAGE]['inputs']['image'] = f'model3d-{slug}.png'; g[N_FACES]['inputs']['target_face_count'] = spec['faces']; g[N_TEX]['inputs']['value'] = spec['tex']
    for k in list(g):
        v = g[k]
        if v['class_type'] == 'SaveImage':
            if keep_maps: v['inputs']['filename_prefix'] = v['inputs']['filename_prefix'].replace('dragon3d_earth', tag)
            else: del g[k]
    g[N_GLB]['inputs']['filename_prefix'] = f'3d/{tag}'
    used, todo = set(), [N_GLB] + [k for k, v in g.items() if v['class_type'] == 'SaveImage']      # keep only what feeds an output
    while todo:
        k = todo.pop()
        if k in used: continue
        used.add(k); todo += [x[0] for x in g[k]['inputs'].values() if isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and x[0] in g]
    return {k: g[k] for k in g if k in used}

def newest(pattern, since):
    f = [p for p in glob.glob(os.path.join(COMFY_OUT, '3d', pattern)) if os.path.getmtime(p) >= since]
    if not f: stop(f'no output matched {pattern}')
    return max(f, key=os.path.getmtime)

def glb_info(path):
    import struct
    d = open(path, 'rb').read(); n = struct.unpack_from('<I', d, 12)[0]; j = json.loads(d[20:20 + n])
    return sum(j['accessors'][p['indices']]['count'] for m in j['meshes'] for p in m['primitives']) // 3

def build(a):
    from PIL import Image
    slug, d = a.slug, os.path.join(OUT, a.slug)
    if not os.path.isfile(a.picture): stop(f'no picture at {a.picture}')
    if os.path.exists(os.path.join(d, 'manifest.json')): stop(f'{slug} is already built in {d}. Pick a new slug; finished models are never overwritten.')
    took_lease = preflight(a.alongside); started = False; t0 = time.time()
    try:
        started = sandbox_up()
        os.makedirs(os.path.join(d, 'maps'), exist_ok=True); os.makedirs(os.path.join(d, 'web'), exist_ok=True)
        src = os.path.join(d, 'source.png'); Image.open(a.picture).convert('RGB').save(src); shutil.copy(src, os.path.join(COMFY_IN, f'model3d-{slug}.png'))
        full = asyncio.run(run(graph_for(slug, FULL, True), 'full model'))
        shutil.copy(newest(f'model3d_{slug}_0*.glb', t0), os.path.join(d, 'model.glb'))
        for m in ('base_color', 'normal', 'ambiant_occlusion', 'roughness', 'metallic'):
            shutil.copy(newest(f'model3d_{slug}_{m}_*.png', t0), os.path.join(d, 'maps', m.replace('ambiant', 'ambient') + '.png'))
        web = asyncio.run(run(graph_for(slug, WEB, False), 'page copy'))
        tmp = os.path.join(d, 'web', 'tmp.glb')
        for cmd in ([os.path.join(KIT, 'repack_glb.py'), newest(f'model3d_{slug}_web_0*.glb', t0), tmp, str(WEB['tex'])], [os.path.join(KIT, 'split_gltf.py'), tmp, os.path.join(d, 'web'), 'model']):
            subprocess.run([sys.executable] + cmd, check=True, capture_output=True)
        os.remove(tmp)
    except KeyboardInterrupt:
        try:
            http(SANDBOX, '/interrupt', {})
            for _ in range(20):
                if queue(SANDBOX) == 0: break
                time.sleep(2)
        except OSError: pass
        stop('build stopped.')
    finally:
        try: idle = queue(SANDBOX) == 0
        except OSError: idle = True
        if started and idle: subprocess.run(['docker', 'stop', 'sandbox'], capture_output=True); say('Sandbox engine stopped again')
        if took_lease: lock('release', 'gpu', '--lease-id', took_lease)
    import numpy as np
    flat = {m: float(np.asarray(Image.open(os.path.join(d, 'maps', m + '.png')).convert('L').resize((256, 256))).std() / 255) for m in ('roughness', 'metallic')}
    man = {'slug': slug, 'title': a.title or slug, 'built': datetime.datetime.now().isoformat(timespec='seconds'), 'source_sha256': sha(src),
           'graph_sha256': sha(os.path.join(KIT, 'graph.json')), 'full': {**FULL, **full, 'triangles': glb_info(os.path.join(d, 'model.glb')), 'bytes': os.path.getsize(os.path.join(d, 'model.glb'))},
           'web': {**WEB, **web, 'bytes': sum(os.path.getsize(p) for p in glob.glob(os.path.join(d, 'web', '*')))},
           'map_spread': flat, 'seconds': full['seconds'] + web['seconds']}
    g = json.load(open(os.path.join(d, 'web', 'model.json'))); man['web']['triangles'] = sum(g['accessors'][p['indices']]['count'] for m in g['meshes'] for p in m['primitives']) // 3
    json.dump(man, open(os.path.join(d, 'manifest.json'), 'w'), indent=1); page(a)
    say(f"BUILT {slug}: {man['full']['triangles']:,} triangles, {man['seconds'] // 60} min {man['seconds'] % 60} s\n  model  {d}/model.glb\n  page   {d}/page/index.html")

def picture(a):
    """Make the clean source picture from reference pictures. Fixed wording per kind (prompts.json); same references and seed give the same picture."""
    from PIL import Image
    cfg = json.load(open(os.path.join(KIT, 'prompts.json'))); kind = cfg['kinds'].get(a.kind) or stop('kind must be one of: ' + ', '.join(cfg['kinds']))
    d = os.path.join(OUT, a.slug); os.makedirs(d, exist_ok=True); seed = a.seed if a.seed is not None else cfg['seed']
    if os.path.exists(os.path.join(d, 'manifest.json')): stop(f'{a.slug} is already built.')
    names = []
    for i, r in enumerate(a.ref[:3]):
        if not os.path.isfile(r): stop(f'no picture at {r}')
        names.append(f'model3d-{a.slug}-ref{i + 1}.png'); Image.open(r).convert('RGB').save(os.path.join(COMFY_IN, names[-1]))
    body = {'subject': kind['prompt'], 'checkpoint': cfg['checkpoint'], 'width': kind['width'], 'height': kind['height'], 'seed': seed, 'identity_references': names}
    req = urllib.request.Request(f'http://{STUDIO}/generate/async', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', **({'X-Agent-Origin': 'claude'} if os.environ.get('MODEL3D_AGENT') else {})})
    try: job = json.loads(urllib.request.urlopen(req, timeout=30).read())['job_id']
    except urllib.error.HTTPError as e: stop('Studio refused the picture: ' + e.read().decode()[:400])
    except OSError as e: stop(f'Studio is not answering on {STUDIO}: {e}')
    say(f'picture: submitted {job} (seed {seed})'); t0 = time.time()
    while True:
        st = http(STUDIO, f'/jobs/{job}').get('status')
        if st == 'completed': break
        if st in ('failed', 'error', 'cancelled'): stop(f'picture: Studio job {st}.')
        if time.time() - t0 > 900: stop('picture: still waiting after 15 minutes.')
        time.sleep(4)
    out = os.path.join(d, 'candidate.png'); urllib.request.urlretrieve(f'http://{STUDIO}/jobs/{job}/image', out + '.part'); os.replace(out + '.part', out)
    json.dump({'kind': a.kind, 'seed': seed, 'job_id': job, 'references': [sha(os.path.join(COMFY_IN, n)) for n in names], 'seconds': round(time.time() - t0)}, open(os.path.join(d, 'picture.json'), 'w'), indent=1)
    say(f'PICTURE {a.slug}: {out}')

def page(a):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    d = os.path.join(OUT, a.slug); man = json.load(open(os.path.join(d, 'manifest.json'))); pg = os.path.join(d, 'page'); os.makedirs(pg, exist_ok=True)
    def uri(path, px, q=80):
        im = Image.open(path).convert('RGB'); im.thumbnail((px, px), Image.LANCZOS); b = io.BytesIO(); im.save(b, 'JPEG', quality=q, optimize=True)
        return 'data:image/jpeg;base64,' + base64.b64encode(b.getvalue()).decode()
    flat = [m for m, s in man['map_spread'].items() if s < 0.02]
    note = '' if not flat else '<p class="note">' + (' and '.join(m.capitalize() for m in flat)) + (' maps were' if len(flat) > 1 else ' map was') + ' also made but came out as one flat value, so the surface shine is uniform.</p>'
    mins = max(1, round(man['seconds'] / 60)); html = open(os.path.join(KIT, 'page.template.html')).read()
    for k, v in {'{{TITLE}}': man['title'], '{{HERO}}': uri(os.path.join(d, 'source.png'), 720, 82), '{{MAP_COLOR}}': uri(os.path.join(d, 'maps', 'base_color.png'), 360),
                 '{{MAP_NORMAL}}': uri(os.path.join(d, 'maps', 'normal.png'), 360), '{{MAP_AO}}': uri(os.path.join(d, 'maps', 'ambient_occlusion.png'), 360),
                 '{{TRIS}}': f"{round(man['web']['triangles'] / 1000)}k", '{{FULL_TRIS}}': f"{round(man['full']['triangles'] / 1000)}k", '{{BUILD}}': f'About {mins} minutes',
                 '{{GLB_MB}}': f"{os.path.getsize(os.path.join(d, 'web', 'model.json')) / 1048576:.0f}", '{{NOTE}}': note}.items():
        html = html.replace(k, v)
    assert '{{' not in html
    open(os.path.join(pg, 'index.html'), 'w').write(html)
    for f in glob.glob(os.path.join(d, 'web', 'model*')): shutil.copy(f, pg)
    say('page written:', os.path.join(pg, 'index.html'))

def listing(a):
    for m in sorted(glob.glob(os.path.join(OUT, '*', 'manifest.json'))):
        j = json.load(open(m)); say(f"{j['slug']:24} {j['built']}  {j['full']['triangles']:>9,} triangles  {j['title']}")

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('build'); b.add_argument('picture'); b.add_argument('slug'); b.add_argument('--title', default=''); b.add_argument('--alongside', action='store_true'); b.set_defaults(fn=build)
    p = sp.add_parser('page'); p.add_argument('slug'); p.set_defaults(fn=page)
    l = sp.add_parser('list'); l.set_defaults(fn=listing)
    c = sp.add_parser('picture'); c.add_argument('slug'); c.add_argument('--kind', required=True); c.add_argument('--ref', action='append', required=True); c.add_argument('--seed', type=int); c.set_defaults(fn=picture)
    k = sp.add_parser('check'); k.set_defaults(fn=lambda a: say(blockers() or 'ready'))
    a = ap.parse_args()
    if getattr(a, 'slug', None) and not all(c.isalnum() or c == '-' for c in a.slug): stop('slug: letters, digits and dashes only')
    def term(*_): raise KeyboardInterrupt          # the page's Stop sends SIGTERM; treat it like Ctrl-C so a build cleans up
    signal.signal(signal.SIGTERM, term)
    try: a.fn(a)
    except KeyboardInterrupt: stop('stopped.')
