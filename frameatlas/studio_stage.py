"""Studio transfers into an owned, quota-limited macOS disk image. No Studio credentials."""
import errno
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import time

from .native import cli,probe_media

MIB = 1024 ** 2
DEFAULT_QUOTA = 1536 * MIB
MAX_QUOTA = 32 * 1024**3
OVERHEAD = 32 * MIB
RECORD = '.studio-volume.json'
IMAGE = 'studio-source.sparseimage'
MOUNT = 'studio-source'
CONFIRMED = '.studio-confirmed'
JOB_NAME = re.compile(r'job-[A-Za-z0-9_-]{6,64}')
RETIRED_MODE = 0o400


class StageBusy(ValueError):
    pass


def command(*args, timeout=45):
    p=subprocess.run(['/usr/bin/hdiutil',*map(str,args)],capture_output=True,timeout=timeout)
    if p.returncode:
        raise StageBusy('Temporary Studio volume: '+p.stderr.decode(errors='replace')[-800:])
    if len(p.stdout)>4*MIB:
        raise ValueError('Disk-image response exceeded its limit')
    return p.stdout


def owned_work(data_root, name):
    if not isinstance(name,str) or not JOB_NAME.fullmatch(name):
        raise ValueError('Invalid owned source job')
    parent=Path(data_root).resolve()/'cache';work=parent/name
    if work.is_symlink() or work.resolve().parent!=parent or not work.is_dir():
        raise ValueError('Studio staging is outside the owned job cache')
    marker=work/'.frameatlas-owned'
    if marker.is_symlink() or not marker.is_file():raise ValueError('Source job ownership marker is missing')
    return work


def work_removed(path):
    """An absent path or a zero-byte retirement marker holds no staged media."""
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return True
    return (stat.S_ISREG(entry.st_mode) and entry.st_size == 0
            and stat.S_IMODE(entry.st_mode) == RETIRED_MODE)


def retire_work(path):
    """Prevent Studio from recreating an unmounted destination as a host folder."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, RETIRED_MODE)
    except FileExistsError:
        if not work_removed(path):
            raise StageBusy('The retired Studio path is occupied. Check it in Studio before retrying cleanup.')
    else:
        try:
            os.fchmod(fd, RETIRED_MODE)
        finally:
            os.close(fd)


def guard_previous_destinations(store, cache_root):
    """Protect recorded, already removed paths from earlier app versions."""
    with store.connect() as db:
        rows = db.execute("""SELECT DISTINCT
            json_extract(stage,'$.work_name'),json_extract(stage,'$.destination')
            FROM (SELECT CASE WHEN json_valid(result)
                THEN json_extract(result,'$.studio_download') END AS stage
                FROM jobs WHERE state IN ('done','cancelled','failed'))
            WHERE CASE WHEN json_valid(stage) THEN json_extract(stage,'$.state') END='removed'""").fetchall()
    root = cache_root.resolve()
    for name, destination in rows:
        if not isinstance(name, str) or not JOB_NAME.fullmatch(name):
            continue
        path = root / name
        mount = path / MOUNT
        if destination != str(mount) or path.is_symlink() or mount.is_symlink():
            continue
        if path.is_dir() and not os.path.ismount(path) and not os.path.ismount(mount):
            # Studio can recreate an empty destination even if no transfer follows.
            # rmdir never removes source files or directories that still contain them.
            try:
                if mount.is_dir():
                    mount.rmdir()
                path.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY):
                    raise
        if not path.exists() and not path.is_symlink():
            retire_work(path)


def record(work):
    path=Path(work)/RECORD
    try:
        if not path.exists():return None
        if path.is_symlink() or path.stat().st_size>16384:raise ValueError('Invalid Studio volume record')
        r=json.loads(path.read_text())
    except FileNotFoundError:return None
    if not isinstance(r,dict) or r.get('image')!=IMAGE or r.get('mount')!=MOUNT:
        raise ValueError('Invalid Studio volume paths')
    quota=r.get('quota')
    if not isinstance(quota,int) or isinstance(quota,bool) or not 512*MIB<=quota<=MAX_QUOTA or quota%MIB:
        raise ValueError('Invalid Studio volume quota')
    return r


def write_record(work, data):
    temporary=Path(work)/'.studio-volume.next'
    temporary.write_text(json.dumps(data));os.replace(temporary,Path(work)/RECORD)


def reservation(work):
    r=record(work)
    return r['quota']+OVERHEAD if r else 0


def mounted_images():
    data=plistlib.loads(command('info','-plist'))
    images=data.get('images',[]) if isinstance(data,dict) else None
    if not isinstance(images,list) or len(images)>1024:raise StageBusy('Invalid mounted-image inventory')
    return images


def mount_at(entity, mount):
    registered = entity.get('mount-point')
    # macOS may report a canonical /private/var path for an input under /var.
    return isinstance(registered, str) and Path(registered).is_absolute() and Path(registered).resolve() == mount


def matching_mount(work):
    """Verify the backing image before detaching; never touch another mounted disk."""
    work=Path(work);image=work/IMAGE;mount=work/MOUNT
    if image.is_symlink() or mount.is_symlink():raise ValueError('Studio volume paths cannot be symlinks')
    mount=mount.resolve()
    found=[]
    for entry in mounted_images():
        if not isinstance(entry,dict):continue
        entities=entry.get('system-entities',[])
        if not isinstance(entities,list) or len(entities)>64:raise StageBusy('Invalid disk-image entity list')
        for entity in entities:
            if not isinstance(entity,dict):raise StageBusy('Invalid disk-image mount entry')
            if mount_at(entity,mount):
                backing=entry.get('image-path','')
                if not backing or Path(backing).resolve()!=image.resolve():
                    raise StageBusy('A different disk occupies the staging folder; nothing was detached')
                found.append(entity)
    if len(found)>1:raise StageBusy('Ambiguous Studio mount; nothing was detached')
    if os.path.ismount(mount) and not found:raise StageBusy('Unverified disk at staging folder; nothing was detached')
    return bool(found)


def release(work,external_finished=False):
    r=record(work)
    if not r:return False
    if matching_mount(work):
        if not external_finished and not confirmed(Path(work)/CONFIRMED):
            raise StageBusy('Studio transfer is unconfirmed. Finish or cancel it in Studio, then explicitly clean this temporary source; it was retained to keep its quota in place.')
        # No force: an external writer must finish or be cancelled in Studio first.
        command('detach',str(Path(work)/MOUNT),timeout=20)
    elif r.get('phase')=='preparing' and time.time()-float(r.get('created',0))<60:
        raise StageBusy('Volume preparation is still settling; retry cleanup shortly')
    if os.path.ismount(Path(work)/MOUNT):raise StageBusy('Studio source is still mounted; files retained safely')
    return True


def create(work, available, limit=DEFAULT_QUOTA):
    work=Path(work).resolve()
    if record(work):raise ValueError('This job already has a Studio source volume')
    if not isinstance(limit,int) or isinstance(limit,bool) or not 512*MIB<=limit<=MAX_QUOTA:
        raise ValueError('Invalid temporary source volume limit')
    quota=min(limit,max(0,int(available)-OVERHEAD))//MIB*MIB
    if quota<512*MIB:raise ValueError('Not enough temporary capacity for a Studio source; finish or clean the active transfer first')
    if shutil.disk_usage(work).free<quota+OVERHEAD+3*1024**3:
        raise ValueError('Studio source needs its quota plus the 3 GiB free-space reserve')
    image=work/IMAGE;mount=work/MOUNT
    if image.exists() or mount.exists():raise ValueError('Temporary Studio volume path already exists')
    mount.mkdir(mode=0o700)
    r={'image':IMAGE,'mount':MOUNT,'quota':quota,'created':time.time(),'phase':'preparing'}
    write_record(work,r)
    command('create','-size',str(quota//MIB)+'m','-type','SPARSE','-fs','APFS','-volname','FrameAtlas Temporary Source',str(image))
    output=plistlib.loads(command('attach','-nobrowse','-mountpoint',str(mount),'-plist',str(image)))
    if not any(isinstance(e,dict) and mount_at(e,mount) for e in output.get('system-entities',[])):
        raise StageBusy('Native mount did not return the requested destination')
    os.chmod(mount,0o700)
    r['phase']='ready';write_record(work,r)
    return mount,quota


def files_named(mount, filename):
    found=[];visited=0
    for root,dirs,files in os.walk(mount,followlinks=False):
        visited+=1
        if visited>256:raise ValueError('Studio output has too many directories')
        dirs[:]=[d for d in dirs if not (Path(root)/d).is_symlink() and not d.startswith('.')]
        if len(files)>4096:raise ValueError('Studio output contains too many files')
        for name in files[:4096]:
            p=Path(root)/name
            if name.casefold()==filename.casefold() and not p.is_symlink() and p.is_file():found.append(p)
        if len(found)>1:raise ValueError('Several files match this recording; no source was chosen automatically')
    return found[0] if found else None


def create_for(worker, assets, work, for_analysis):
    available=worker.cache.budget()-(256*MIB if for_analysis else 0)
    # Leave half a GiB for native working images, disk-image overhead and metadata.
    limit=max(DEFAULT_QUOTA,min(MAX_QUOTA,worker.cache.cap-512*MIB))
    if sum(asset.get('bytes', 0) for asset in assets) > min(limit,max(0,available-OVERHEAD)):
        raise ValueError('Known camera originals exceed this temporary allowance; choose a larger storage limit or attach the complete read-only source package')
    return create(work,available,limit=limit)


def needs_trailer(asset):
    return Path(asset['filename']).suffix.lower() in ('.insv', '.insp')


def validated_source(mount, paths, asset, quota, records):
    from .camera_parts import validated_parts
    parts = validated_parts(mount, paths, records) if needs_trailer(asset) else [
        {'path': str(path), 'filename': path.name} for path in paths]
    if not parts:
        return None
    if sum(path.stat().st_size for path in paths)>quota:
        raise ValueError('Source logical size exceeds the temporary volume allowance')
    for path in paths:
        probe_media(path)
    for key in ('proxy','second_lens','secondLens'):
        value = parts[0].get(key)
        if value:
            asset['locator'][key] = value
    if len(parts)>1:
        asset['locator']['recording_parts'] = parts
    return parts


def scan_recordings(mount):
    return cli('exec','insta360.list',json.dumps({'folders':[str(mount)]}),timeout=60).get('recordings',[])


def recording_signature(entry):
    return entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns


def retirement_plan(mount, filename, parts):
    """Snapshot a dedicated Studio folder containing only validated media."""
    folder = mount / (Path(filename).stem + '-Original')
    paths = {Path(part[key]) for part in parts for key in ('path', 'proxy', 'second_lens', 'secondLens')
             if part.get(key)}
    if not paths or any(path.parent != folder for path in paths):
        return None
    try:
        entry = folder.lstat()
        if not stat.S_ISDIR(entry.st_mode) or entry.st_dev != mount.stat().st_dev:
            return None
        if set(folder.iterdir()) != paths:
            return None
        signatures = {}
        for path in paths:
            item = path.lstat()
            if not stat.S_ISREG(item.st_mode) or item.st_dev != entry.st_dev or item.st_nlink != 1:
                return None
            signatures[path.name] = recording_signature(item)
        return folder, (entry.st_dev, entry.st_ino), signatures
    except OSError:
        return None


def retire_recording(work, jid, plan):
    """Release a confirmed member's unchanged package after analysis is saved."""
    if not plan or not confirmed(member_confirmation(work, jid)) or confirmed(work / CONFIRMED):
        return 0
    folder, identity, signatures = plan
    if folder.parent != work / MOUNT:
        return 0
    try:
        if not matching_mount(work):
            return 0
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            entry = os.fstat(fd)
            if (entry.st_dev, entry.st_ino) != identity or set(os.listdir(fd)) != set(signatures):
                return 0
            for name, signature in signatures.items():
                if recording_signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) != signature:
                    return 0
            for name in signatures:
                os.unlink(name, dir_fd=fd)
        finally:
            os.close(fd)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0  # Shared-volume cleanup remains the fallback.
    return sum(signature[2] for signature in signatures.values())


def acquire(worker, asset, work, jid, for_analysis=True):
    from .camera_parts import names, source_paths
    from .studio_batch import index_waiting_photo

    _, counters = names(asset["filename"])
    mount, quota = create_for(worker, [asset], work, for_analysis)
    stage = {
        "work_name": work.name, "destination": str(mount), "filename": asset["filename"],
        "quota_bytes": quota, "state": "waiting", "requires_confirmation": True,
        "expected_parts": len(counters),
        "instructions": "In signed-in Studio, select this exact recording → Download → Browse to this destination. "
                        "Leave Set as default download path OFF. Wait until Studio reports the entire transfer "
                        "complete, then confirm this job in FrameAtlas. The volume enforces a hard size limit.",
    }
    worker.store.job_result(jid, {"studio_download": stage})
    worker.state(jid, "Waiting for Studio download · " + asset["filename"])
    deadline = time.monotonic() + 1200
    marker = work / CONFIRMED
    last, stable_since = None, None
    attempted = set()
    can_index_while_waiting = True

    def should_yield():
        return confirmed(marker) or worker.draining.is_set() or worker.cancelled(jid)

    while (now := time.monotonic()) < deadline:
        if worker.cancelled(jid):
            raise RuntimeError("Job cancelled; cancel any active transfer in Studio before cleaning its temporary volume")
        if confirmed(marker):
            paths = source_paths(mount, asset["filename"])
            signature = tuple((str(path), st.st_size, st.st_mtime_ns) for path in paths for st in (path.stat(),))
            if not signature or any(size <= 0 for _, size, _ in signature):
                last, stable_since = None, None
            elif signature != last:
                last, stable_since = signature, now
            elif stable_since is not None and now - stable_since >= 2:
                records = scan_recordings(mount) if needs_trailer(asset) else []
                parts = validated_source(mount, paths, asset, quota, records)
                if parts:
                    stage.update(state="ready", part_filenames=[part["filename"] for part in parts])
                    worker.store.job_result(jid, {"studio_download": stage})
                    return paths[0], "Temporary Studio download in an owned quota-limited volume; every recording part removed after this job"
        elif can_index_while_waiting and deadline - now >= 45 and not should_yield():
            cap = worker.cache.cap
            try:
                if index_waiting_photo(worker, attempted, quota, should_yield):
                    continue
            except Exception as exc:
                can_index_while_waiting = False
                print("Waiting-photo work stopped for this Studio transfer: " + str(exc)[:300], flush=True)
            finally:
                worker.cache.cap = cap
        worker.stop.wait(0.5)
    raise ValueError("Studio download timed out. Cancel it in Studio if still active, then retry or clean the owned temporary source.")


def member_confirmation(work, jid):
    return work / f'{CONFIRMED}-{jid}'


def confirmed(marker):
    return not marker.is_symlink() and marker.is_file()


def mark_confirmed(marker):
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        if not confirmed(marker):
            raise ValueError('Invalid Studio confirmation marker')


def confirm(store,jid,individual=False):
    if not isinstance(jid,int) or isinstance(jid,bool) or jid<=0:raise ValueError('Use a positive job ID')
    if not isinstance(individual,bool):raise ValueError('Individual confirmation must be a boolean')
    job=store.job(jid)
    if job['state']!='running':raise ValueError('Only an active Studio transfer can be confirmed')
    stage=job['result'].get('studio_download',{})
    work=owned_work(store.root,stage.get('work_name'))
    if not matching_mount(work):raise ValueError('Temporary source volume is not mounted')
    from .camera_parts import source_paths

    def all_parts_arrived(filename):
        paths = source_paths(work/MOUNT, filename)
        return bool(paths) and all(path.stat().st_size > 0 for path in paths)

    if individual:
        if not stage.get('batch'):
            raise ValueError('Individual confirmation requires a Studio batch job')
        if stage.get('state') in ('ready', 'released') and confirmed(member_confirmation(work, jid)):
            return {'confirmed':True,'job':jid,'scope':'recording'}
        if not all_parts_arrived(stage.get('filename','')):
            raise ValueError('Every nonempty part of this recording must arrive before confirmation. Check the current job’s download destination in Studio.')
        mark_confirmed(member_confirmation(work, jid))
        return {'confirmed':True,'job':jid,'scope':'recording'}
    members = stage.get('batch') or [{'id': jid, 'filename': stage.get('filename', '')}]
    files = []
    for member in members:
        peer = job if member['id'] == jid else store.job(member['id'])
        if peer['state'] in ('cancelling', 'cancelled'):
            continue
        if peer['state'] != 'running':
            raise ValueError('Wait for the remaining Studio jobs to start before confirmation')
        peer_stage = peer['result'].get('studio_download', {})
        if (peer_stage.get('work_name') == work.name and peer_stage.get('state') in ('ready', 'released')
                and confirmed(member_confirmation(work, member['id']))):
            continue
        files.append(member['filename'])
    if not all(all_parts_arrived(filename) for filename in files):
        raise ValueError('Every expected part of the remaining recordings must arrive and be nonempty before confirmation. Check the current job’s download destination in Studio.')
    mark_confirmed(work/CONFIRMED)
    return {'confirmed':True,'job':jid}


def cleanup(store,jid):
    if not isinstance(jid,int) or isinstance(jid,bool) or jid<=0:raise ValueError('Use a positive job ID')
    job=store.job(jid)
    if job['state'] in ('queued','running','cancelling'):raise ValueError('Finish or cancel the source job before cleanup')
    result=job['result'];stage=result.get('studio_download',{});name=stage.get('work_name')
    peers = [store.job(member['id']) for member in stage.get('batch', [])] or [job]
    for peer in peers:
        if peer['state'] in ('queued','running','cancelling'):
            raise ValueError('Finish or cancel every job sharing this Studio source before cleanup')
    if not isinstance(name,str) or not JOB_NAME.fullmatch(name):raise ValueError('Invalid owned source job')
    candidate=store.root/'cache'/name
    already_removed = work_removed(candidate)
    if not already_removed:
        work=owned_work(store.root,name)
        from .worker import Cache
        Cache(store.root).remove(work,studio_finished=True)
    for peer in peers:
        peer_stage = peer['result'].get('studio_download', {})
        if peer_stage.get('work_name') == name:
            peer_stage['state'] = 'removed'
            store.job_result(peer['id'], peer['result'])
    return {'cleaned':True,'already_removed':already_removed,'job':jid}
