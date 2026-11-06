"""Workspace command-path admission prototype. Metadata only; no source reads."""
import os
from pathlib import Path
import re

PREPARATION={'chmod','date','printf','pwd','bash','sh'}
INTERPRETERS={'bash','sh'}

def resolve(name,cwd):
    if not isinstance(name,str) or not name or any(c in name for c in '\x00*?[~'):
        return None
    name=name.replace('\\','/')
    if os.name=='nt':
        match=re.match(r'^/(?:mnt/)?([A-Za-z])/(.*)$',name)
        if match:name=match[1].upper()+':/'+match[2]
    path=Path(name)
    if not path.is_absolute():
        if cwd is None:return None
        path=Path(cwd)/path
    try:return path.resolve()
    except (OSError,RuntimeError,ValueError):return None

def within(path,root):
    if path is None or root is None:return False
    try:path.relative_to(root);return True
    except ValueError:return False

def remember(name,cwd,root,prepared):
    path=resolve(name,cwd)
    if within(path,root):prepared.add(path)

def advance(words,cwd):
    args=words[1:]
    if args and args[0][0] in ('--','-L','-P'):args=args[1:]
    if len(args)!=1 or not args[0][1] or args[0][0]=='-':return None
    return resolve(args[0][0],cwd)

def local_command(name,cwd,root,prepared):
    if '/' not in name.replace('\\','/'):return False
    target=resolve(name,cwd)
    if not within(target,root):return False
    if target in prepared:return True
    try:return target.is_file()
    except OSError:return False
