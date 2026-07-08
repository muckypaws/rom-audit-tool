#!/usr/bin/env python3
"""
Quick diagnostic for Solarus .solarus archives.
Lists contents summary for each file to help identify missing assets.
Run on the device: python3 check_solarus.py /path/to/roms/solarus/
"""
import os, sys, zipfile

path = sys.argv[1] if len(sys.argv) > 1 else '.'

for fname in sorted(os.listdir(path)):
    if not fname.endswith('.solarus'):
        continue
    fpath = os.path.join(path, fname)
    try:
        with zipfile.ZipFile(fpath) as z:
            names = z.namelist()
            has_main = any('main.lua' in n for n in names)
            has_quest = any('quest.dat' in n for n in names)
            has_data  = any(n.startswith('data/') for n in names)
            total     = len(names)
            size_mb   = os.path.getsize(fpath) / 1048576
            status = 'OK' if (has_main or has_quest) else 'SUSPECT'
            print(f"[{status}] {fname}")
            print(f"       {total} files, {size_mb:.1f}MB, "
                  f"main.lua={'Y' if has_main else 'N'} "
                  f"quest.dat={'Y' if has_quest else 'N'} "
                  f"data/={'Y' if has_data else 'N'}")
    except Exception as e:
        print(f"[ERROR] {fname}: {e}")