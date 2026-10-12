import bz2
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from PIL import Image


SPEC = importlib.util.spec_from_file_location("p18", Path(__file__).resolve().parents[1] / "scripts/download_p18.py")
p18 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p18)


def statement(prop, value, rank="normal"):
    return {"id": "Q42$test", "rank": rank, "mainsnak": {"property": prop,
            "snaktype": "value", "datavalue": {"value": value}},
            "qualifiers": {"P2096": [{"snaktype": "value", "datavalue": {"value": {"text": "caption", "language": "en"}}}]}}


def entity(qid="Q42", filename="Portrait.jpg", human=True):
    return {"id": qid, "type": "item", "lastrevid": 123,
            "labels": {"en": {"value": "A person" if human else "A city"}},
            "aliases": {"en": [{"value": "Another name"}]},
            "descriptions": {"en": {"value": "description"}},
            "claims": {"P31": [statement("P31", {"id": "Q5" if human else "Q515"})],
                       "P18": [statement("P18", filename)]}}


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), (120, 50, 20)).save(stream, "PNG")
    return stream.getvalue()


class FakeLimiter:
    def __init__(self):
        self.delays = []

    def wait(self, group):
        pass

    def pause(self, group, delay):
        self.delays.append((group, delay))


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = p18.Catalog(self.root / "out")
        self.networks = []

    def tearDown(self):
        for network in self.networks:
            network.close()
        self.catalog.close()
        self.temp.cleanup()

    def network(self, handler, retries=2):
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        network = p18.Network("test/1 (offline)", client=client, limiter=FakeLimiter(), retries=retries)
        self.networks.append(network)
        return network

    def add(self, item=None):
        record = p18.entity_record(item or entity(), "all", {"kind": "test"})
        self.catalog.add(record)
        self.catalog.db.commit()
        return record

    def commons(self, request, *, bad=False):
        if request.url.host == "commons.wikimedia.org":
            pages = []
            for title in request.url.params["titles"].split("|"):
                pages.append({"title": title, "pageid": 7, "imageinfo": [{
                    "url": "https://upload.wikimedia.org/original.jpg",
                    "thumburl": "https://thumb.wikimedia.org/thumb.png",
                    "width": 4000, "height": 3000, "size": 1000000, "mime": "image/jpeg",
                    "sha1": "original-sha1", "descriptionurl": "https://commons.wikimedia.org/wiki/" + title,
                    "extmetadata": {"LicenseShortName": {"value": "CC BY-SA"}}}]})
            return httpx.Response(200, json={"query": {"pages": pages}})
        return httpx.Response(200, content=b"<html>not an image</html>" if bad else png_bytes())

    def test_all_entity_types_ranks_and_complete_statements(self):
        item = entity(human=False)
        item["claims"]["P18"] = [statement("P18", "A_photo.jpg", "preferred"),
                                   statement("P18", "A photo.jpg"),
                                   statement("P18", "B.jpg"),
                                   statement("P18", "Deprecated.jpg", "deprecated"),
                                   {"mainsnak": {"snaktype": "novalue"}}]
        record = p18.entity_record(item, "all", {})
        self.assertEqual(record["p18_files"], ["A photo.jpg", "B.jpg"])
        self.assertEqual(len(record["p18_statements"]), 3)
        self.assertIn("qualifiers", record["p18_statements"][0])
        self.assertFalse(record["reference_verified"])
        self.assertIsNone(p18.entity_record(item, "humans", {}))

    def test_shared_file_download_task_and_missing_names(self):
        self.add(entity("Q42"))
        item = entity("Q90", human=False)
        item["labels"] = {}
        record = self.add(item)
        summary = self.catalog.summary()
        self.assertEqual((summary["entities"], summary["name_image_pairs"], summary["unique_commons_files"]), (2, 2, 1))
        self.assertEqual(record["name"], "Q90")
        self.assertTrue(record["name_is_qid_fallback"])

    def test_dump_resume_gzip_bz2_plain_without_qid_order_assumption(self):
        data = b"[\n" + b",\n".join(json.dumps(entity(qid)).encode() for qid in ("Q90", "Q42", "Q517")) + b"\n]\n"
        for suffix, compress in ((".gz", gzip.compress), (".bz2", bz2.compress), (".json", lambda x: x)):
            with self.subTest(suffix=suffix):
                path = self.root / ("dump" + suffix)
                path.write_bytes(compress(data))
                catalog = p18.Catalog(self.root / ("catalog" + suffix))
                try:
                    p18.import_dump(catalog, path, limit=1)
                    self.assertEqual(catalog.summary()["entities"], 1)
                    self.assertFalse(catalog.get("dump_progress")["complete"])
                    p18.import_dump(catalog, path)
                    self.assertEqual(catalog.summary()["entities"], 3)
                    self.assertTrue(catalog.get("dump_progress")["complete"])
                    p18.import_dump(catalog, path)
                    self.assertEqual(catalog.summary()["entities"], 3)
                finally:
                    catalog.close()

    def test_interrupted_transaction_does_not_advance_dump_checkpoint(self):
        path = self.root / "dump.json"
        path.write_text("[\n" + json.dumps(entity()) + ",\n" + json.dumps(entity("Q90")) + "\n]\n")
        original = self.catalog.add
        calls = 0

        def interrupted(record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            original(record)

        self.catalog.add = interrupted
        with self.assertRaises(KeyboardInterrupt):
            p18.import_dump(self.catalog, path)
        self.catalog.close()
        self.catalog = p18.Catalog(self.root / "out")
        p18.import_dump(self.catalog, path)
        self.assertEqual(self.catalog.summary()["entities"], 2)

    def test_truncated_or_wrong_dump_is_not_complete(self):
        path = self.root / "truncated.json"
        path.write_text("[\n" + json.dumps(entity()) + "\n")
        with self.assertRaisesRegex(ValueError, "Truncated"):
            p18.import_dump(self.catalog, path)
        self.assertFalse(self.catalog.get("dump_progress", {}).get("complete", False))
        wrong = self.root / "html.json"
        wrong.write_text("<html>error</html>")
        other = p18.Catalog(self.root / "wrong")
        try:
            with self.assertRaisesRegex(ValueError, "line-oriented"):
                p18.import_dump(other, wrong)
        finally:
            other.close()

    def test_changed_snapshot_or_scope_is_refused(self):
        path = self.root / "dump.json"
        path.write_text(json.dumps(entity()) + "\n")
        p18.import_dump(self.catalog, path)
        with self.assertRaisesRegex(ValueError, "differs"):
            p18.import_dump(self.catalog, path, scope="humans")
        path.write_text(json.dumps(entity("Q90")) + "\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            p18.import_dump(self.catalog, path)

    def test_mediawiki_maxlag_and_http_retry_after(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, headers={"Retry-After": "11"}, json={"error": {"code": "maxlag"}})
            if len(calls) == 2:
                return httpx.Response(429, headers={"Retry-After": "17"})
            return httpx.Response(200, json={"success": 1})

        network = self.network(handler)
        self.assertEqual(network.json(p18.WD_API, {}), {"success": 1})
        self.assertEqual(network.limiter.delays, [("api", 11.0), ("api", 17.0)])
        self.assertEqual(p18.retry_after("nonsense"), 0)
        self.assertEqual(p18.retry_after("-2"), 0)

    def test_normalization_redirect_and_no_big_original_fallback(self):
        def handler(request):
            return httpx.Response(200, json={"query": {
                "normalized": [{"from": "File:portrait.jpg", "to": "File:Portrait.jpg"}],
                "redirects": [{"from": "File:Portrait.jpg", "to": "File:Canonical.jpg"}],
                "pages": [{"title": "File:Canonical.jpg", "imageinfo": [{
                    "url": "https://upload.wikimedia.org/big.jpg", "width": 6000, "mime": "image/jpeg"}]}]}})
        result = p18.resolve_files(self.network(handler), [{"filename": "portrait.jpg", "file_id": "a"}], 960)
        self.assertEqual(result["a"]["commons_title"], "File:Canonical.jpg")
        self.assertIn("thumbnail_unavailable", result["a"]["error"])
        self.assertIsNone(result["a"]["download_url"])

    def test_download_resume_export_and_repair(self):
        self.add()
        self.add(entity("Q90", human=False))
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return self.commons(request)

        network = self.network(handler)
        self.assertEqual(p18.download(self.catalog, network), (1, 0))
        self.assertEqual(p18.download(self.catalog, network), (0, 0))
        self.assertEqual(calls, ["commons.wikimedia.org", "thumb.wikimedia.org"])
        p18.export(self.catalog)
        pairs = [json.loads(line) for line in (self.catalog.root / "pairs.jsonl").read_text().splitlines()]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["path"], pairs[1]["path"])
        self.assertEqual(pairs[0]["sha256"], hashlib.sha256(png_bytes()).hexdigest())
        self.assertEqual(pairs[0]["source"]["sha1"], "original-sha1")
        self.assertEqual((pairs[0]["width"], pairs[0]["height"]), (32, 24))
        self.assertFalse(pairs[0]["reference_verified"])
        path = self.catalog.root / pairs[0]["path"]
        path.write_bytes(b"x" * path.stat().st_size)
        self.assertEqual(p18.verify(self.catalog)["invalid"], 1)
        self.assertEqual(p18.verify(self.catalog, repair=True)["requeued"], 1)
        self.assertEqual(p18.download(self.catalog, network), (1, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)
        path.unlink()
        self.assertEqual(p18.download(self.catalog, network), (1, 0))
        with self.assertRaisesRegex(ValueError, "image_width differs"):
            p18.download(self.catalog, network, width=500)

    def test_corrupt_image_never_marked_downloaded_and_explicit_retry(self):
        self.add()
        network = self.network(lambda request: self.commons(request, bad=True))
        self.assertEqual(p18.download(self.catalog, network), (1, 1))
        self.assertEqual(self.catalog.summary()["file_status"], {"failed": 1})
        self.assertEqual(list(self.catalog.root.rglob("*.part")), [])
        self.assertEqual(p18.download(self.catalog, network), (0, 0))
        good = self.network(self.commons)
        self.assertEqual(p18.download(self.catalog, good, retry_failed=True), (1, 0))

    def test_thumbnail_size_guard(self):
        self.add()
        result = p18.download(self.catalog, self.network(self.commons), max_bytes=1)
        self.assertEqual(result, (1, 1))
        self.assertEqual(list(self.catalog.root.rglob("*.part")), [])

    def test_original_upload_host_remains_supported(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(200, content=png_bytes())

        result = self.network(handler).image(
            "https://upload.wikimedia.org/original.png", self.catalog.root, "original", 1024**2)
        self.assertEqual(calls, ["upload.wikimedia.org"])
        self.assertEqual(result["sha256"], hashlib.sha256(png_bytes()).hexdigest())

    def test_other_hosts_and_non_https_are_rejected_before_request(self):
        def handler(request):
            self.fail(f"Rejected URL must not be requested: {request.url}")

        network = self.network(handler)
        for url in ("http://thumb.wikimedia.org/image.png",
                    "https://thumb.wikimedia.org.example.com/image.png",
                    "https://example.com/image.png"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "Unexpected Commons image host"):
                network.image(url, self.catalog.root, "rejected", 1024**2)

    def test_retry_old_thumbnail_host_failures_preserves_completed_files(self):
        self.add(entity("Q42", "First.jpg"))
        self.add(entity("Q90", "Second.jpg", human=False))
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return self.commons(request)

        network = self.network(handler)
        self.assertEqual(p18.download(self.catalog, network, width=500, limit=1), (1, 0))
        self.catalog.db.execute("""UPDATE files SET status='failed',
            error='ValueError: Unexpected Commons image host: thumb.wikimedia.org'
            WHERE status='pending'""")
        self.catalog.db.commit()
        calls.clear()
        self.assertEqual(p18.download(self.catalog, network, width=500, retry_failed=True), (1, 0))
        self.assertEqual(calls, ["commons.wikimedia.org", "thumb.wikimedia.org"])
        self.assertEqual(self.catalog.summary()["file_status"], {"downloaded": 2})
        self.assertEqual(p18.verify(self.catalog)["verified"], 2)

    def test_sample_persists_id_selection_and_skips_network_on_resume(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "query.wikidata.org":
                return httpx.Response(200, json={"results": {"bindings": [
                    {"item": {"value": "http://www.wikidata.org/entity/Q90"}}]}})
            return httpx.Response(200, json={"entities": {"Q90": entity("Q90", human=False)}})

        network = self.network(handler)
        p18.sample(self.catalog, network, limit=1)
        p18.sample(self.catalog, network, limit=1)
        self.assertEqual(calls, ["query.wikidata.org", "www.wikidata.org"])
        self.assertTrue(self.catalog.get("sample_complete"))
        self.assertEqual(self.catalog.summary()["entities"], 1)

    def test_missing_api_batch_is_not_silently_completed(self):
        network = self.network(lambda request: httpx.Response(200, json={"entities": {}}))
        with self.assertRaisesRegex(ValueError, "omitted"):
            p18.sample(self.catalog, network, qids=["Q42"])
        self.assertFalse(self.catalog.get("sample_complete", False))
        self.assertEqual(self.catalog.get("sample_offset", 0), 0)

    def test_reader_works_while_writer_is_locked(self):
        self.add()
        with p18.output_lock(self.catalog.root):
            reader = p18.Catalog(self.catalog.root, readonly=True)
            try:
                self.assertEqual(reader.summary()["entities"], 1)
            finally:
                reader.close()
            with self.assertRaisesRegex(ValueError, "Another process"):
                with p18.output_lock(self.catalog.root):
                    self.fail("Second writer lock should fail")


if __name__ == "__main__":
    unittest.main()
