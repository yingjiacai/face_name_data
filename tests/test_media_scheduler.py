"""Check Commons request admission without network calls or real sleeps."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from test_download_p18 import entity, p18, png_bytes


UPLOAD = "https://upload.wikimedia.org/wikipedia/commons/a/a0/Portrait.jpg"
THUMB = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a0/Portrait.jpg/500px-Portrait.jpg"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class MediaSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = p18.RateLimiter(image_interval=0, clock=self.clock)

    def complete(self, count, cache="miss", status=200):
        for _ in range(count):
            ticket = self.limiter.reserve_image(UPLOAD)
            self.assertIsNotNone(ticket)
            self.limiter.finish_image(ticket, status=status, cache=cache)

    def test_miss_budget_uses_whole_tokens_and_discards_fractional_remainder(self):
        self.complete(8)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.clock.advance(1.5 / 0.95)
        self.complete(1)
        self.clock.advance(0.5 / 0.95)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.assertGreater(self.limiter.image_delay(UPLOAD), 0.5)
        self.clock.advance(self.limiter.image_delay(UPLOAD))
        self.complete(1)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_wait_estimates_do_not_consume_or_accumulate_credit(self):
        self.complete(8)
        for _ in range(10):
            self.clock.advance(0.1)
            self.assertGreater(self.limiter.image_delay(UPLOAD), 0)
            self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.clock.advance(self.limiter.image_delay(UPLOAD))
        self.complete(1)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_response_time_is_excluded_from_future_refill(self):
        self.complete(7)
        ticket = self.limiter.reserve_image(UPLOAD)
        self.clock.advance(10)
        self.limiter.finish_image(ticket, status=200, cache="miss")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.assertGreater(self.limiter.image_delay(UPLOAD), 1.05)

    def test_reserved_worker_obeys_cooldown_installed_before_http_starts(self):
        ticket = self.limiter.reserve_image(UPLOAD)
        self.limiter.rate_limited("image", 7)

        def handler(request):
            self.assertGreaterEqual(self.clock(), 7)
            return httpx.Response(200, content=png_bytes(), headers={"X-Cache-Status": "miss"})

        network = p18.Network("test/1 (offline)", limiter=self.limiter,
                              client=httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                    network.cancel, "wait", side_effect=lambda delay: self.clock.advance(delay)):
                network.image(UPLOAD, Path(directory), "a" * 64, 1024, ticket=ticket)
            self.assertEqual(self.limiter._image_buckets["miss"]["tokens"], 7)
            self.assertEqual(self.limiter._image_tickets, {})
        finally:
            network.close()

    def test_front_hit_does_not_consume_or_reset_miss_credit(self):
        self.complete(8)
        self.clock.advance(self.limiter.image_delay(UPLOAD))
        self.complete(5, cache="hit-front")
        self.complete(1, cache="miss")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_local_hit_uses_miss_bucket_and_preserves_hit_credit(self):
        self.complete(24, cache="hit-front")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.clock.advance(self.limiter.image_delay(UPLOAD))
        self.complete(8, cache="hit-local")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_unknown_responses_and_errors_charge_both_buckets(self):
        for status, cache in ((None, None), (200, "unknown"),
                              (429, "int-front"), (503, None)):
            with self.subTest(status=status, cache=cache):
                self.setUp()
                self.complete(8, status=status, cache=cache)
                self.assertIsNone(self.limiter.reserve_image(UPLOAD))
                self.clock.advance(self.limiter.image_delay(UPLOAD))
                # Eight unknown requests used eight HIT tokens. The wait for
                # one MISS token replenishes three, leaving nineteen HITs.
                self.complete(19, cache="hit-front")
                self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_duplicate_completion_cannot_charge_another_request(self):
        self.complete(7)
        first = self.limiter.reserve_image(UPLOAD)
        self.limiter.finish_image(first, status=200, cache="hit-front")
        second = self.limiter.reserve_image(UPLOAD)
        self.limiter.finish_image(first, status=200, cache="miss")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.limiter.finish_image(second, unused=True)
        self.complete(1)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_unsent_cancellation_releases_reservation_without_debit(self):
        for _ in range(30):
            ticket = self.limiter.reserve_image(UPLOAD)
            self.assertIsNotNone(ticket)
            self.limiter.finish_image(ticket, unused=True)
        ticket = self.limiter.reserve_image(UPLOAD)
        self.limiter.cancel.set()
        self.limiter.finish_image(ticket, unused=True)
        with self.assertRaises(CancelledError):
            self.limiter.reserve_image(UPLOAD)
        with self.assertRaises(CancelledError):
            self.limiter.image_delay(UPLOAD)
        self.limiter.cancel.clear()
        self.complete(8)
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))

    def test_concurrent_originals_have_only_one_unconfirmed_reservation(self):
        barrier = threading.Barrier(8)

        def reserve(_):
            barrier.wait(timeout=5)
            return self.limiter.reserve_image(UPLOAD)

        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(reserve, range(8)))
        admitted = [ticket for ticket in tickets if ticket is not None]
        self.assertEqual(len(admitted), 1)
        self.assertGreater(self.limiter.image_delay(UPLOAD), 0)
        thumbnail = self.limiter.reserve_image(THUMB)
        self.assertIsNotNone(thumbnail)
        self.limiter.finish_image(thumbnail, status=200, cache="miss")
        self.limiter.finish_image(admitted[0], status=200, cache="miss")
        following = self.limiter.reserve_image(UPLOAD)
        self.assertIsNotNone(following)
        self.limiter.finish_image(following, unused=True)

    def test_global_gap_is_shared_while_thumbnail_bypasses_original_wait(self):
        self.limiter = p18.RateLimiter(clock=self.clock)
        original = self.limiter.reserve_image(UPLOAD)
        self.assertIsNone(self.limiter.reserve_image(THUMB))
        self.assertAlmostEqual(self.limiter.image_delay(THUMB), 0.1)
        self.clock.advance(0.1)
        thumbnail = self.limiter.reserve_image(THUMB)
        self.assertIsNotNone(thumbnail)
        self.limiter.finish_image(thumbnail, status=200, cache="miss")
        self.limiter.finish_image(original, status=200, cache="hit-front")
        self.assertIsNone(self.limiter.reserve_image(UPLOAD))
        self.clock.advance(0.1)
        following = self.limiter.reserve_image(UPLOAD)
        self.assertIsNotNone(following)
        self.limiter.finish_image(following, unused=True)

    def test_retry_after_and_pause_block_both_media_hosts(self):
        original = self.limiter.reserve_image(UPLOAD)
        self.limiter.rate_limited("image", 11)
        self.limiter.finish_image(original, status=429, cache="int-front")
        self.limiter.wait("api")
        for url in (UPLOAD, THUMB):
            self.assertIsNone(self.limiter.reserve_image(url))
            self.assertEqual(self.limiter.image_delay(url), 11)
        self.clock.advance(11)
        for url in (UPLOAD, THUMB):
            ticket = self.limiter.reserve_image(url)
            self.assertIsNotNone(ticket)
            self.limiter.finish_image(ticket, unused=True)
        self.limiter.pause("image", 3)
        self.assertEqual(self.limiter.image_delay(THUMB), 3)
        self.assertEqual(self.limiter.image_delay(UPLOAD), 3)


class SchedulerIntegrationTests(unittest.TestCase):
    def test_last_429_installs_cooldown_and_releases_original_reservation(self):
        clock = Clock()
        limiter = p18.RateLimiter(image_interval=0, clock=clock)
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "7"})))
        network = p18.Network("test/1 (offline)", client=client, limiter=limiter, retries=0)
        try:
            with tempfile.TemporaryDirectory() as directory:
                ticket = limiter.reserve_image(UPLOAD)
                with self.assertRaises(p18.RetryableError):
                    network.image(UPLOAD, Path(directory), "a" * 64, 1024, ticket=ticket)
            self.assertEqual(limiter.snapshot("image")["responses"], 1)
            self.assertEqual(limiter.snapshot("image")["cooldown_seconds"], 7)
            self.assertEqual(limiter.image_delay(THUMB), 7)
            self.assertEqual(limiter.image_delay(UPLOAD), 7)
            clock.advance(7)
            following = limiter.reserve_image(UPLOAD)
            self.assertIsNotNone(following)
            limiter.finish_image(following, unused=True)
        finally:
            network.close()

    def test_staged_cache_hit_releases_ticket_without_spending_original_credit(self):
        clock = Clock()
        limiter = p18.RateLimiter(image_interval=0, clock=clock)
        requests = []

        def handler(request):
            requests.append(request.url)
            return httpx.Response(200, content=png_bytes(), headers={"X-Cache-Status": "miss"})

        network = p18.Network("test/1 (offline)", limiter=limiter,
                              client=httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            with tempfile.TemporaryDirectory() as directory:
                staging = p18.Staging(Path(directory), capacity=4096, max_bytes=1024)
                row = {"file_id": "b" * 64, "info": {"download_url": UPLOAD}}
                first = staging.fetch(network, row, ticket=limiter.reserve_image(UPLOAD))
                self.assertFalse(first["cache_hit"])
                cached = staging.fetch(network, row, ticket=limiter.reserve_image(UPLOAD))
                self.assertTrue(cached["cache_hit"])
                self.assertEqual(len(requests), 1)
                for _ in range(7):
                    ticket = limiter.reserve_image(UPLOAD)
                    self.assertIsNotNone(ticket)
                    limiter.finish_image(ticket, status=200, cache="miss")
                self.assertIsNone(limiter.reserve_image(UPLOAD))
        finally:
            network.close()

    def test_pipeline_waits_for_original_credit_without_starving_thumbnails(self):
        clock = Clock()

        class AdvancingEvent(threading.Event):
            def __init__(self):
                super().__init__()
                self.delays = []

            def wait(self, timeout=None):
                if self.is_set():
                    return True
                if timeout is None or len(self.delays) >= 100:
                    raise AssertionError("Pipeline did not make bounded progress")
                self.delays.append(timeout)
                clock.advance(timeout)
                return False

        cancel = AdvancingEvent()
        limiter = p18.RateLimiter(api_interval=0, image_interval=0, clock=clock, cancel=cancel)
        thumbnail_seen = threading.Event()
        requests, requests_lock = [], threading.Lock()

        def handler(request):
            host = request.url.host
            self.assertIn(host, {"upload.wikimedia.org", "thumb.wikimedia.org"})
            with requests_lock:
                first_original = host == "upload.wikimedia.org" and not any(
                    previous == host for previous, _ in requests)
                requests.append((host, clock()))
            if first_original:
                self.assertTrue(thumbnail_seen.wait(5),
                                "An original awaiting headers occupied both media workers")
            if host == "thumb.wikimedia.org":
                thumbnail_seen.set()
            return httpx.Response(200, content=png_bytes(), headers={"X-Cache-Status": "miss"})

        network = p18.Network("test/1 (offline)", limiter=limiter,
                              client=httpx.Client(transport=httpx.MockTransport(handler)))
        network.cancel = cancel
        try:
            with tempfile.TemporaryDirectory() as directory:
                catalog = p18.Catalog(Path(directory) / "output")
                try:
                    for number in range(12):
                        catalog.add(p18.entity_record(
                            entity(f"Q{number + 1}", f"Portrait {number}.png"), "all", {}))
                    rows = catalog.db.execute("SELECT file_id FROM files ORDER BY file_id").fetchall()
                    # Originals precede thumbnails in the actual catalog order;
                    # their eight-token budget must run out before all twelve finish.
                    for number, row in enumerate(rows):
                        host = "upload.wikimedia.org" if number < 10 else "thumb.wikimedia.org"
                        info = {"download_url": f"https://{host}/{row['file_id']}.png"}
                        catalog.db.execute("UPDATE files SET info=? WHERE file_id=?",
                                           (p18.encode(info), row["file_id"]))
                    catalog.db.commit()
                    self.assertEqual(p18.download(catalog, network, width=500, archive_workers=0,
                                                  max_bytes=1024, staging_bytes=32 * 1024), (12, 0))
                    self.assertEqual(p18.verify(catalog)["verified"], 12)
                finally:
                    catalog.close()
            self.assertTrue(cancel.delays, "The coordinator never waited for the exhausted budget")
            self.assertEqual(sum(host == "upload.wikimedia.org" for host, _ in requests), 10)
            self.assertEqual(sum(host == "thumb.wikimedia.org" for host, _ in requests), 2)
            self.assertTrue(all(started == 0 for host, started in requests
                                if host == "thumb.wikimedia.org"))
        finally:
            network.close()


if __name__ == "__main__":
    unittest.main()
