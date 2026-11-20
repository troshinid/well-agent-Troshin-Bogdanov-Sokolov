"""Explicit Windows/SLB environment for an MCP stdio subprocess.

The SDK's default environment is deliberately narrow. Do not pass all of
os.environ: an LLM API key, for example, is not needed by the PIPESIM worker.
This module only preserves already configured values; it does not set a license
server, change a license file, or print environment values.
"""

import os


RUNTIME_NAMES = frozenset({
    'ALLUSERSPROFILE', 'APPDATA', 'COMSPEC', 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)',
    'COMMONPROGRAMW6432', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'PATH', 'PATHEXT',
    'PROCESSOR_ARCHITECTURE', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'PROGRAMW6432', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TMP', 'USERDOMAIN',
    'USERNAME', 'USERPROFILE', 'WINDIR',
    'SLBSLS_LICENSE_FILE', 'LM_LICENSE_FILE', 'CLBFD_LICENSE_FILE', 'LMGRD.SLB_LICENSE_FILE',
})
DIAGNOSTIC_NAMES = ('PROGRAMDATA', 'ALLUSERSPROFILE', 'SLBSLS_LICENSE_FILE',
                    'LM_LICENSE_FILE', 'CLBFD_LICENSE_FILE', 'LMGRD.SLB_LICENSE_FILE')


def server_environment(source=None):
    source = os.environ if source is None else source
    return {name.upper(): value for name, value in source.items()
            if name.upper() in RUNTIME_NAMES and isinstance(value, str) and not value.startswith('()')}


def environment_presence(environment):
    """Boolean diagnostics only: never serialize license paths or server names."""
    return {name: bool(environment.get(name)) for name in DIAGNOSTIC_NAMES}
