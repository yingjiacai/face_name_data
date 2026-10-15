"""Exercise concurrency and crash recovery without using Wikimedia or a bucket."""

from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from test_download_p18 import entity, FakeLimiter, p18, png_bytes


def commons(request):
    if request.url.host == "commons.wikimedia.org":
        return httpx.Response(200, json={"query": {"pages": [
            {"title": title, "imageinfo": [{
                "url": "https://upload.wikimedia.org/original.png",
                "thumburl": "https://thumb.wikimedia.org/" + hashlib.sha256(title.encode()).hexdigest() + ".png",
                "width": 1600, "mime": "image/png"}]}
            for title in request.url.params["titles"].split("|")]}})
    return httpx.Response(200, content=png_bytes())


class PipelineTests(unittest.TestCase):
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

    def network(self, handler=commons):
        network = p18.Network("test/1 (offline)", limiter=FakeLimiter(),
                              client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.networks.append(network)
        return network

    def add(self, count):
        for n in range(1, count + 1):
            self.catalog.add(p18.entity_record(entity(f"Q{n}", f"Image {n}.jpg"), "all", {}))
        self.catalog.db.commit()

    def reopen(self):
        self.catalog.close()
        self.catalog = p18.Catalog(self.root / "out")

    def test_slow_image_does_not_hold_up_later_batches(self):
        self.add(12)
        eighth = threading.Event()
        lock = threading.Lock()
        active = peak = calls = 0
        batches = []

        def handler(request):
            nonlocal active, peak, calls
            if request.url.host == "commons.wikimedia.org":
                batches.append(len(request.url.params["titles"].split("|")))
                return commons(request)
            with lock:
                active += 1
                peak = max(peak, active)
                calls += 1
                number = calls
            try:
                if number == 1:
                    self.assertTrue(eighth.wait(5), "A slow image blocked later batches")
                if number == 8:
                    eighth.set()
                return commons(request)
            finally:
                with lock:
                    active -= 1

        self.assertEqual(p18.download(self.catalog, self.network(handler), metadata_batch_size=5), (12, 0))
        self.assertEqual(peak, 2)
        self.assertEqual(batches, [5, 5, 2])
        self.assertEqual(p18.verify(self.catalog)["verified"], 12)

    def test_lightweight_batches_prefetch_200_links_while_upload_waits(self):
        self.add(220)
        prefetched = threading.Event()
        batches = []
        original = p18.store_image

        def handler(request):
            if request.url.host == "commons.wikimedia.org":
                self.assertNotIn("extmetadata", request.url.params["iiprop"])
                self.assertNotIn("iiextmetadatafilter", request.url.params)
                batches.append(len(request.url.params["titles"].split("|")))
                if sum(batches) >= 200:
                    prefetched.set()
            return commons(request)

        def store(*args):
            self.assertTrue(prefetched.wait(5), "Link lookahead was limited by the upload queue")
            return original(*args)

        with mock.patch.object(p18, "store_image", store):
            self.assertEqual(p18.download(self.catalog, self.network(handler), upload_workers=1), (220, 0))
        self.assertEqual(batches, [50, 50, 50, 50, 20])
        self.assertEqual(p18.verify(self.catalog)["verified"], 220)

    def test_full_metadata_remains_opt_in_and_uses_small_batches(self):
        self.add(12)
        batches = []

        def handler(request):
            if request.url.host == "commons.wikimedia.org":
                self.assertIn("extmetadata", request.url.params["iiprop"])
                batches.append(len(request.url.params["titles"].split("|")))
            return commons(request)

        self.assertEqual(p18.download(self.catalog, self.network(handler), full_metadata=True), (12, 0))
        self.assertEqual(batches, [5, 5, 2])

    def test_retry_preserves_previous_attribution_without_requesting_it_again(self):
        self.add(1)
        previous = {"error": "thumbnail_unavailable", "resolved_at": "earlier-snapshot",
                    "sha1": "old-source-hash", "extmetadata": {"Artist": {"value": "Original artist"}}}
        self.catalog.db.execute("UPDATE files SET status='failed',info=?", (p18.encode(previous),))
        self.catalog.db.commit()
        self.assertEqual(p18.download(self.catalog, self.network(), retry_failed=True), (1, 0))
        p18.export(self.catalog)
        record = json.loads((self.catalog.root / "pairs.jsonl").read_text())
        self.assertEqual(record["source"]["previous_extmetadata"]["value"], previous["extmetadata"])
        self.assertEqual(record["source"]["previous_extmetadata"]["resolved_at"], "earlier-snapshot")
        self.assertEqual(record["source"]["metadata_profile"], "basic")

    def test_more_than_32_uploads_can_run_without_reserving_maximum_size_each(self):
        self.add(40)
        all_started = threading.Event()
        lock = threading.Lock()
        active = peak = 0
        original_store, original_reserve = p18.store_image, p18.Staging.reserve

        def reserve(staging, file_id):
            allowed = original_reserve(staging, file_id)
            self.assertLessEqual(staging.used, 2 * 1024**2)
            return allowed

        def store(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 40:
                    all_started.set()
            try:
                self.assertTrue(all_started.wait(10), "Upload concurrency was capped at 32 or maximum-size slots")
                return original_store(*args)
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(p18.Staging, "reserve", reserve), mock.patch.object(p18, "store_image", store):
            self.assertEqual(p18.download(self.catalog, self.network(), upload_workers=40,
                                          max_bytes=1024**2, staging_bytes=2 * 1024**2), (40, 0))
        self.assertEqual(peak, 40)
        self.assertEqual(p18.verify(self.catalog)["verified"], 40)

    def test_storage_upload_does_not_occupy_media_connections(self):
        self.add(12)
        storing, eighth = threading.Event(), threading.Event()
        calls = 0
        lock = threading.Lock()
        original = p18.store_image

        def handler(request):
            nonlocal calls
            if request.url.host == "thumb.wikimedia.org":
                with lock:
                    calls += 1
                    number = calls
                if number == 4:
                    self.assertTrue(storing.wait(5))
                if number == 8:
                    eighth.set()
            return commons(request)

        def slow_store(*args):
            storing.set()
            self.assertTrue(eighth.wait(5), "Slow storage blocked the download stage")
            return original(*args)

        with mock.patch.object(p18, "store_image", slow_store):
            self.assertEqual(p18.download(self.catalog, self.network(handler), upload_workers=1), (12, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 12)

    def test_local_budget_applies_while_storage_is_blocked(self):
        self.add(10)
        second = threading.Event()
        calls = 0
        lock = threading.Lock()
        original_store, original_reserve = p18.store_image, p18.Staging.reserve
        high_water = []

        def handler(request):
            nonlocal calls
            if request.url.host == "thumb.wikimedia.org":
                with lock:
                    calls += 1
                    if calls == 2:
                        second.set()
            return commons(request)

        def reserve(spool, file_id):
            allowed = original_reserve(spool, file_id)
            high_water.append(spool.used)
            self.assertLessEqual(spool.used, 512)
            return allowed

        def slow_store(*args):
            self.assertTrue(second.wait(5))
            return original_store(*args)

        with mock.patch.object(p18.Staging, "reserve", reserve), mock.patch.object(p18, "store_image", slow_store):
            self.assertEqual(p18.download(self.catalog, self.network(handler), max_bytes=256,
                                          staging_bytes=512, upload_workers=1), (10, 0))
        self.assertEqual(max(high_water), 512)
        self.assertEqual(list((self.catalog.root / "staging").glob("*.json")), [])

    def test_failed_upload_is_retained_and_retried_after_restart_without_http(self):
        self.add(1)

        def broken_store(staging, output, result):
            target = output / result["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"partial upload")
            raise OSError(errno.EIO, "simulated storage failure")

        with mock.patch.object(p18, "store_image", broken_store):
            self.assertEqual(p18.download(self.catalog, self.network()), (1, 1))
        self.assertEqual(self.catalog.summary()["file_status"], {"failed": 1})
        self.assertEqual(len(list((self.catalog.root / "staging").glob("*.json"))), 1)
        self.reopen()
        offline = self.network(lambda request: self.fail(f"Staged retry made an HTTP request: {request.url}"))
        self.assertEqual(p18.download(self.catalog, offline), (0, 0))
        self.assertEqual(p18.download(self.catalog, offline, retry_failed=True), (1, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)
        self.assertEqual(list((self.catalog.root / "staging").glob("*.json")), [])

    def test_corrupt_staged_bytes_are_refetched(self):
        self.add(1)
        with mock.patch.object(p18, "store_image", side_effect=OSError(errno.EIO, "offline storage")):
            self.assertEqual(p18.download(self.catalog, self.network()), (1, 1))
        path = next((self.catalog.root / "staging").glob("images/*/*.png"))
        path.write_bytes(b"x" * path.stat().st_size)
        self.reopen()
        calls = []

        def handler(request):
            calls.append(request.url.host)
            return commons(request)

        self.assertEqual(p18.download(self.catalog, self.network(handler), retry_failed=True), (1, 0))
        self.assertEqual(calls, ["thumb.wikimedia.org"])
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)

    def test_full_spool_preserves_failed_upload_and_drains_it_first_on_resume(self):
        self.add(3)
        budget = len(png_bytes()) + 1
        media_calls = []

        def handler(request):
            if request.url.host == "thumb.wikimedia.org":
                media_calls.append(str(request.url))
            return commons(request)

        with mock.patch.object(p18, "store_image", side_effect=OSError(errno.EIO, "storage offline")):
            with self.assertRaisesRegex(ValueError, "staging is full"):
                p18.download(self.catalog, self.network(handler), max_bytes=budget, staging_bytes=budget)
        self.assertEqual(len(media_calls), 1)
        self.assertEqual(len(list((self.catalog.root / "staging").glob("*.json"))), 1)
        self.reopen()
        self.assertEqual(p18.download(self.catalog, self.network(handler), retry_failed=True,
                                      max_bytes=budget, staging_bytes=budget), (3, 0))
        self.assertEqual(len(media_calls), 3, "The retained image must not be fetched twice")
        self.assertEqual(p18.verify(self.catalog)["verified"], 3)

    def test_interrupt_keeps_staged_image_uncommitted_and_resumable(self):
        self.add(1)
        with mock.patch.object(p18, "store_image", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                p18.download(self.catalog, self.network())
        self.assertEqual(self.catalog.summary()["file_status"], {"pending": 1})
        self.assertEqual(len(list((self.catalog.root / "staging").glob("*.json"))), 1)
        self.reopen()
        offline = self.network(lambda request: self.fail(f"Interrupted upload was fetched again: {request.url}"))
        self.assertEqual(p18.download(self.catalog, offline), (1, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)

    def test_api_batch_failure_can_resume_all_unfinished_work(self):
        self.add(12)
        calls = 0

        def handler(request):
            nonlocal calls
            if request.url.host == "commons.wikimedia.org":
                calls += 1
                if calls == 2:
                    return httpx.Response(200, json={"error": {"code": "fixture_error"}})
            return commons(request)

        with self.assertRaisesRegex(ValueError, "fixture_error"):
            p18.download(self.catalog, self.network(handler), metadata_batch_size=5)
        self.assertNotIn("failed", self.catalog.summary()["file_status"])
        self.reopen()
        p18.download(self.catalog, self.network())
        self.assertEqual(p18.verify(self.catalog)["verified"], 12)

    def test_missing_files_do_not_stop_the_rest_of_the_catalog(self):
        self.add(14)

        def handler(request):
            response = commons(request)
            if request.url.host == "commons.wikimedia.org":
                data = response.json()
                for page in data["query"]["pages"]:
                    if page["title"] != "File:Image 14.jpg":
                        page.pop("imageinfo")
                return httpx.Response(200, json=data)
            return response

        self.assertEqual(p18.download(self.catalog, self.network(handler), max_failures=1), (14, 13))
        self.assertEqual(self.catalog.summary()["file_status"], {"downloaded": 1, "failed": 13})

    def test_bad_images_and_http_404_do_not_count_as_systemic_failures(self):
        self.add(12)
        calls = 0

        def handler(request):
            nonlocal calls
            if request.url.host == "commons.wikimedia.org":
                return commons(request)
            calls += 1
            return httpx.Response(404) if calls % 2 else httpx.Response(200, content=b"not an image")

        self.assertEqual(p18.download(self.catalog, self.network(handler), max_failures=1), (12, 12))
        self.assertEqual(self.catalog.summary()["file_status"], {"failed": 12})

    def test_repeated_network_failures_still_stop_new_work(self):
        self.add(12)

        def handler(request):
            if request.url.host == "commons.wikimedia.org":
                return commons(request)
            raise httpx.ConnectError("network offline", request=request)

        with self.assertRaisesRegex(ValueError, "consecutive failures"):
            p18.download(self.catalog, self.network(handler), max_failures=1)
        self.assertGreaterEqual(self.catalog.summary()["file_status"].get("failed", 0), 1)

    def test_fast_resume_uses_db_and_explicit_verify_can_requeue(self):
        self.add(1)
        self.assertEqual(p18.download(self.catalog, self.network()), (1, 0))
        row = self.catalog.db.execute("SELECT path FROM files").fetchone()
        target = self.catalog.root / row["path"]
        target.unlink()
        offline = self.network(lambda request: self.fail(f"Completed entry was fetched: {request.url}"))
        original = Path.stat

        def stat(path, *args, **kwargs):
            self.assertNotEqual(path, target, "Fast resume must not stat remote images")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(p18.download(self.catalog, offline, skip_existing_check=True), (0, 0))
        self.assertEqual(p18.verify(self.catalog, repair=True)["requeued"], 1)
        self.assertEqual(p18.download(self.catalog, self.network(), skip_existing_check=True), (1, 0))

    def test_disk_full_during_repair_cannot_leave_downloaded_status(self):
        self.add(1)
        self.assertEqual(p18.download(self.catalog, self.network()), (1, 0))
        row = self.catalog.db.execute("SELECT path FROM files").fetchone()
        (self.catalog.root / row["path"]).write_bytes(b"truncated")
        with mock.patch.object(p18, "store_image", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                p18.download(self.catalog, self.network())
        self.assertEqual(self.catalog.summary()["file_status"], {"pending": 1})
        self.reopen()
        offline = self.network(lambda request: self.fail(f"Disk-full recovery refetched: {request.url}"))
        self.assertEqual(p18.download(self.catalog, offline, skip_existing_check=True), (1, 0))
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)

    def test_success_is_committed_while_other_jobs_stop_after_failures(self):
        self.add(12)
        original = p18.store_image
        first_storing = threading.Event()
        calls = 0
        lock = threading.Lock()

        def store(*args):
            nonlocal calls
            with lock:
                calls += 1
                number = calls
            first_storing.set()
            if number == 1:
                return original(*args)
            raise OSError(errno.EIO, "storage unavailable")

        with mock.patch.object(p18, "store_image", store):
            with self.assertRaisesRegex(ValueError, "consecutive failures"):
                p18.download(self.catalog, self.network(), max_failures=1)
        self.assertTrue(first_storing.is_set())
        self.assertGreaterEqual(self.catalog.summary()["file_status"].get("downloaded", 0), 1)
        self.reopen()
        p18.download(self.catalog, self.network(), retry_failed=True)
        self.assertEqual(p18.verify(self.catalog)["verified"], 12)

    def test_limit_includes_all_pipeline_stages(self):
        self.add(12)
        self.assertEqual(p18.download(self.catalog, self.network(), limit=7), (7, 0))
        self.assertEqual(self.catalog.summary()["file_status"], {"downloaded": 7, "pending": 5})

    def test_transient_storage_error_retries_local_copy(self):
        self.add(1)
        original = Path.open
        attempts = 0

        def flaky_open(path, *args, **kwargs):
            nonlocal attempts
            if path.is_relative_to(self.catalog.root / "images") and args and args[0] == "wb":
                attempts += 1
                if attempts < 3:
                    raise OSError(errno.EIO, "temporary upload error")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", flaky_open), mock.patch.object(p18.time, "sleep"):
            self.assertEqual(p18.download(self.catalog, self.network()), (1, 0))
        self.assertEqual(attempts, 3)
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)

    def test_symlink_storage_only_receives_final_names_and_successful_status(self):
        self.add(1)
        remote = self.root / "mounted_bucket"
        remote.mkdir()
        (self.catalog.root / "images").symlink_to(remote, target_is_directory=True)
        original = Path.open
        storage_opens = []

        def track(path, *args, **kwargs):
            if path.is_relative_to(self.catalog.root / "images"):
                storage_opens.append((str(path), args[0] if args else "r"))
                self.assertEqual(path.suffix, ".png")
                self.assertEqual(args[0], "wb", "Validation must read the local spool")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", track):
            self.assertEqual(p18.download(self.catalog, self.network()), (1, 0))
        self.assertEqual(len(storage_opens), 1)
        self.assertEqual(len(list(remote.glob("*/*.png"))), 1)
        self.assertEqual(p18.verify(self.catalog)["verified"], 1)


class LimiterTests(unittest.TestCase):
    def test_image_429_shares_cooldown_without_permanently_reducing_rate(self):
        clock = [100.0]
        delays = []
        event = mock.Mock()
        event.is_set.return_value = False

        def pause(seconds):
            delays.append(seconds)
            clock[0] += seconds
            return False

        event.wait.side_effect = pause
        limiter = p18.RateLimiter(cancel=event)
        with mock.patch.object(p18.time, "monotonic", lambda: clock[0]):
            limiter.wait("image")
            limiter.wait("image")
            self.assertEqual([round(delay, 2) for delay in delays], [0.1])
            limiter.wait("api")
            limiter.wait("api")
            first = limiter.rate_limited("image", 17)
            duplicate = limiter.rate_limited("image", 17)
            limiter.wait("image")
            limiter.wait("image")
            second = limiter.rate_limited("image", 11)
            limiter.wait("image")
            limiter.wait("image")
        self.assertEqual(first, (0.1, 1, 1, 17.0, True))
        self.assertEqual(duplicate, (0.1, 2, 1, 17.0, False))
        self.assertEqual(second, (0.1, 3, 2, 28.0, True))
        self.assertEqual(limiter.snapshot("image"), {
            "interval": 0.1, "responses": 3, "events": 2, "cooldown_seconds": 28.0})
        self.assertEqual([round(delay, 2) for delay in delays], [0.1, 1, 17, 0.1, 11, 0.1])

    def test_concurrent_media_connections_share_rolling_ten_second_budget(self):
        delays = []
        clock = [100.0]
        clock_lock = threading.Lock()

        def now():
            with clock_lock:
                return clock[0]

        def sleep(delay):
            with clock_lock:
                delays.append(delay)
                clock[0] += delay

        # 8 Mbps permits 10,000,000 bytes in any rolling ten-second window.
        limiter = p18.BandwidthLimiter(8, clock=now, sleep=sleep)
        with ThreadPoolExecutor(max_workers=2) as pool:
            waits = list(pool.map(limiter.consume, [500_000, 500_000]))
        self.assertEqual(waits, [0.0, 0.0])
        self.assertEqual(delays, [])
        self.assertEqual(limiter.consume(9_000_000), 0.0)
        self.assertEqual(limiter.consume(1), 10.0)
        self.assertEqual(delays, [10.0])


if __name__ == "__main__":
    unittest.main()
