#!/usr/bin/env python3

import csv
from collections import defaultdict

md5_entries = defaultdict(list)

with open("rom_audit.csv", newline="") as f:
    reader = csv.reader(f)

    for row in reader:
        if len(row) >= 6 and row[5].startswith("md5:"):
            md5_entries[row[5]].append(row)

for md5, entries in md5_entries.items():
    if len(entries) > 1:
        print(f"\nDuplicate MD5: {md5} ({len(entries)} occurrences)")
        print("-" * 80)

        for entry in entries:
            print(",".join(entry))