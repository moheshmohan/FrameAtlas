"""Bounded directional sampling through FilmCraft's calibrated native 360 camera."""
import base64
import math
import struct

MAX_WINDOWS = 720
BATCH_WINDOWS = 16
INTERVAL_SECONDS = 20
MAX_FRAME_BYTES = 2 * 1024 ** 2
MAX_RENDER_BYTES = (BATCH_WINDOWS * 6 + 1) * MAX_FRAME_BYTES
DIRECTIONS = (("Heading 0°",0,0),("Heading 90°",90,0),("Heading 180°",180,0),
              ("Heading 270°",-90,0),("Up",0,90),("Down",0,-90))


def windows(duration, kind):
    try:
        duration = float(duration)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid 360 source duration")
    if not math.isfinite(duration) or duration < 0 or duration > (2**63-1)/254016000000:
        raise ValueError("Invalid 360 source duration")
    if kind == 'photo':
        return [{'start':0.0,'end':0.0,'at':0.0}]
    if duration <= 0:
        raise ValueError("360 video needs a positive source duration")
    count = min(MAX_WINDOWS,max(1,math.ceil(duration/INTERVAL_SECONDS)))
    return [{'start':i*duration/count,'end':(i+1)*duration/count,'at':(i+0.5)*duration/count} for i in range(count)]


def render_png(native, seconds, path):
    if native.cancelled():
        raise RuntimeError("Job cancelled")
    result = native.rpc('tools/call',{'name':'render_frame','arguments':{'seconds':seconds,'max_side':512}})
    if result.get('isError'):
        raise RuntimeError('Native 360 frame could not be rendered')
    block = next((x for x in result.get('content',[]) if x.get('type')=='image'),None)
    if not block or block.get('mimeType')!='image/png':
        raise RuntimeError('Native 360 renderer returned no PNG')
    encoded = block.get('data','')
    if not isinstance(encoded,str) or len(encoded)>MAX_FRAME_BYTES*4//3+4:
        raise ValueError('Native directional frame exceeds its byte limit')
    data = base64.b64decode(encoded,validate=True)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)<24 or data[12:16]!=b'IHDR' or len(data)>MAX_FRAME_BYTES:
        raise ValueError('Invalid or oversized native directional frame')
    if struct.unpack('>II',data[16:24])!=(512,512):
        raise ValueError('Native directional frame must be 512 × 512')
    path.write_bytes(data)
    return len(data)


def describe_directions(native, item, probe, kind, detail, stage):
    plan = windows(probe.get('duration',0)/254016000000,kind)
    native.run('file.newSequence',{'name':'Catalogue direction samples','width':512,'height':512,
                                 'fps':30,'video':1,'audio':0})
    placed = native.run('timeline.place',{'item':item,'seconds':0,'video':True,'audio':False})
    clips = placed.get('clips',[])
    if len(clips)!=1 or clips[0] not in placed.get('reframed360',[]):
        raise RuntimeError('Native media did not produce a validated 360 camera')
    clip = clips[0]
    native.run('media.toggleProxies',{'enabled':True})
    grouped=[[] for _ in plan];missing=0;total_bytes=0;peak_bytes=0
    reports=[];preview=None;preview_bytes=0;success=False
    # Retain one preview only. Each batch is described, removed from the native
    # project's media pool, and unlinked before rendering the next batch.
    try:
        for offset in range(0,len(plan),BATCH_WINDOWS):
            frames=[];batch_bytes=0;items=[]
            try:
                for wi in range(offset,min(offset+BATCH_WINDOWS,len(plan))):
                    window=plan[wi]
                    for di,(label,yaw,pitch) in enumerate(DIRECTIONS):
                        if native.cancelled():raise RuntimeError('Job cancelled')
                        stage('Rendering 360 directions locally · sample %d/%d · %s'%(wi+1,len(plan),label))
                        native.run('clip.reframe360',{'clips':[clip],'yaw':yaw,'pitch':pitch,'roll':0,
                                   'fov':100,'distance':0,'width':512,'height':512})
                        path=native.work/('direction-%03d-%d.png'%(wi,di))
                        # Track the path before writing so a failed render is cleaned too.
                        frame={'path':path,'window':wi,'label':label,'yaw':yaw,'pitch':pitch,'at':window['at']}
                        frames.append(frame)
                        size=render_png(native,window['at'],path)
                        batch_bytes+=size;total_bytes+=size
                        held=batch_bytes+(preview_bytes if offset else 0)
                        peak_bytes=max(peak_bytes,held)
                        if held>MAX_RENDER_BYTES:
                            raise ValueError('Directional frames exceeded the bounded analysis allowance')
                imported=native.tool('media_import',{'paths':[str(x['path']) for x in frames]})
                raw=imported.get('items',[])
                items=[i.get('id',i.get('item')) if isinstance(i,dict) else i for i in raw]
                if imported.get('errors') or len(items)!=len(frames):
                    raise RuntimeError('Some directional frames could not be imported; coverage was not completed')
                if any(not isinstance(i,int) or isinstance(i,bool) or i<=0 or i==item for i in items) or len(set(items))!=len(items):
                    raise RuntimeError('Native frame import returned an invalid media identity')
                stage('Describing 360 batch %d/%d · %d frames with the installed local vision model'%(
                    offset//BATCH_WINDOWS+1,math.ceil(len(plan)/BATCH_WINDOWS),len(frames)))
                reports.append(native.run('media.analyzeScenes',{'items':items,'detail':detail,'describe':True,
                                    'embed':False,'aesthetic':False,'wait':True}))
                for frame,frame_item in zip(frames,items):
                    if native.cancelled():raise RuntimeError('Job cancelled')
                    index=native.run('media.scenes',{'item':frame_item})
                    description=next((s.get('description') for s in index.get('shots',[]) if isinstance(s.get('description'),dict) and s['description'].get('caption')),None)
                    if not isinstance(description,dict):
                        missing+=1;description={'caption':'No local description produced for this view.'}
                    grouped[frame['window']].append({k:frame[k] for k in ('label','yaw','pitch','at')} | {'description':description})
                native.run('project.select',{'items':items})
                deleted=native.run('project.delete',{'items':items})
                if deleted.get('items')!=len(items) or deleted.get('offline',0):
                    raise RuntimeError('Native directional media could not be released; stopped before another batch')
                if preview is None:
                    preview=frames[0]['path'];preview_bytes=preview.stat().st_size
            finally:
                for frame in frames:
                    if frame['path']!=preview:frame['path'].unlink(missing_ok=True)
        success=True
    finally:
        if not success and preview is not None:preview.unlink(missing_ok=True)
    scenes=[]
    for window,views in zip(plan,grouped):
        # One temporal record includes six directions. The sample identity keeps
        # its corrections separate from a native shot with the same time range.
        caption='Sampled at %.3f seconds · '%window['at']+' | '.join(v['label']+': '+str(v['description'].get('caption',''))[:480] for v in views)
        scenes.append({'start':window['start'],'end':window['end'],'sample_key':'360-v1:%.6f:%.6f'%(window['start'],window['end']),
                       'sample_time':window['at'],'views':views,'description':{'caption':caption,'views':views},
                       'coverage':'Six fixed 100° square views at one sampled time; events between samples can be missed.'})
    spacing=(plan[0]['end']-plan[0]['start']) if plan else 0
    coverage={'version':2,'status':'partial' if missing else 'sampled','windows':len(plan),'directions_per_window':6,
              'frames_described':len(plan)*len(DIRECTIONS)-missing,'frames_missing':missing,'sample_spacing_seconds':spacing,
              'time_basis':'Requested source time; native renderer uses the nearest frame',
              'fov_degrees':100,'image_size':[512,512],'temporary_frame_bytes':total_bytes,'peak_temporary_frame_bytes':peak_bytes,
              'batch_windows':BATCH_WINDOWS,'batches':len(reports),'window_limit':MAX_WINDOWS,
              'target_spacing_seconds':INTERVAL_SECONDS,'spacing_capped':len(plan)==MAX_WINDOWS and spacing>INTERVAL_SECONDS,
              'source_projection':'Calibrated native VR camera, with the attached proxy used internally when available',
              'reports':reports}
    return scenes,coverage,preview.name
