"""Exercise the full shell pipeline with a real tiny dump and mocked HTTP."""

import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


REPO = Path(__file__).resolve().parents[1]


class FullDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("download_p18.py", "download_all_p18.sh"):
            shutil.copy2(REPO / "scripts" / name, scripts / name)
        fixtures = []
        for number in range(1, 206):
            fixtures.append({"id": f"Q{number}", "labels": {"en": {"value": f"Name {number}"}},
                             "claims": {"P18": [{"rank": "normal", "mainsnak": {
                                 "snaktype": "value", "datavalue": {"value": f"Image {number}.png"}}}]}})
        payload = gzip.compress(("[\n" + ",\n".join(json.dumps(e) for e in fixtures) + "\n]\n").encode())
        self.dump = self.root / "fixture.gz"
        self.dump.write_bytes(payload)
        self.expected = hashlib.sha1(payload).hexdigest()
        stream = io.BytesIO()
        Image.new("RGB", (16, 16)).save(stream, "PNG")
        png = base64.b64encode(stream.getvalue()).decode()
        bins = self.root / "bin"
        bins.mkdir()
        self.executable(bins / "flock", "#!/bin/sh\nexit 0\n")
        self.executable(bins / "curl", f"""#!{sys.executable}
import os
from pathlib import Path
import sys
args = sys.argv[1:]
target = Path(args[args.index('--output') + 1])
with open('fetches.log', 'a') as log:
    log.write(args[-1] + '\\n')
if args[-1].endswith('-sha1sums.txt'):
    digest = '0' * 40 if os.environ.get('TEST_BAD_SHA1') else {self.expected!r}
    target.write_text(digest + '  wikidata-20261005-all.json.gz\\n')
else:
    target.write_bytes(Path({str(self.dump)!r}).read_bytes())
""")
        self.python = bins / "fixture-python"
        self.executable(self.python, f"""#!{sys.executable}
import base64
import importlib.util
import os
from pathlib import Path
import sys
if len(sys.argv) < 2 or sys.argv[1] != 'scripts/download_p18.py':
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
import httpx
spec = importlib.util.spec_from_file_location('p18', 'scripts/download_p18.py')
p18 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p18)
def handler(request):
    with open('requests.log', 'a') as log:
        log.write(request.url.host + '\\n')
    if request.url.host == 'commons.wikimedia.org':
        pages = []
        for title in request.url.params['titles'].split('|'):
            pages.append({{'title': title, 'imageinfo': [{{
                'url': 'https://upload.wikimedia.org/original.png',
                'thumburl': 'https://thumb.wikimedia.org/thumb.png',
                'width': 1600, 'mime': 'image/png'}}]}})
        return httpx.Response(200, json={{'query': {{'pages': pages}}}})
    return httpx.Response(200, content=base64.b64decode({png!r}))
class Limiter:
    def wait(self, group): pass
    def pause(self, group, delay): pass
Network = p18.Network
p18.Network = lambda user_agent: Network(user_agent, client=httpx.Client(transport=httpx.MockTransport(handler)), limiter=Limiter())
sys.exit(p18.main(sys.argv[2:]))
""")
        self.env = dict(os.environ, P18_USER_AGENT="fixture/1 (offline)",
                        P18_PYTHON=str(self.python), P18_DUMP_DATE="20261005",
                        P18_DUMP_COMPRESSION="gz", P18_IMAGE_WIDTH="500",
                        PATH=str(bins) + os.pathsep + os.environ["PATH"])

    @staticmethod
    def executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_full(self):
        return subprocess.run(["bash", "scripts/download_all_p18.sh"], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_full_pipeline_exceeds_200_and_resumes_without_network(self):
        result = self.run_full()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((self.root / "data/p18-all/summary.json").read_text())
        self.assertEqual(summary["entities"], 205)
        self.assertEqual(summary["file_status"], {"downloaded": 205})
        self.assertEqual(summary["image_width"], 500)
        self.assertTrue(summary["dump_progress"]["complete"])
        fetches = (self.root / "fetches.log").read_text()
        requests = (self.root / "requests.log").read_text()
        result = self.run_full()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "fetches.log").read_text(), fetches)
        self.assertEqual((self.root / "requests.log").read_text(), requests)

    def test_bad_checksum_stops_before_import_and_keeps_partial(self):
        self.env["TEST_BAD_SHA1"] = "1"
        result = self.run_full()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.root / "data/p18-all/state.sqlite3").exists())
        self.assertTrue((self.root / "downloads/wikidata-20261005-all.json.gz.part").exists())
        self.assertFalse((self.root / "downloads/wikidata-20261005-all.json.gz").exists())

    def test_sample_directory_is_detected_before_large_download(self):
        output = self.root / "data/p18-all"
        output.mkdir(parents=True)
        with sqlite3.connect(output / "state.sqlite3") as db:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO meta VALUES ('catalog_source', ?)",
                       (json.dumps({"kind": "online_sample"}),))
        result = self.run_full()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("online sample", result.stderr)
        self.assertFalse((self.root / "fetches.log").exists())


if __name__ == "__main__":
    unittest.main()
