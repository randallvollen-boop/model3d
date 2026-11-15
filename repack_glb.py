"""Lighten a GLB for the web: JPEG textures, no tangents. Usage: repack_glb.py in.glb out.glb [max_texture_px]"""
import io, json, struct, sys
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
src, dst = sys.argv[1], sys.argv[2]; cap = int(sys.argv[3]) if len(sys.argv) > 3 else 2048
f = open(src, 'rb'); _, _, _ = struct.unpack('<4sII', f.read(12))
n, _ = struct.unpack('<I4s', f.read(8)); j = json.loads(f.read(n)); n, _ = struct.unpack('<I4s', f.read(8)); bin_ = f.read(n)
def view(i):
    v = j['bufferViews'][i]; o = v.get('byteOffset', 0); return bin_[o:o + v['byteLength']]
out, views = bytearray(), []
def add(data, extra=None):
    while len(out) % 4: out.append(0)
    views.append({'buffer': 0, 'byteOffset': len(out), 'byteLength': len(data), **(extra or {})}); out.extend(data); return len(views) - 1
for m in j['meshes']:
    for p in m['primitives']: p['attributes'].pop('TANGENT', None)
used = sorted({a for m in j['meshes'] for p in m['primitives'] for a in list(p['attributes'].values()) + [p['indices']]})
remap = {}
for a in used:
    acc = j['accessors'][a]; old = j['bufferViews'][acc['bufferView']]
    assert not old.get('byteStride') and not acc.get('byteOffset'), 'interleaved buffers not handled'
    acc['bufferView'] = add(view(acc['bufferView']), {k: old[k] for k in ('target',) if k in old}); remap[a] = len(remap)
j['accessors'] = [j['accessors'][a] for a in used]
for m in j['meshes']:
    for p in m['primitives']:
        p['attributes'] = {k: remap[v] for k, v in p['attributes'].items()}; p['indices'] = remap[p['indices']]
normal_tex = {m['normalTexture']['index'] for m in j['materials'] if 'normalTexture' in m}
normal_img = {j['textures'][t]['source'] for t in normal_tex}
for i, img in enumerate(j.get('images', [])):
    im = Image.open(io.BytesIO(view(img['bufferView']))).convert('RGB')
    if max(im.size) > cap: im = im.resize((cap, cap), Image.LANCZOS)
    b = io.BytesIO(); im.save(b, 'JPEG', quality=93 if i in normal_img else 86, subsampling=0 if i in normal_img else 2, optimize=True)
    img['bufferView'] = add(b.getvalue()); img['mimeType'] = 'image/jpeg'; print('image', i, im.size, len(b.getvalue()))
while len(out) % 4: out.append(0)
j['bufferViews'] = views; j['buffers'] = [{'byteLength': len(out)}]
js = json.dumps(j, separators=(',', ':')).encode(); js += b' ' * (-len(js) % 4)
with open(dst, 'wb') as o:
    o.write(struct.pack('<4sII', b'glTF', 2, 12 + 8 + len(js) + 8 + len(out))); o.write(struct.pack('<I4s', len(js), b'JSON')); o.write(js); o.write(struct.pack('<I4s', len(out), b'BIN\0')); o.write(out)
tris = sum(j['accessors'][p['indices']]['count'] for m in j['meshes'] for p in m['primitives']) // 3
print('wrote', dst, 12 + 16 + len(js) + len(out), 'bytes;', tris, 'triangles')
