"""Read-only PRE-LIVE evidence. No discovery, focus hooks, input or inference."""
import ctypes
import hashlib
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
from typing import Literal

from app.delivery.models import FrozenModel


class PreparationStatus(FrozenModel):
    state: Literal['PRE-LIVE PREPARED', 'PRE-LIVE NOT PREPARED', 'PREPARATION FAILED'] = 'PRE-LIVE NOT PREPARED'
    result: Literal['PENDING', 'READY', 'FAILED', 'CANCELLED'] | None = None
    launch_id: str | None = None
    reason: Literal['not_prepared', 'pending', 'failed', 'cancelled', 'identity_mismatch', 'prepared'] = 'not_prepared'
    backend_launch_id: str | None = None
    startup_configuration_revision: str | None = None


def repository_head(root):
    directory = root / '.git'
    if directory.is_file():
        pointer = directory.read_text(encoding='utf-8').strip()
        if not pointer.startswith('gitdir: '): return None
        directory = (root / pointer[8:]).resolve()
    value = (directory / 'HEAD').read_text(encoding='ascii').strip()
    if value.startswith('ref: '):
        reference = value[5:]
        if not re.fullmatch(r'refs/[A-Za-z0-9_./-]+', reference) or '..' in reference: return None
        ref_path = directory / reference
        if ref_path.is_file(): value = ref_path.read_text(encoding='ascii').strip()
        else:
            value = next((line.split(' ')[0] for line in (directory / 'packed-refs').read_text(encoding='ascii').splitlines()
                          if line.endswith(' ' + reference)), '')
    return value if re.fullmatch(r'[a-f0-9]{40}', value) else None


def process_identity(pid):
    """Limited process query only; FILETIME string matches PowerShell StartTime."""
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD); k.OpenProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)); k.GetProcessTimes.restype = w.BOOL
    k.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
    k.QueryFullProcessImageNameW.restype = w.BOOL
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle: raise ValueError('process_unavailable')
    try:
        times = [w.FILETIME() for _ in range(4)]
        name = ctypes.create_unicode_buffer(32768); size = w.DWORD(len(name))
        if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)): raise ValueError('process_unavailable')
        if not k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)): raise ValueError('process_unavailable')
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime), Path(name.value)
    finally: k.CloseHandle(handle)


class StartupPreparation:
    def __init__(self, settings, *, root=None):
        self.settings = settings
        self.root = root or Path(__file__).resolve().parents[3]
        self.launch_id = os.environ.get('ACCEPTANCE_LAUNCH_ID')
        # Startup-only contract; a new launcher must compare this to its own
        # freshly resolved settings before reusing a persistent backend.
        contract = {name: getattr(settings, name) for name in (
            'tiktok_transport', 'enable_physical_windows_send', 'enable_physical_tiktok_send',
            'admin_live_enabled', 'live_mode', 'diagnostic')}
        contract.update(account=settings.tiktok_username.strip().removeprefix('@').casefold(),
            windows=settings.windows.model_dump(mode='json'), cwd=str(Path.cwd().resolve()),
            **{name: str(getattr(settings, name).resolve()) for name in
               ('state_dir', 'log_dir', 'project_config_path', 'knowledge_path')})
        self.configuration_revision = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        try: self.loaded_head = repository_head(self.root)
        except (OSError, ValueError): self.loaded_head = None

    def status(self, **values):
        return PreparationStatus(backend_launch_id=self.launch_id,
            startup_configuration_revision=self.configuration_revision, **values)

    def view(self):
        if self.settings.tiktok_transport != 'windows_live_studio': return PreparationStatus()
        path = self.root / 'work/acceptance/startup/latest.json'
        try:
            with path.open('rb') as stream: raw = stream.read(65_537)
            if len(raw) > 65_536: return self.status()
            data = json.loads(raw)
            result, launch_id = data.get('result'), data.get('launch_id')
            if result not in {'PENDING', 'READY', 'FAILED', 'CANCELLED'} or not re.fullmatch(r'[a-f0-9]{32}', launch_id or ''):
                return self.status()
            if result != 'READY':
                return self.status(state='PREPARATION FAILED' if result in {'FAILED', 'CANCELLED'} else 'PRE-LIVE NOT PREPARED',
                    result=result, launch_id=launch_id, reason={'PENDING':'pending', 'FAILED':'failed', 'CANCELLED':'cancelled'}[result])
            s = self.settings
            valid = (data.get('git_head') == self.loaded_head == repository_head(self.root) and self.loaded_head is not None and
                data.get('git_branch') in {'main', 'feature/windows-live-studio'} and
                data.get('backend_launch_id') == self.launch_id and self.launch_id is not None and
                data.get('startup_configuration_revision') == self.configuration_revision and
                data.get('backend_pid') == os.getpid() and data.get('studio_pid') == s.windows.process_id and
                data.get('account') == s.tiktok_username.strip().removeprefix('@').casefold() and
                all(data.get(key) is True for key in ('auto_off', 'one_shot_available', 'physical_gate', 'numeric_uid_present')) and
                all(data.get(key) == 'ready' for key in ('lmstudio_status', 'model_status', 'backend_health', 'admin_health')) and
                bool(s.windows.broadcaster_user_id) and s.enable_physical_windows_send and s.windows.acceptance_one_shot)
            if valid:
                backend_start, backend_path = process_identity(os.getpid())
                studio_start, studio_path = process_identity(s.windows.process_id)
                valid = (data.get('backend_start_time') == backend_start and data.get('studio_start_time') == studio_start and
                    Path(data.get('backend_executable', '')) == backend_path and s.windows.executable_path == studio_path)
            return self.status(state='PRE-LIVE PREPARED' if valid else 'PRE-LIVE NOT PREPARED',
                result='READY', launch_id=launch_id, reason='prepared' if valid else 'identity_mismatch')
        except (OSError, ValueError, TypeError, AttributeError): return self.status()
