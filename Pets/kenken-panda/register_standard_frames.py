"""Register already extracted generated pixels with one transform per state."""
import argparse
import json
from pathlib import Path
from PIL import Image

p = argparse.ArgumentParser()
p.add_argument('--frames-root', required=True)
p.add_argument('--json-out', required=True)
p.add_argument('--height', type=float, default=150)
p.add_argument('--baseline', type=float, default=200)
a = p.parse_args()
root = Path(a.frames_root)
report = {'purpose': 'deterministic shared row scale and ground registration', 'rows': {}}
for state in ['idle','running-right','running-left','waving','jumping','failed','waiting','running','review']:
    paths = sorted((root/state).glob('*.png'))
    images = [Image.open(path).convert('RGBA') for path in paths]
    boxes = [im.getbbox() for im in images]
    if not boxes or any(b is None for b in boxes):
        raise RuntimeError('missing frame in '+state)
    ref = boxes[0]
    ground = ref[3]
    cx = sum((b[0]+b[2])/2 for b in boxes)/len(boxes)
    scale = a.height/(ref[3]-ref[1])
    scale = min(scale, (a.baseline-8)/(ground-min(b[1] for b in boxes)))
    scale = min(scale, 176/max(b[2]-b[0] for b in boxes))
    # Apply the same scale and origin to every complete image in this row.
    tx = 96-scale*cx
    ty = a.baseline-scale*ground
    newboxes=[]
    for path,im in zip(paths,images):
        out = im.transform((192,208),Image.Transform.AFFINE,
            (1/scale,0,-tx/scale,0,1/scale,-ty/scale),
            resample=Image.Resampling.BICUBIC)
        box=out.getbbox()
        if box is None or box[0]<3 or box[1]<3 or box[2]>189 or box[3]>205:
            raise RuntimeError('unsafe transformed bounds '+state+': '+str(box))
        out.save(path)
        newboxes.append(box)
    report['rows'][state]={'scale':scale,'translation':[tx,ty],
        'source_bounds':boxes,'registered_bounds':newboxes}
Path(a.json_out).write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({'ok':True,'rows':len(report['rows'])}))
