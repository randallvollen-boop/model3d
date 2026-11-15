"""Split a GLB into served file types: a .json glTF (geometry inline as base64) plus .jpg textures.
Usage: python3 split_gltf.py in.glb out_dir name"""
import sys, json, struct, base64, os
src, out, name = sys.argv[1:4]
d = open(src, 'rb').read()
jl = struct.unpack_from('<I', d, 12)[0]; g = json.loads(d[20:20 + jl]); bo = 20 + jl + 8; bl = struct.unpack_from('<I', d, 20 + jl)[0]; bin_ = d[bo:bo + bl]
img_views = {im['bufferView']: i for i, im in enumerate(g['images'])}
new_bin = bytearray(); remap = {}; views = []
for i, bv in enumerate(g['bufferViews']):
    chunk = bin_[bv.get('byteOffset', 0):bv.get('byteOffset', 0) + bv['byteLength']]
    if i in img_views:
        im = g['images'][img_views[i]]; ext = {'image/jpeg': 'jpg', 'image/png': 'png'}[im['mimeType']]
        fn = f'{name}-tex{img_views[i]}.{ext}'; open(os.path.join(out, fn), 'wb').write(chunk)
        im.pop('bufferView'); im.pop('mimeType'); im['uri'] = fn; print(fn, len(chunk)); continue
    while len(new_bin) % 4: new_bin.append(0)
    nb = dict(bv); nb['byteOffset'] = len(new_bin); nb['buffer'] = 0; new_bin += chunk; remap[i] = len(views); views.append(nb)
for a in g['accessors']: a['bufferView'] = remap[a['bufferView']]
g['bufferViews'] = views
g['buffers'] = [{'byteLength': len(new_bin), 'uri': 'data:application/octet-stream;base64,' + base64.b64encode(bytes(new_bin)).decode()}]
p = os.path.join(out, name + '.json'); open(p, 'w').write(json.dumps(g, separators=(',', ':'))); print(p, os.path.getsize(p), 'geometry bytes', len(new_bin))
