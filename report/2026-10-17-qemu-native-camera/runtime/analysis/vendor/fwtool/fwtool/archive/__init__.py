# Minimal FAT-only package entry point; original UnixFile definition from fwtool.
from collections import namedtuple

UnixFile = namedtuple('UnixFile', 'path, size, mtime, mode, uid, gid, contents')
