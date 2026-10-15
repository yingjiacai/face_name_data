"""Verify archive fallback and pipeline isolation with offline HTTP fixtures."""

from concurrent.futures import CancelledError
import errno
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

import httpx

from test_download_p18 import entity, FakeLimiter, p18, png_bytes


def original_info(filename="Portrait.png", **changes):
    body = png_bytes()
    url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/" + quote(filename.replace(" ", "_"))
    info = {"url": url, "download_url": url, "commons_title": "File:" + filename,
            "width": 32, "height": 24, "mime": "image/png", "size": len(body),
            "sha1": hashlib.sha1(body).hexdigest(), "timestamp": "2010-05-06T12:34:56Z",
            "requested_width": 500}
    info.update(changes)
    return info


def commons(request):
    if request.url.host == "commons.wikimedia.org":
        pages = []
        for title in request.url.params["titles"].split("|"):
            info = original_info(title.removeprefix("File:"))
            if title.startswith("File:Thumb"):
                info.update(width=1600, thumburl="https://thumb.wikimedia.org/" +
                            hashlib.sha256(title.encode()).hexdigest() + ".png")
            pages.append({"title": title, "imageinfo": [info]})
        return httpx.Response(200, json={"query": {"pages": pages}})
    return httpx.Response(200, content=png_bytes())


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = p18.Catalog(self.root / "out")
        self.networks = []
        self.file_id = hashlib.sha256(b"archive fixture").hexdigest()

    def tearDown(self):
        for network in self.networks:
            network.close()
        self.catalog.close()
        self.temp.cleanup()

    def network(self, archive_handler=None, commons_handler=commons):
        if archive_handler is None:
            archive_handler = lambda request: httpx.Response(200, content=png_bytes())
        network = p18.Network("test/1 (offline)", retries=0, limiter=FakeLimiter(),
                              client=httpx.Client(transport=httpx.MockTransport(commons_handler)),
                              archive_client=httpx.Client(transport=httpx.MockTransport(archive_handler)))
        self.networks.append(network)
        return network

    def add(self, originals=1, thumbnails=0):
        filenames = ([f"Original {n}.png" for n in range(originals)] +
                     [f"Thumb {n}.png" for n in range(thumbnails)])
        for n, filename in enumerate(filenames, 1):
            self.catalog.add(p18.entity_record(entity(f"Q{n}", filename), "all", {}))
        self.catalog.db.commit()

    def archive_image(self, network, info=None, max_bytes=1024):
        return network.archive_image(info or original_info(), self.root / "direct", self.file_id, max_bytes)

    def test_urls_preserve_original_filename_and_archive_item_case(self):
        info = original_info("Johann Döderlein (colour).png")
        urls = self.network().archive_urls(info)
        suffix = "/2010-05-06.zip/2010%2F05%2F06%2FJohann_D%C3%B6derlein_%28colour%29.png"
        self.assertEqual(urls, ["https://archive.org/download/Wikimediacommons-201005" + suffix,
                                "https://archive.org/download/wikimediacommons-2010-05-06" + suffix])

    def test_ineligible_metadata_never_contacts_archive(self):
        network = self.network(lambda request: self.fail(f"Ineligible image requested {request.url}"))
        for changes in ({"width": 501}, {"width": 0}, {"timestamp": "2018-01-01T00:00:00Z"},
                        {"timestamp": "invalid"}, {"sha1": "bad"}, {"size": 0},
                        {"download_url": "https://thumb.wikimedia.org/thumb.png"},
                        {"download_url": "https://upload.wikimedia.org/another-original.png"}):
            with self.subTest(changes=changes):
                info = original_info(**changes)
                self.assertEqual(network.archive_urls(info), [])
                self.assertIsNone(self.archive_image(network, info))
        self.assertEqual(network.archive_urls(original_info("x" * 101 + ".png")), [])

    def test_exact_sha1_and_size_produce_normal_image_result(self):
        result = self.archive_image(self.network())
        self.assertIsNotNone(result)
        self.assertEqual((self.root / "direct" / result["path"]).read_bytes(), png_bytes())
        self.assertEqual(result["sha256"], hashlib.sha256(png_bytes()).hexdigest())
        self.assertEqual((result["nbytes"], result["width"], result["height"]), (len(png_bytes()), 32, 24))
        self.assertEqual(list((self.root / "direct").rglob("*.part")), [])

    def test_hash_size_and_byte_budget_failures_leave_no_staged_image(self):
        for changes, budget in (({"sha1": "0" * 40}, 1024),
                                ({"size": len(png_bytes()) + 1}, 1024),
                                ({"size": len(png_bytes()) - 1}, 1024),
                                ({}, len(png_bytes()) - 1)):
            with self.subTest(changes=changes, budget=budget):
                self.assertIsNone(self.archive_image(self.network(), original_info(**changes), budget))
                self.assertEqual([p for p in (self.root / "direct").rglob("*") if p.is_file()], [])

    def test_monthly_404_falls_back_once_to_daily_item(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(404) if len(requests) == 1 else httpx.Response(200, content=png_bytes())

        network = self.network(handler)
        self.assertIsNotNone(self.archive_image(network))
        self.assertEqual(requests, network.archive_urls(original_info()))

    def test_missing_unavailable_and_network_errors_are_mirror_misses(self):
        for status in (404, 503):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(str(request.url))
                    return httpx.Response(status)

                self.assertIsNone(self.archive_image(self.network(handler)))
                self.assertEqual(len(calls), 2 if status == 404 else 1)

        def disconnected(request):
            raise httpx.ReadError("mirror disconnected", request=request)

        self.assertIsNone(self.archive_image(self.network(disconnected)))
        self.assertEqual(list((self.root / "direct").rglob("*.part")), [])

    def test_cancellation_and_local_disk_failure_are_not_mirror_misses(self):
        def cancelled(request):
            raise CancelledError()

        with self.assertRaises(CancelledError):
            self.archive_image(self.network(cancelled))
        with mock.patch.object(p18.os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError) as caught:
                self.archive_image(self.network())
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(list((self.root / "direct").rglob("*.part")), [])

    def test_archive_403_and_429_disable_further_mirror_requests(self):
        for status in (403, 429):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(str(request.url))
                    return httpx.Response(status, headers={"Retry-After": "30"})

                network = self.network(handler)
                self.assertIsNone(self.archive_image(network))
                self.assertIsNone(self.archive_image(network, original_info("Another.png")))
                self.assertEqual(len(calls), 1, "A blocked mirror must stop receiving new requests")

    def test_hash_mismatch_falls_back_to_commons_without_final_failure(self):
        self.add()
        media = []
        archive_calls = []

        def handler(request):
            if request.url.host != "commons.wikimedia.org":
                media.append(str(request.url))
            return commons(request)

        def mismatch(request):
            archive_calls.append(str(request.url))
            body = png_bytes()
            return httpx.Response(200, content=body[:-1] + bytes([body[-1] ^ 1]))

        self.assertEqual(p18.download(self.catalog, self.network(mismatch, handler), width=500), (1, 0))
        self.assertEqual(len(archive_calls), 1)
        self.assertEqual(len(media), 1)
        self.assertEqual(self.catalog.summary()["file_status"], {"downloaded": 1})
        self.assertEqual(self.catalog.db.execute("SELECT attempts FROM files").fetchone()[0], 1)
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)

    def test_slow_failing_mirrors_allow_commons_progress_and_late_fallback(self):
        self.add(originals=3, thumbnails=2)
        thumbnails_done = threading.Event()
        lock = threading.Lock()
        thumbnail_count = original_count = 0

        def handler(request):
            nonlocal thumbnail_count, original_count
            with lock:
                if request.url.host == "thumb.wikimedia.org":
                    thumbnail_count += 1
                    if thumbnail_count == 2:
                        thumbnails_done.set()
                elif request.url.host == "upload.wikimedia.org":
                    original_count += 1
            return commons(request)

        def slow_miss(request):
            self.assertTrue(thumbnails_done.wait(5), "Mirror workers blocked Commons thumbnails")
            return httpx.Response(503)

        network = self.network(slow_miss, handler)
        self.assertEqual(p18.download(self.catalog, network, width=500, max_failures=1), (5, 0))
        self.assertEqual((thumbnail_count, original_count), (2, 3))
        self.assertEqual(self.catalog.summary()["file_status"], {"downloaded": 5})
        self.assertEqual(p18.verify(self.catalog)["verified"], 5)
        self.assertEqual(list((self.catalog.root / "staging").glob("*.json")), [])

    def test_archive_and_commons_have_independent_bounded_worker_pools(self):
        self.add(originals=14, thumbnails=4)
        full = threading.Event()
        lock = threading.Lock()
        active = {"archive": 0, "commons": 0}
        peak = {"archive": 0, "commons": 0}

        def media(group):
            with lock:
                active[group] += 1
                peak[group] = max(peak[group], active[group])
                if active["archive"] == 10 and active["commons"] == 2:
                    full.set()
            try:
                self.assertTrue(full.wait(5), "Archive and Commons did not run independently")
                return httpx.Response(200, content=png_bytes())
            finally:
                with lock:
                    active[group] -= 1

        def handler(request):
            return commons(request) if request.url.host == "commons.wikimedia.org" else media("commons")

        network = self.network(lambda request: media("archive"), handler)
        self.assertEqual(p18.download(self.catalog, network, width=500), (18, 0))
        self.assertEqual(peak, {"archive": 10, "commons": 2})
        self.assertEqual(p18.verify(self.catalog)["verified"], 18)

    def test_zero_archive_workers_uses_only_commons(self):
        self.add(originals=2, thumbnails=1)
        network = self.network(lambda request: self.fail(f"Archive was disabled: {request.url}"))
        self.assertEqual(p18.download(self.catalog, network, width=500, archive_workers=0), (3, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 3)

    def test_interrupted_archive_storage_resumes_without_either_media_service(self):
        self.add()
        with mock.patch.object(p18, "store_image", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                p18.download(self.catalog, self.network(), width=500)
        self.assertEqual(self.catalog.summary()["file_status"], {"pending": 1})
        self.assertEqual(len(list((self.catalog.root / "staging").glob("*.json"))), 1)
        self.catalog.close()
        self.catalog = p18.Catalog(self.root / "out")

        def offline(request):
            self.fail(f"Verified staged archive image was requested again: {request.url}")

        self.assertEqual(p18.download(self.catalog, self.network(offline, offline), width=500), (1, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)
        self.assertEqual(list((self.catalog.root / "staging").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
