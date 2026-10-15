#!/usr/bin/env bash
# Google Cloud / Linux: complete Wikidata snapshot -> named humans with P18 -> images.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

if [[ ${1:-} == --help || $# -gt 1 ]]; then
    cat <<'HELP'
Usage: bash scripts/download_all_p18.sh [OUTPUT_DIRECTORY]

Downloads the dated official JSON dump, verifies it, imports named human
entities with P18, downloads their images, and exports the results. There is no
item limit. Repeat the same command to resume. Default output: data/p18-humans.

Environment:
  P18_USER_AGENT       Existing downloader User-Agent (required)
  P18_IMAGE_WIDTH      Thumbnail width, default 500
  P18_ARCHIVE_WORKERS  Internet Archive connections, 0 to 10, default 10 (0 disables)
  P18_DUMP_DATE        Official dump date, default 20261005
  P18_DUMP_COMPRESSION gz (faster decompression, default) or bz2 (smaller download)
  P18_PYTHON           Python executable, default .venv/bin/python or python3
HELP
    exit 0
fi

: "${P18_USER_AGENT:?Set P18_USER_AGENT to the User-Agent used by download_p18.py}"
output_dir="${1:-data/p18-humans}"
dump_date="${P18_DUMP_DATE:-20261005}"
compression="${P18_DUMP_COMPRESSION:-gz}"
image_width="${P18_IMAGE_WIDTH:-500}"
archive_workers="${P18_ARCHIVE_WORKERS:-10}"
python_bin="${P18_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" && -z ${P18_PYTHON:-} ]]; then
    python_bin=python3
fi
if [[ ! $dump_date =~ ^[0-9]{8}$ || ! $compression =~ ^(gz|bz2)$ || ! $image_width =~ ^[0-9]+$ ]]; then
    printf '%s\n' 'Invalid dump date, compression, or image width.' >&2
    exit 1
fi
if [[ ! $archive_workers =~ ^([0-9]|10)$ ]]; then
    printf '%s\n' 'P18_ARCHIVE_WORKERS must be an integer from 0 to 10.' >&2
    exit 1
fi
command -v curl >/dev/null
command -v flock >/dev/null
"$python_bin" -c 'import httpx, PIL'
mkdir -p -- "$output_dir" downloads

# A separate lock spans the whole pipeline; individual Python stages also lock.
exec 9>"$output_dir/.all.lock"
flock -n 9 || { printf '%s\n' 'A full run already uses this output directory.' >&2; exit 1; }

# Catch accidental reuse of an incompatible catalog before downloading the dump.
"$python_bin" - "$output_dir" <<'PY'
import json
from pathlib import Path
import sqlite3
import sys

path = Path(sys.argv[1]).resolve() / 'state.sqlite3'
if path.exists():
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='catalog_source'").fetchone()
    if row:
        source = json.loads(row[0])
        if source.get('kind') == 'online_sample':
            raise SystemExit('Use a full human-data output directory; this directory contains an online sample.')
        if (source.get('kind') != 'dump' or source.get('scope') != 'humans'
                or not source.get('require_label')):
            raise SystemExit('Use a new human-only output directory, such as data/p18-humans; this catalog has different selection rules.')
PY

dump_name="wikidata-${dump_date}-all.json.${compression}"
dump_path="downloads/$dump_name"
checksums="downloads/wikidata-${dump_date}-sha1sums.txt"
base_url="https://dumps.wikimedia.org/wikidatawiki/entities/$dump_date"

# Also protect the shared dump when two outputs use the same dated snapshot.
exec 8>"$dump_path.lock"
flock 8
printf '[1/4] Downloading complete Wikidata snapshot %s\n' "$dump_date"
if [[ ! -f "$checksums" ]]; then
    curl --fail --location --retry 8 --retry-delay 15 --user-agent "$P18_USER_AGENT" \
        --output "$checksums.part" "$base_url/wikidata-${dump_date}-sha1sums.txt"
    mv -- "$checksums.part" "$checksums"
fi
if [[ ! -f "$dump_path" ]]; then
    curl --fail --location --retry 8 --retry-delay 15 --continue-at - \
        --user-agent "$P18_USER_AGENT" --output "$dump_path.part" "$base_url/$dump_name"
fi

# Verify once; bind the successful check to the unchanged local file's stat data.
# A .part file becomes the final dump only after the checksum matches.
"$python_bin" - "$dump_path" "$checksums" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

target, checksums = map(Path, sys.argv[1:])
entries = [line.split() for line in checksums.read_text().splitlines()]
expected = next((p[0] for p in entries if len(p) == 2 and p[1].lstrip('*') == target.name), None)
if expected is None:
    raise SystemExit(f'No official checksum found for {target.name}')
source = target if target.exists() else target.with_name(target.name + '.part')
marker = target.with_name(target.name + '.verified.json')
stat = source.stat()
state = {'sha1': expected, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
verified = False
if source == target and marker.exists():
    try:
        verified = json.loads(marker.read_text()) == state
    except (ValueError, OSError):
        pass
if not verified:
    print(f'Checking SHA-1 of {source.name} ...', flush=True)
    digest = hashlib.sha1()
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise SystemExit('Dump checksum mismatch; import was not started. Inspect the downloaded file.')
    if source != target:
        os.replace(source, target)
    temporary = marker.with_name(marker.name + '.part')
    temporary.write_text(json.dumps(state))
    os.replace(temporary, marker)
print(f'Complete dump verified: {target}', flush=True)
PY
flock -u 8

printf '[2/4] Importing named human entities with P18\n'
"$python_bin" scripts/download_p18.py import-dump \
    --dump "$dump_path" --output "$output_dir" --scope humans --require-label

printf '[3/4] Downloading human catalog images at width %s\n' "$image_width"
download_status=0
"$python_bin" scripts/download_p18.py download \
    --output "$output_dir" --width "$image_width" \
    --archive-workers "$archive_workers" --retry-failed || download_status=$?

printf '[4/4] Exporting complete catalog and current download results\n'
"$python_bin" scripts/download_p18.py export --output "$output_dir"
exit "$download_status"
