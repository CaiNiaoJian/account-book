"""Local complete backup, preview and restore into an independent book."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ...core.errors import ValidationError
from ...services import backups
from ..state import context_of

router = APIRouter(prefix="/api/backups", tags=["backups"])


@router.get("")
def list_backups(request: Request):
    ctx = context_of(request.app)
    items = []
    for path in sorted(ctx.paths.backups.glob('*.abk'), key=lambda item: item.stat().st_mtime, reverse=True)[:100]:
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.getinfo('manifest.json').file_size > 10 * 1024**2:
                    continue
                manifest = json.loads(archive.read('manifest.json'))
            items.append({'name': path.name, 'created_at': manifest['created_at'], 'summary': manifest['summary'], 'kind': manifest['kind']})
        except (OSError, zipfile.BadZipFile, ValueError, KeyError, TypeError):
            items.append({'name': path.name, 'kind': 'invalid', 'created_at': '', 'summary': {}})
    return {'items': items}


@router.post("")
def create_backup(request: Request):
    return backups.create(context_of(request.app))


@router.post('/import')
async def import_backup(request: Request):
    ctx = context_of(request.app)
    destination = ctx.paths.backups / f'import-{uuid4().hex}.abk'
    size = 0
    try:
        with destination.open('xb') as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > backups.MAX_BYTES:
                    raise ValidationError('备份文件超过 2 GB 上限')
                output.write(chunk)
        return await run_in_threadpool(backups.preview, ctx, destination.name)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@router.get('/{name}/download')
def download(request: Request, name: str):
    return FileResponse(backups.locate(context_of(request.app), name), filename=name, media_type='application/zip')


@router.get('/{name}/preview')
def preview(request: Request, name: str):
    return backups.preview(context_of(request.app), name)


@router.post('/{name}/restore')
def restore(request: Request, name: str):
    return backups.restore(context_of(request.app), name)


@router.post('/restored/{name}/open')
def open_restored(request: Request, name: str):
    ctx = context_of(request.app)
    parent = ctx.paths.data / 'restored'
    if Path(name).name != name or not name.startswith('book-') or '/' in name or '\\' in name:
        raise ValidationError('恢复账本不存在')
    target = parent / name
    if target.is_symlink() or not (target / 'accountbook.db').is_file():
        raise ValidationError('恢复账本不存在')
    command = [sys.executable] if ctx.paths.frozen else [sys.executable, str(ctx.paths.program_root / 'run.py')]
    subprocess.Popen([*command, '--data-dir', str(target)], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return {'status': 'ok'}
