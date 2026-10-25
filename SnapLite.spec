# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['snap.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['ssl', '_ssl', 'hashlib', '_hashlib', 'hmac', 'secrets', 'socket', '_socket', 'select', 'selectors', 'socketserver', 'http', 'urllib', 'email', 'unicodedata', 'stringprep', 'encodings.idna', 'decimal', '_decimal', '_pydecimal', 'lzma', '_lzma', 'bz2', '_bz2', 'gzip', 'tarfile', 'csv', 'json', 'xml', 'xmlrpc', 'plistlib', 'mimetypes', 'base64', 'quopri', 'calendar', 'datetime', '_pydatetime', '_strptime', 'statistics', 'fractions', 'numbers', 'random', 'ipaddress', 'logging', 'gettext', 'locale', 'argparse', 'getopt', 'optparse', 'tempfile', 'pprint', 'difflib', 'subprocess', 'signal', 'tracemalloc', 'py_compile', 'compileall', 'pickle', '_compat_pickle', 'dataclasses', 'asyncio', 'multiprocessing', 'concurrent', 'unittest', 'sqlite3', 'tkinter', 'pydoc', 'doctest', 'ensurepip', 'venv', 'distutils', 'setuptools', 'pkg_resources', 'lib2to3', 'curses', 'uuid', 'zoneinfo', 'graphlib', '_aix_support', '_compression'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='SnapLite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
