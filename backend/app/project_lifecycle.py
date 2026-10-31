"""Durable deletion with cross-process artifact leases on local POSIX storage."""
from __future__ import annotations

import re
import shutil
from contextlib import contextmanager

from starlette.responses import JSONResponse


@contextmanager
def artifact_lease(settings, project_id: str, *, exclusive=False, blocking=True):
    import fcntl
    root = settings.data_dir / '.locks'
    root.mkdir(mode=0o700, exist_ok=True)
    # IDs are validated UUIDs by every caller; lockfiles contain no private data.
    with (root / f'{project_id}.lock').open('a+b') as lock:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock, operation | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class ProjectLeaseMiddleware:
    """Keep streamed files and in-flight edits alive until their response finishes."""
    def __init__(self, app, settings, db):
        self.app, self.settings, self.db = app, settings, db

    async def __call__(self, scope, receive, send):
        match = re.match(r'^/api/projects/([0-9a-fA-F-]{36})(?:/|$)', scope.get('path', ''))
        if scope['type'] != 'http' or scope.get('method') == 'OPTIONS' or not match:
            return await self.app(scope, receive, send)
        project_id = match.group(1)
        from .auth import current_user_id
        # Unknown or foreign IDs must not allocate even an empty lock file.
        if not self.db.authorizes(project_id, current_user_id()):
            return await JSONResponse({'detail': 'Project not found.'}, status_code=404)(scope, receive, send)
        # Never block the event loop on an exclusive cleanup lease.
        lease = artifact_lease(self.settings, project_id, blocking=False)
        try:
            lease.__enter__()
        except BlockingIOError:
            return await JSONResponse({'detail': 'Project storage is being cleaned. Try again shortly.'},
                                      status_code=503, headers={'Retry-After':'1'})(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            lease.__exit__(None, None, None)


def cleanup_deleted_projects(settings, db):
    """Retry tombstones after restart; never race a live process or streamed file."""
    for project in db.pending_deletions():
        try:
            with artifact_lease(settings, project['id'], exclusive=True, blocking=False):
                directory = settings.data_dir / 'projects' / project['id']
                if directory.exists():
                    shutil.rmtree(directory)
                db.purge_deleted_project(project['id'])
        except (BlockingIOError, OSError):
            # Tombstone persists. A later worker sweep retries after users of the
            # files finish, or after the operator repairs storage permissions.
            continue
