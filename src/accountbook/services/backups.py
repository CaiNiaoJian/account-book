"""Complete local backups: committed SQLite, attachments and preferences, verified before restore."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

from alembic.script import ScriptDirectory
from alembic.util import CommandError

from .. import __version__
from ..config import UserPreferences
from ..core.errors import ConflictError, ValidationError
from ..db.migrations import MIGRATIONS_DIR, run_migrations
from ..db.session import Database

MAX_BYTES = 2 * 1024**3
MAX_FILES = 100000


def _digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _summary(root: Path) -> dict:
    with closing(sqlite3.connect(root.joinpath('accountbook.db').resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValidationError('账本完整性检查未通过')
        revision = db.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        if ScriptDirectory(str(MIGRATIONS_DIR)).get_revision(revision) is None:
            raise ValidationError('此备份的数据库版本不受支持，请先更新应用')
        refs = db.execute('SELECT file_ref FROM attachments').fetchall()
        for (ref,) in refs:
            relative = _safe_name('attachments/' + ref)
            if not (root / relative).is_file():
                raise ValidationError('备份缺少账本引用的附件', file_ref=ref)
        return {
            'transactions': db.execute('SELECT COUNT(*) FROM transactions WHERE deleted_at IS NULL').fetchone()[0],
            'accounts': db.execute('SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL').fetchone()[0],
            'attachments': len(refs),
            'revision': revision,
        }


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if (not name or '\\' in name or path.is_absolute() or str(path) != name or
        any(part in {'.', '..'} or ':' in part or part.endswith((' ', '.')) or
            part.split('.')[0].upper() in reserved or any(ord(c) < 32 for c in part) for part in path.parts)):
        raise ValidationError('备份内的文件路径不合法')
    if name not in {'accountbook.db', 'config.json', 'manifest.json'} and not name.startswith('attachments/'):
        raise ValidationError('备份含不支持的文件')
    return name


def _pack(ctx, output: Path, *, kind: str) -> dict:
    with ctx.database.maintenance(), tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp)
        with closing(sqlite3.connect(ctx.database.path)) as source, closing(sqlite3.connect(root / 'accountbook.db')) as target:
            source.backup(target)
        (root / 'config.json').write_text(ctx.config.snapshot().model_dump_json(indent=2), encoding='utf-8')
        summary = _summary_database_only(root)
        files = {'accountbook.db': root / 'accountbook.db', 'config.json': root / 'config.json'}
        for attachment in ctx.paths.attachments.rglob('*'):
            if attachment.is_symlink():
                raise ValidationError('附件目录含符号链接，无法完整备份')
            if attachment.is_file():
                files['attachments/' + attachment.relative_to(ctx.paths.attachments).as_posix()] = attachment
        # Validate every DB reference before committing a backup; never report a partial archive as successful.
        with closing(sqlite3.connect(root / 'accountbook.db')) as db:
            for (ref,) in db.execute('SELECT file_ref FROM attachments'):
                if _safe_name('attachments/' + ref) not in files:
                    raise ValidationError('原账本缺少附件，备份未完成', file_ref=ref)
        manifest = {'format': 1, 'version': __version__, 'created_at': datetime.now().isoformat(), 'kind': kind,
            'summary': summary, 'files': {name: {'size': path.stat().st_size, 'sha256': _digest(path)} for name, path in files.items()}}
        if len(files) > MAX_FILES or sum(item['size'] for item in manifest['files'].values()) > MAX_BYTES:
            raise ValidationError('完整备份超过当前支持的 2 GB 上限')
        temporary = output.with_suffix('.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, path in files.items():
                    archive.write(path, _safe_name(name))
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest


def _summary_database_only(root: Path) -> dict:
    with closing(sqlite3.connect(root / 'accountbook.db')) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValidationError('原账本完整性检查未通过')
        return {'transactions': db.execute('SELECT COUNT(*) FROM transactions WHERE deleted_at IS NULL').fetchone()[0],
            'accounts': db.execute('SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL').fetchone()[0],
            'attachments': db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]}


def create(ctx, *, kind='manual') -> dict:
    ctx.paths.backups.mkdir(parents=True, exist_ok=True)
    output = ctx.paths.backups / f'{kind}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.abk'
    manifest = _pack(ctx, output, kind=kind)
    return {'name': output.name, **manifest}


def locate(ctx, name: str) -> Path:
    if Path(name).name != name or not name.endswith('.abk') or '/' in name or '\\' in name:
        raise ValidationError('请选择有效的备份文件')
    path = ctx.paths.backups / name
    if path.is_symlink() or not path.is_file():
        raise ValidationError('备份文件不存在')
    return path


def unpack(archive_path: Path, root: Path) -> dict:
    """No extractall: reject traversal, links, duplicates and oversized archives before writing."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1 or sum(item.file_size for item in entries) > MAX_BYTES:
                raise ValidationError('备份超过大小或文件数量限制')
            seen = set()
            for item in entries:
                _safe_name(item.filename)
                if item.is_dir() or item.filename.casefold() in seen or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValidationError('备份含重复路径或不支持的链接')
                seen.add(item.filename.casefold())
            if 'manifest.json' not in archive.namelist() or archive.getinfo('manifest.json').file_size > 10 * 1024**2:
                raise ValidationError('备份缺少有效清单')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest['format'] != 1 or set(manifest['files']) | {'manifest.json'} != set(archive.namelist()):
                raise ValidationError('备份文件与清单不一致')
            for name, expected in manifest['files'].items():
                destination = root / _safe_name(name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, destination.open('xb') as target:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                if destination.stat().st_size != expected['size'] or _digest(destination) != expected['sha256']:
                    raise ValidationError('备份文件校验失败', file=name)
            preferences = UserPreferences.model_validate_json((root / 'config.json').read_bytes())
            summary = _summary(root)
            return {**manifest, 'summary': summary, 'settings': {'theme': preferences.theme, 'language': preferences.language},
                'attachment_files': sum(name.startswith('attachments/') for name in manifest['files'])}
    except (OSError, sqlite3.DatabaseError, zipfile.BadZipFile, ValueError, KeyError, TypeError, CommandError) as error:
        raise ValidationError('备份损坏或格式不受支持，未修改当前账本') from error


def preview(ctx, name: str) -> dict:
    with tempfile.TemporaryDirectory(dir=ctx.paths.backups) as temp:
        return {'name': name, **unpack(locate(ctx, name), Path(temp))}


def restore(ctx, name: str) -> dict:
    # Restore into a new book, never overwrite the running book. Publication is a single rename.
    parent = ctx.paths.data / 'restored'
    parent.mkdir(exist_ok=True)
    destination = parent / f'book-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}'
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        root = Path(temp) / 'book'
        root.mkdir()
        manifest = unpack(locate(ctx, name), root)
        database = Database(root / 'accountbook.db')
        try:
            run_migrations(database)
        finally:
            database.dispose()
        if destination.exists():
            raise ConflictError('恢复目录已存在，未覆盖任何账本')
        os.rename(root, destination)
    return {'directory': str(destination), 'name': destination.name, 'summary': manifest['summary']}


def scheduled(ctx, *, now: datetime | None = None) -> dict | None:
    moment = now or datetime.now()
    prefs = ctx.config.snapshot()
    if not prefs.backup_enabled:
        return None
    previous = list(ctx.paths.backups.glob('auto-*.abk'))
    if previous and max(item.stat().st_mtime for item in previous) > (moment - timedelta(hours=prefs.backup_interval_hours)).timestamp():
        return None
    return create(ctx, kind='auto')
