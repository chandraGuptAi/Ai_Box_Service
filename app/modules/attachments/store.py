"""Opaque, expiring IDs; caller identity must come from the trusted backend."""
import json
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException


class Store:
    def __init__(self, root: Path, ttl: int):
        self.root, self.ttl = root, ttl
        root.mkdir(parents=True, exist_ok=True)
        self._last_purge = 0
        self.purge_expired()

    def purge_expired(self):
        now = time.time()
        if now - self._last_purge < 60:
            return
        self._last_purge = now
        for folder in self.root.iterdir():
            if not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                continue
            try:
                meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
                if now - meta['created_at'] <= self.ttl:
                    continue
                for name in ('original', 'metadata.json'):
                    (folder / name).unlink(missing_ok=True)
                folder.rmdir()
            except (OSError, ValueError, KeyError):
                continue

    def create(self, owner, name, blob, inspect):
        self.purge_expired()
        identifier = uuid.uuid4().hex
        folder = self.root / identifier
        folder.mkdir()
        try:
            result = inspect(blob)
            metadata = {'id': identifier, 'owner': owner, 'filename': Path(name.replace('\\', '/')).name[:200],
                        'created_at': time.time(), 'inspection': result}
            (folder / 'original').write_bytes(blob)
            (folder / 'metadata.json').write_text(json.dumps(metadata, default=str), encoding='utf-8')
        except BaseException:
            for name in ('original', 'metadata.json'):
                (folder / name).unlink(missing_ok=True)
            folder.rmdir()
            raise
        return metadata

    def get(self, owner, identifier):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise HTTPException(404, 'Attachment not found')
        folder = self.root / identifier
        try:
            meta = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, 'Attachment not found') from None
        if meta['owner'] != owner or time.time() - meta['created_at'] > self.ttl:
            raise HTTPException(404, 'Attachment not found')
        try:
            return meta, (folder / 'original').read_bytes()
        except FileNotFoundError:
            raise HTTPException(404, 'Attachment not found') from None

    def delete(self, owner, identifier):
        self.get(owner, identifier)
        folder = self.root / identifier
        for name in ('metadata.json', 'original'):
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()

    def update_inspection(self, owner, identifier, inspection):
        meta, _ = self.get(owner, identifier)
        meta['inspection'] = inspection
        folder = self.root / identifier
        temporary = folder / ('metadata-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(meta, default=str), encoding='utf-8')
            temporary.replace(folder / 'metadata.json')
        finally:
            temporary.unlink(missing_ok=True)
