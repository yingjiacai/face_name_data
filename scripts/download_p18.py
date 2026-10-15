#!/usr/bin/env python3
"""Collect Wikidata names/P18 pairs and resumable Commons thumbnails.

Python 3.10+, Linux/macOS. Run --help; see README.md for the full-dump workflow.
The catalog is a snapshot, not a claim that an image has been identity-verified.
"""

from __future__ import annotations

import argparse
import bz2
from collections import deque
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlparse
import warnings

import httpx
from PIL import Image


LOG = logging.getLogger("p18")
WD_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia moved thumbnails to thumb.wikimedia.org in 2026; originals stay on upload.
COMMONS_IMAGE_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
SPARQL = "https://query.wikidata.org/sparql"
SCHEMA_VERSION = 1
ID_RE = re.compile(r"[QP][1-9][0-9]*\Z")
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_API = {"maxlag", "ratelimited", "readonly", "internal_api_error_DBQueryError"}
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif",
              "TIFF": ".tif", "BMP": ".bmp", "AVIF": ".avif", "MPO": ".mpo"}


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fingerprint(path):
    """Bind a checkpoint to one unchanged dump, without hashing 100+ GB."""
    stat = path.stat()
    with path.open("rb") as stream:
        first = stream.read(65536)
        stream.seek(max(0, stat.st_size - 65536))
        last = stream.read(65536)
    return {"path": str(path.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "edge_sha256": hashlib.sha256(first + last).hexdigest()}


def normalized_filename(value):
    return unicodedata.normalize("NFC", value.replace("_", " ").strip())


def live_statements(entity, prop):
    return [s for s in entity.get("claims", {}).get(prop, [])
            if s.get("rank", "normal") != "deprecated"]


def value_of(statement):
    snak = statement.get("mainsnak", {})
    if snak.get("snaktype") != "value":
        return None
    return snak.get("datavalue", {}).get("value")


def entity_record(entity, scope, source, require_label=False):
    """Keep all non-deprecated P18 statements, including qualifiers/references."""
    qid = entity.get("id", "")
    if not ID_RE.fullmatch(qid):
        return None
    p31 = live_statements(entity, "P31")
    types = sorted({v["id"] for s in p31
                    if isinstance((v := value_of(s)), dict) and "id" in v})
    if scope == "humans" and "Q5" not in types:
        return None
    statements = []
    files = set()
    for statement in live_statements(entity, "P18"):
        value = value_of(statement)
        if isinstance(value, str) and (filename := normalized_filename(value)):
            statements.append(statement)
            files.add(filename)
    if not files:
        return None
    labels = {lang: item["value"] for lang, item in entity.get("labels", {}).items()}
    if require_label and not labels:
        return None
    names = [labels.get(lang) for lang in ("zh", "zh-hans", "en")]
    name = next((n for n in names if n), next(iter(labels.values()), qid))
    return {"qid": qid, "name": name, "name_is_qid_fallback": not bool(labels),
            "labels": labels,
            "aliases": {lang: [a["value"] for a in values]
                        for lang, values in entity.get("aliases", {}).items()},
            "descriptions": {lang: item["value"]
                             for lang, item in entity.get("descriptions", {}).items()},
            "instance_of": types, "is_human": "Q5" in types,
            "p18_files": sorted(files), "p18_statements": statements,
            "commons_categories": [v for s in live_statements(entity, "P373")
                                   if isinstance((v := value_of(s)), str)],
            "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
            "wikidata_revision_id": entity.get("lastrevid"),
            "wikidata_modified": entity.get("modified"),
            "retrieved_at": now(), "source": source,
            "reference_verified": False}


class Catalog:
    def __init__(self, root, readonly=False):
        self.root = Path(root).resolve()
        if readonly:
            self.db = sqlite3.connect((self.root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entities (
                qid TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY, filename TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending', info TEXT,
                path TEXT, sha256 TEXT, nbytes INTEGER, width INTEGER, height INTEGER,
                error TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS pairs (
                qid TEXT NOT NULL REFERENCES entities(qid),
                file_id TEXT NOT NULL REFERENCES files(file_id),
                PRIMARY KEY(qid, file_id));
            CREATE INDEX IF NOT EXISTS pair_files ON pairs(file_id);
            CREATE INDEX IF NOT EXISTS file_status ON files(status);
        """)
        version = self.get("schema_version")
        if version is not None and version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported database schema: {version}")
        self.set("schema_version", SCHEMA_VERSION)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))

    def bind(self, key, value):
        old = self.get(key)
        if old is not None and old != value:
            raise ValueError(f"{key} differs from this output's snapshot. Use a new --output directory.")
        self.set(key, value)

    def add(self, record):
        self.db.execute("INSERT OR IGNORE INTO entities VALUES (?,?)",
                        (record["qid"], encode(record)))
        for filename in record["p18_files"]:
            file_id = hashlib.sha256(filename.encode("utf-8")).hexdigest()
            self.db.execute("INSERT OR IGNORE INTO files(file_id,filename) VALUES (?,?)",
                            (file_id, filename))
            self.db.execute("INSERT OR IGNORE INTO pairs VALUES (?,?)", (record["qid"], file_id))

    def summary(self):
        count = lambda table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {"entities": count("entities"), "name_image_pairs": count("pairs"),
                "unique_commons_files": count("files"),
                "file_status": dict(self.db.execute("SELECT status,COUNT(*) FROM files GROUP BY status")),
                "downloaded_bytes": self.db.execute("SELECT COALESCE(SUM(nbytes),0) FROM files WHERE status='downloaded'").fetchone()[0],
                "catalog_source": self.get("catalog_source"),
                "dump_progress": self.get("dump_progress"),
                "sample_complete": self.get("sample_complete", False),
                "image_width": self.get("image_width"), "reference_verified": False}


@contextmanager
def output_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process is using this output directory.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def import_dump(catalog, path, scope="all", limit=None, require_label=False):
    path = Path(path).resolve()
    if path.name.endswith(".part"):
        raise ValueError("Finish the dump download before importing its .part file.")
    source = {"kind": "dump", "scope": scope, "file": fingerprint(path)}
    if require_label:
        source["require_label"] = True
    catalog.bind("catalog_source", source)
    progress = catalog.get("dump_progress", {"offset": 0, "lines": 0, "matched": 0,
                                             "complete": False, "array": None})
    if progress["complete"]:
        LOG.info("Dump already completely imported.")
        return
    catalog.db.commit()
    opener = gzip.open if path.suffix == ".gz" else bz2.open if path.suffix == ".bz2" else open
    selected = 0
    last_log = time.monotonic()
    with opener(path, "rb") as stream:
        if progress["offset"]:
            LOG.info("Resuming at uncompressed byte %d; compressed input must decompress its prefix again.", progress["offset"])
            stream.seek(progress["offset"])
        while True:
            line = stream.readline()
            if not line:
                if progress["array"]:
                    raise ValueError("Truncated JSON array: missing closing ]. Import remains incomplete.")
                progress["complete"] = True
                break
            progress["lines"] += 1
            content = line.strip()
            if content == b"[":
                if progress["array"] is not None:
                    raise ValueError("Unexpected [ in dump")
                progress["array"] = True
            elif content == b"]":
                if not progress["array"]:
                    raise ValueError("Unexpected ] in dump")
                progress["complete"] = True
            elif content:
                if not content.startswith(b"{") or not content.rstrip(b",").endswith(b"}"):
                    raise ValueError("Expected an official line-oriented JSON dump or entity JSONL")
                if progress["array"] is None:
                    progress["array"] = False
                # The official dump has one complete entity per line. This cheap
                # prefilter avoids decoding the overwhelming majority of entities.
                if b'"P18"' in content:
                    entity = json.loads(content.rstrip(b","))
                    record = entity_record(entity, scope, {"kind": "dump", "filename": path.name},
                                           require_label=require_label)
                    if record:
                        catalog.add(record)
                        selected += 1
                        progress["matched"] += 1
            progress["offset"] = stream.tell()
            if progress["lines"] % 2000 == 0 or progress["complete"] or (limit and selected >= limit):
                catalog.set("dump_progress", progress)
                catalog.db.commit()
            if time.monotonic() - last_log >= 10:
                LOG.info("Imported %d lines, %d matching entities, %.2f GiB uncompressed",
                         progress["lines"], progress["matched"], progress["offset"] / 2**30)
                last_log = time.monotonic()
            if progress["complete"] or (limit and selected >= limit):
                break
        catalog.set("dump_progress", progress)
        catalog.db.commit()
    LOG.info("Import paused/finished: %s", encode(progress))


def human_subset_stats(source, require_label=False):
    """Count a human-only view of an existing catalog without changing it."""
    source.db.execute("DROP TABLE IF EXISTS temp.human_selection")
    source.db.execute(
        """CREATE TEMP TABLE human_selection(
               qid TEXT PRIMARY KEY, missing_label INTEGER NOT NULL) WITHOUT ROWID""")
    try:
        # Parse each large entity JSON once; later counts use only compact QIDs.
        source.db.execute(
            """INSERT INTO human_selection
               SELECT qid,COALESCE(json_extract(data,'$.name_is_qid_fallback'),1)
               FROM entities WHERE json_extract(data,'$.is_human')=1""")
        selected = "WHERE h.missing_label=0" if require_label else ""
        entities = source.db.execute(
            f"SELECT COUNT(*) FROM human_selection h {selected}").fetchone()[0]
        pairs = source.db.execute(
            f"""SELECT COUNT(*) FROM pairs p JOIN human_selection h USING(qid)
                {selected}""").fetchone()[0]
        rows = source.db.execute(
            f"""WITH selected AS (
                    SELECT DISTINCT p.file_id FROM pairs p
                    JOIN human_selection h USING(qid) {selected}
                )
                SELECT f.status,COUNT(*),COALESCE(SUM(f.nbytes),0)
                FROM selected s JOIN files f USING(file_id) GROUP BY f.status""").fetchall()
        status = {row[0]: row[1] for row in rows}
        downloaded_bytes = sum(row[2] for row in rows if row[0] == "downloaded")
        missing_labels = source.db.execute(
            "SELECT COUNT(*) FROM human_selection WHERE missing_label=1").fetchone()[0]
        unique_files = sum(status.values())
        return {"scope": "humans", "require_label": require_label,
                "entities": entities, "name_image_pairs": pairs,
                "unique_commons_files": unique_files,
                "source_file_status": status,
                "source_downloaded_bytes": downloaded_bytes,
                "human_entities_without_labels": missing_labels,
                "new_file_status": {"pending": unique_files}}
    finally:
        source.db.execute("DROP TABLE temp.human_selection")


def derive_human_catalog(source, output, require_label=False, dry_run=False):
    """Build a clean human-only catalog from a completed catalog.

    Entity and relationship data are copied, while every image is reset to
    pending. The source database and its image directory are never changed.
    """
    source_description = source.get("catalog_source")
    progress = source.get("dump_progress")
    if not source_description or source_description.get("scope") not in ("all", "humans"):
        raise ValueError("Source catalog has no supported scope metadata")
    if not progress or not progress.get("complete"):
        raise ValueError("Source dump import is incomplete")
    stats = human_subset_stats(source, require_label)
    if dry_run:
        return {"dry_run": True, **stats}
    if not stats["entities"] or not stats["unique_commons_files"]:
        raise ValueError("Human filter selected no entities or images")

    output = Path(output).resolve()
    existing = [path.name for path in output.iterdir() if path.name != ".writer.lock"]
    if existing:
        raise ValueError(f"Derived output must be empty; found: {', '.join(sorted(existing)[:5])}")

    condition = "json_extract(e.data,'$.is_human')=1"
    if require_label:
        condition += " AND COALESCE(json_extract(e.data,'$.name_is_qid_fallback'),1)=0"
    with tempfile.TemporaryDirectory(prefix=".derive-", dir=output) as temporary:
        target = Catalog(Path(temporary))
        try:
            # This database is disposable until the final atomic rename. DELETE
            # mode avoids a second multi-gigabyte WAL during bulk insertion.
            target.db.execute("PRAGMA journal_mode=DELETE")
            target.db.execute("PRAGMA synchronous=NORMAL")
            target.db.execute("ATTACH DATABASE ? AS source_db",
                              (str(source.root / "state.sqlite3"),))
            target.db.execute(
                f"""INSERT INTO entities(qid,data)
                    SELECT e.qid,e.data FROM source_db.entities e WHERE {condition}""")
            target.db.execute(
                "CREATE TEMP TABLE selected_files(file_id TEXT PRIMARY KEY) WITHOUT ROWID")
            target.db.execute(
                """INSERT INTO selected_files
                   SELECT DISTINCT p.file_id FROM source_db.pairs p
                   JOIN main.entities e USING(qid)""")
            # Only identity and filename are copied. Download state, cached URLs,
            # hashes and paths intentionally start clean in the new catalog.
            target.db.execute(
                """INSERT INTO files(file_id,filename)
                   SELECT f.file_id,f.filename FROM source_db.files f
                   JOIN selected_files s USING(file_id)""")
            target.db.execute(
                """INSERT INTO pairs(qid,file_id)
                   SELECT p.qid,p.file_id FROM source_db.pairs p
                   JOIN main.entities e USING(qid)
                   JOIN selected_files s USING(file_id)""")
            target.set("catalog_source", {
                "kind": "derived", "scope": "humans",
                "require_label": require_label, "downloads_reset": True,
                "created_at": now(), "source_catalog": str(source.root),
                "source": source_description})
            target.set("dump_progress", {
                "complete": True, "derived": True,
                "source_dump_progress": progress})
            target.db.commit()
            problems = target.db.execute("PRAGMA foreign_key_check").fetchall()
            integrity = target.db.execute("PRAGMA integrity_check").fetchone()[0]
            summary = target.summary()
            if problems or integrity != "ok":
                raise ValueError("Derived catalog failed SQLite integrity checks")
            if (summary["entities"] != stats["entities"] or
                    summary["name_image_pairs"] != stats["name_image_pairs"] or
                    summary["unique_commons_files"] != stats["unique_commons_files"] or
                    summary["file_status"] != stats["new_file_status"]):
                raise ValueError("Derived catalog counts differ from the read-only plan")
            target.db.execute("DETACH DATABASE source_db")
        finally:
            target.close()
        os.replace(Path(temporary) / "state.sqlite3", output / "state.sqlite3")
    return {"dry_run": False, **stats,
            "output": str(output), "downloaded_bytes": 0}


class RetryableError(Exception):
    def __init__(self, message, delay=0, status_code=None):
        super().__init__(message)
        self.delay = delay
        self.status_code = status_code


def retry_after(value):
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0.0


class RateLimiter:
    def __init__(self, api_interval=1.0, image_interval=0.0, cancel=None):
        self.intervals = {"api": api_interval, "image": image_interval}
        self.next_at = {"api": 0.0, "image": 0.0}
        self.cooldown_until = {"api": 0.0, "image": 0.0}
        self.rate_limits = {"api": 0, "image": 0}
        self.lock = threading.Lock()
        self.cancel = cancel or threading.Event()

    def wait(self, group):
        while True:
            if self.cancel.is_set():
                raise CancelledError()
            with self.lock:
                delay = self.next_at[group] - time.monotonic()
                if delay <= 0:
                    self.next_at[group] = time.monotonic() + self.intervals[group]
                    return
            if self.cancel.wait(delay):
                raise CancelledError()

    def pause(self, group, delay):
        with self.lock:
            self.next_at[group] = max(self.next_at[group], time.monotonic() + delay)

    def rate_limited(self, group, delay):
        """Share Retry-After and adapt media request starts after distinct 429s."""
        with self.lock:
            current = time.monotonic()
            distinct = current >= self.cooldown_until[group]
            until = current + delay
            self.cooldown_until[group] = max(self.cooldown_until[group], until)
            self.next_at[group] = max(self.next_at[group], until)
            self.rate_limits[group] += 1
            if group == "image" and distinct:
                # Small media files can hit a request-rate limit well below the
                # 25 Mbps byte cap. Start at five requests/s, then converge in
                # 0.05 s steps without double-counting two in-flight responses.
                self.intervals[group] = min(
                    1.0, max(0.2, self.intervals[group] + 0.05))
            return self.intervals[group], self.rate_limits[group], distinct


class BandwidthLimiter:
    """Share an exact rolling-window byte budget across media connections."""
    def __init__(self, mbps=25, cancel=None, clock=None, sleep=None, window_seconds=10):
        if not math.isfinite(mbps) or not 0 < mbps <= 25:
            raise ValueError("--bandwidth-mbps must be greater than 0 and at most 25")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("Bandwidth window must be finite and positive")
        self.bytes_per_second = mbps * 1_000_000 / 8
        self.window_seconds = window_seconds
        self.capacity = self.bytes_per_second * window_seconds
        self.events = deque()
        self.used = 0
        self.lock = threading.Lock()
        self.cancel = cancel or threading.Event()
        self.clock = clock or time.monotonic
        self.sleep = sleep or self.cancel.wait

    def consume(self, size):
        if size < 0 or size > self.capacity:
            raise ValueError("Bandwidth chunk exceeds the rolling-window budget")
        waited = 0.0
        while size:
            if self.cancel.is_set():
                raise CancelledError()
            with self.lock:
                current = self.clock()
                cutoff = current - self.window_seconds
                while self.events and self.events[0][0] <= cutoff:
                    _, expired = self.events.popleft()
                    self.used -= expired
                if self.used + size <= self.capacity:
                    self.events.append((current, size))
                    self.used += size
                    return waited
                released = 0
                release_at = current
                for recorded_at, recorded_size in self.events:
                    released += recorded_size
                    if self.used - released + size <= self.capacity:
                        release_at = recorded_at + self.window_seconds
                        break
                delay = max(0.0, release_at - current)
            self.sleep(delay)
            waited += delay
        return waited


class Network:
    def __init__(self, user_agent, *, retries=5, client=None, limiter=None):
        if not user_agent or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("Provide --user-agent or P18_USER_AGENT with application name and contact.")
        self.client = client or httpx.Client(headers={"User-Agent": user_agent},
                                             timeout=httpx.Timeout(90, connect=20),
                                             limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                                                 keepalive_expiry=60),
                                             follow_redirects=True)
        self.cancel = threading.Event()
        self.limiter = limiter or RateLimiter(cancel=self.cancel)
        self.bandwidth = BandwidthLimiter(cancel=self.cancel)
        self.retries = retries

    def close(self):
        self.client.close()

    def run(self, group, operation):
        for attempt in range(self.retries + 1):
            if self.cancel.is_set():
                raise CancelledError()
            self.limiter.wait(group)
            try:
                return operation()
            except (httpx.TransportError, RetryableError) as exc:
                if attempt == self.retries:
                    raise
                delay = max(min(2 ** (attempt + 1), 60), getattr(exc, "delay", 0))
                if isinstance(exc, RetryableError) and exc.status_code == 429:
                    interval, count, distinct = self.limiter.rate_limited(group, delay)
                    detail = (f"; shared {group} request gap {interval:.3f}s, "
                              f"429 count {count}"
                              + ("" if distinct else " (same cooldown window)"))
                else:
                    self.limiter.pause(group, delay)
                    detail = ""
                LOG.warning("Retry %s %d/%d in %.1fs: %s%s",
                            group, attempt + 1, self.retries, delay, exc, detail)

    @staticmethod
    def check_response(response):
        if response.status_code in RETRY_STATUS:
            raise RetryableError(f"HTTP {response.status_code}",
                                 retry_after(response.headers.get("retry-after")),
                                 status_code=response.status_code)
        response.raise_for_status()

    def json(self, url, params):
        def operation():
            started = time.monotonic()
            # Long Unicode filenames can make a 50-title GET exceed proxy limits.
            if len(str(httpx.QueryParams(params))) > 6000:
                response = self.client.post(url, data=params)
            else:
                response = self.client.get(url, params=params)
            if time.monotonic() - started > 1:
                self.limiter.pause("api", 5)
            self.check_response(response)
            data = response.json()
            if "error" in data:
                error = data["error"]
                if error.get("code") in RETRY_API:
                    raise RetryableError(encode(error), max(5, retry_after(response.headers.get("retry-after"))))
                raise ValueError(f"API error: {encode(error)}")
            return data
        return self.run("api", operation)

    def image(self, url, root, file_id, max_bytes):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in COMMONS_IMAGE_HOSTS:
            raise ValueError(f"Unexpected Commons image host: {parsed.hostname}")
        directory = root / "images" / file_id[:2]
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{file_id}.part"

        def operation():
            digest = hashlib.sha256()
            count = 0
            throttle_seconds = 0.0
            try:
                with self.client.stream("GET", url) as response:
                    self.check_response(response)
                    if int(response.headers.get("content-length", 0)) > max_bytes:
                        raise ValueError("Image exceeds --max-image-mib")
                    with partial.open("wb") as stream:
                        for block in response.iter_bytes(128 * 1024):
                            throttle_seconds += self.bandwidth.consume(len(block))
                            count += len(block)
                            if count > max_bytes:
                                raise ValueError("Image exceeds --max-image-mib")
                            stream.write(block)
                            digest.update(block)
                        stream.flush()
                        os.fsync(stream.fileno())
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(partial) as image:
                        width, height, image_format = image.width, image.height, image.format
                        image.verify()
                    # verify() alone does not decode JPEG pixels.
                    with Image.open(partial) as image:
                        image.load()
                if image_format not in EXTENSIONS:
                    raise ValueError(f"Unsupported raster format: {image_format}")
                target = directory / f"{file_id}{EXTENSIONS[image_format]}"
                os.replace(partial, target)
                return {"path": str(target.relative_to(root)), "sha256": digest.hexdigest(),
                        "nbytes": count, "width": width, "height": height,
                        "throttle_seconds": throttle_seconds}
            finally:
                partial.unlink(missing_ok=True)
        return self.run("image", operation)


def sample(catalog, network, limit=200, qids=None, scope="all", require_label=False):
    source = {"kind": "online_sample", "scope": scope,
              "qids": sorted(set(qids)) if qids else None, "limit": None if qids else limit}
    if require_label:
        source["require_label"] = True
    catalog.bind("catalog_source", source)
    if catalog.get("sample_complete", False):
        LOG.info("Sample already imported.")
        return
    selected = catalog.get("sample_qids")
    if selected is None:
        if qids:
            selected = sorted(set(qids))
        else:
            human = "?item wdt:P31 wd:Q5 ." if scope == "humans" else ""
            query = ("PREFIX wd: <http://www.wikidata.org/entity/>\n"
                     "PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n"
                     "SELECT DISTINCT ?item WHERE { " + human +
                     " ?item wdt:P18 ?image . } LIMIT " + str(limit))
            data = network.json(SPARQL, {"query": query, "format": "json"})
            selected = [b["item"]["value"].rsplit("/", 1)[-1] for b in data["results"]["bindings"]]
            if not selected:
                raise ValueError("SPARQL returned no entities; catalog is not marked complete.")
        if not all(ID_RE.fullmatch(qid) for qid in selected):
            raise ValueError("Invalid Wikidata entity ID")
        catalog.set("sample_qids", selected)
        catalog.db.commit()
    offset = catalog.get("sample_offset", 0)
    for start in range(offset, len(selected), 50):
        batch = selected[start:start + 50]
        data = network.json(WD_API, {"action": "wbgetentities", "format": "json",
                                    "ids": "|".join(batch), "props": "labels|descriptions|aliases|claims|info",
                                    "maxlag": 5})
        entities = data.get("entities")
        if not isinstance(entities, dict):
            raise ValueError("wbgetentities response has no entities object")
        for qid in batch:
            if qid not in entities:
                raise ValueError(f"wbgetentities omitted {qid}; batch checkpoint was not advanced")
            record = entity_record(entities[qid], scope, {"kind": "wikidata_api"},
                                   require_label=require_label)
            if record:
                catalog.add(record)
            else:
                LOG.warning("Entity absent, outside scope, or without non-deprecated P18: %s", qid)
        catalog.set("sample_offset", start + len(batch))
        catalog.db.commit()
    catalog.set("sample_complete", True)
    catalog.db.commit()


def resolve_files(network, rows, width, full_metadata=False):
    params = {"action": "query", "format": "json", "formatversion": 2,
              "titles": "|".join("File:" + row["filename"] for row in rows),
              "redirects": 1, "prop": "imageinfo", "iilimit": 1,
              "iiprop": "url|size|mime|thumbmime|sha1|timestamp", "maxlag": 5}
    if full_metadata:
        params.update(
            iiprop=params["iiprop"] + "|extmetadata",
            iiextmetadatafilter="Artist|Credit|LicenseShortName|LicenseUrl|UsageTerms|AttributionRequired|Copyrighted|ImageDescription",
            iiextmetadatalanguage="en")
    if width:
        params["iiurlwidth"] = width
    data = network.json(COMMONS_API, params)
    query = data.get("query", {})
    if not isinstance(query.get("pages"), list):
        raise ValueError("Commons response has no pages array")
    aliases = {entry["from"]: entry["to"] for key in ("normalized", "redirects", "converted")
               for entry in query.get(key, [])}
    pages = {page["title"]: page for page in query["pages"]}
    resolved = {}
    for row in rows:
        title = "File:" + row["filename"]
        seen = set()
        while title in aliases and title not in seen:
            seen.add(title)
            title = aliases[title]
        page = pages.get(title, {})
        infos = page.get("imageinfo", [])
        if not infos or not infos[0].get("url"):
            resolved[row["file_id"]] = {"error": "commons_file_missing_or_unavailable", "commons_title": title}
            continue
        info = dict(infos[0])
        info.update({"commons_title": title, "commons_page_id": page.get("pageid"),
                     "resolved_at": now(), "requested_width": width,
                     "metadata_profile": "full" if full_metadata else "basic"})
        if width:
            image_url = info.get("thumburl")
            if not image_url and info.get("width", 0) and info["width"] <= width and info.get("mime") != "image/svg+xml":
                image_url = info["url"]
            if not image_url:
                info["error"] = "thumbnail_unavailable; original was not downloaded"
        else:
            image_url = info["url"]
        info["download_url"] = image_url
        resolved[row["file_id"]] = info
    return resolved


class Staging:
    """Local, restartable spool. Only the coordinator changes budget accounting."""
    def __init__(self, root, capacity, max_bytes):
        self.root = root / "staging"
        if self.root.is_symlink():
            raise ValueError("staging must remain on the local disk beside state.sqlite3")
        self.root.mkdir(parents=True, exist_ok=True)
        self.capacity, self.max_bytes = capacity, max_bytes
        self.sizes = {}
        self.reserved = set()
        # Incomplete local writes have no valid manifest and can be downloaded again.
        for path in self.root.glob("images/*/*"):
            if not re.fullmatch(r"[0-9a-f]{64}", path.stem):
                continue
            if path.suffix == ".part":
                path.unlink()
            elif path.suffix in EXTENSIONS.values():
                if not self.manifest(path.stem).exists():
                    path.unlink()
                else:
                    self.sizes[path.stem] = self.sizes.get(path.stem, 0) + path.stat().st_size
        for path in self.root.glob("*.json.part"):
            if re.fullmatch(r"[0-9a-f]{64}\.json\.part", path.name):
                path.unlink()

    def manifest(self, file_id):
        return self.root / f"{file_id}.json"

    @property
    def used(self):
        return sum(max(size, self.max_bytes) if key in self.reserved else size
                   for key, size in self.sizes.items()) + sum(
                       self.max_bytes for key in self.reserved if key not in self.sizes)

    def reserve(self, file_id):
        extra = max(0, self.max_bytes - self.sizes.get(file_id, 0))
        if self.used + extra > self.capacity:
            return False
        self.reserved.add(file_id)
        return True

    def remove_files(self, file_id):
        self.manifest(file_id).unlink(missing_ok=True)
        for path in (self.root / "images" / file_id[:2]).glob(f"{file_id}.*"):
            if path.suffix in {*EXTENSIONS.values(), ".part"}:
                path.unlink()

    def release(self, file_id, *, discard=False):
        if discard:
            self.remove_files(file_id)
        self.reserved.discard(file_id)
        size = sum(path.stat().st_size for path in
                   (self.root / "images" / file_id[:2]).glob(f"{file_id}.*")
                   if path.suffix in EXTENSIONS.values())
        if size:
            self.sizes[file_id] = size
        else:
            self.sizes.pop(file_id, None)

    def fetched(self, file_id, size):
        """Completed downloads need their actual bytes, not a maximum-size slot."""
        self.sizes[file_id] = size
        self.reserved.discard(file_id)

    def fetch(self, network, row):
        file_id, url = row["file_id"], row["info"]["download_url"]
        started = time.monotonic()
        try:
            result = json.loads(self.manifest(file_id).read_text())
            allowed = {f"images/{file_id[:2]}/{file_id}{ext}" for ext in EXTENSIONS.values()}
            if result["path"] not in allowed or result["download_url"] != url:
                raise ValueError("Staged source changed")
            path = self.root / result["path"]
            if path.stat().st_size != result["nbytes"] or result["nbytes"] > self.max_bytes:
                raise ValueError("Staged image size changed")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != result["sha256"]:
                raise ValueError("Staged image checksum changed")
            return {**result, "fetch_seconds": time.monotonic() - started,
                    "throttle_seconds": 0.0, "cache_hit": True}
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            self.remove_files(file_id)
        result = network.image(url, self.root, file_id, self.max_bytes)
        result["download_url"] = url
        with atomic_text(self.manifest(file_id)) as stream:
            stream.write(encode(result))
            stream.flush()
            os.fsync(stream.fileno())
        return {**result, "fetch_seconds": time.monotonic() - started, "cache_hit": False}


def store_image(staging_root, output_root, result, retries=2):
    """Publish a verified local file. The DB commit is the completion boundary.

    Writing the final key avoids remote .part uploads, rereads and copy/delete
    renames. On interruption it remains uncommitted and will be overwritten.
    """
    started = time.monotonic()
    source, target = staging_root / result["path"], output_root / result["path"]
    for attempt in range(retries + 1):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with source.open("rb") as src, target.open("wb") as dst:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(block)
                    digest.update(block)
                    size += len(block)
                if size != result["nbytes"] or digest.hexdigest() != result["sha256"]:
                    raise ValueError("Staged image changed during upload; retained for retry")
                dst.flush()
                os.fsync(dst.fileno())
            return {**result, "store_seconds": time.monotonic() - started}
        except OSError as exc:
            if exc.errno in (28, 13, 30) or attempt == retries:
                raise
            LOG.warning("Storage retry %d/%d for %s: %s", attempt + 1, retries, result["path"], exc)
            time.sleep(2 ** attempt)


class DownloadProgress:
    def __init__(self, total, interval):
        self.total, self.interval = total, interval
        self.processed = self.failures = self.stored_bytes = 0
        self.fetch_seconds = self.store_seconds = 0.0
        self.throttle_seconds = 0.0
        self.fetched = self.stored = self.cache_hits = 0
        self.metadata_seconds = self.metadata_idle_seconds = 0.0
        self.metadata_batches = self.metadata_files = 0
        self.last_at = time.monotonic()
        self.samples = deque([(self.last_at, 0, 0)])

    def report(self, staging, active, force=False, *, fetching=0, storing=0, ready=0):
        current = time.monotonic()
        if not force and current - self.last_at < self.interval:
            return
        self.samples.append((current, self.processed, self.stored_bytes))
        while len(self.samples) > 2 and self.samples[1][0] < current - 60:
            self.samples.popleft()
        at, count, size = self.samples[0]
        elapsed = max(current - at, 0.001)
        rate = (self.processed - count) / elapsed
        remaining = max(0, self.total - self.processed)
        eta = f"{remaining / rate / 3600:.1f}h" if rate else "unknown"
        LOG.info("This run: %d processed, %d failed | %.2f files/s | %.2f MiB/s stored | "
                 "remaining ~%d | ETA %s | avg fetch %.2fs, throttle %.3fs, store %.2fs | "
                 "metadata %.2fs/batch, %.1f files/batch, link wait %.1fs | "
                 "%d fetching, %d storing, %d ready | "
                 "%d staged reused | %d in flight | staging reserved %.1f/%.1f MiB",
                 self.processed, self.failures, rate, (self.stored_bytes - size) / elapsed / 1024**2,
                 remaining, eta, self.fetch_seconds / max(1, self.fetched),
                 self.throttle_seconds / max(1, self.fetched),
                 self.store_seconds / max(1, self.stored),
                 self.metadata_seconds / max(1, self.metadata_batches),
                 self.metadata_files / max(1, self.metadata_batches), self.metadata_idle_seconds,
                 fetching, storing, ready, self.cache_hits, active,
                 staging.used / 1024**2, staging.capacity / 1024**2)
        self.last_at = current


def download(catalog, network, *, width=960, workers=2, limit=None,
             retry_failed=False, max_bytes=20 * 1024**2, max_failures=10,
             upload_workers=4, staging_bytes=1024**3, skip_existing_check=False,
             log_interval=15, metadata_batch_size=50, metadata_prefetch=200,
             full_metadata=False):
    if workers not in (1, 2):
        raise ValueError("--workers must be 1 or 2 (Wikimedia media concurrency)")
    if upload_workers < 1 or staging_bytes < max_bytes:
        raise ValueError("Upload workers must be positive; --staging-mib must cover --max-image-mib")
    if not 1 <= metadata_batch_size <= 50 or metadata_prefetch < 1:
        raise ValueError("Metadata batch size must be 1..50; prefetch must be positive")
    catalog.bind("image_width", width)
    catalog.db.commit()
    network.cancel.clear()
    staging = Staging(catalog.root, staging_bytes, max_bytes)
    eligible = "status!='downloaded'" if retry_failed else "status='pending'"
    total = catalog.db.execute(f"SELECT COUNT(*) FROM files WHERE {eligible}").fetchone()[0]
    progress = DownloadProgress(min(total, limit) if limit is not None else total, log_interval)
    ready, futures = deque(), {}
    consecutive = selected = 0
    exhausted = False
    fatal = None
    # Link lookahead consumes no image disk space. Storage concurrency must not
    # be capped at 32 when an operator chooses 32 or 64 upload workers.
    window = max(32, workers + upload_workers * 2)
    batch_size = min(metadata_batch_size, metadata_prefetch, 5 if full_metadata else 50)
    LOG.info("Download pipeline: %d media connections, %d storage workers, %d-file window, "
             "%d metadata/batch, %d links prefetched (%s), %.1f Mbps media cap; local staging: %s",
             workers, upload_workers, window, batch_size, metadata_prefetch,
             "full" if full_metadata else "basic",
             network.bandwidth.bytes_per_second * 8 / 1_000_000, staging.root)

    def candidates():
        resumed = set()
        # Prioritize retained uploads so a full spool can drain on restart.
        for file_id in list(staging.sizes):
            row = catalog.db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if row is None or row["status"] == "downloaded":
                staging.release(file_id, discard=True)
            elif row["status"] != "failed" or retry_failed:
                resumed.add(file_id)
                yield dict(row)
        cursor = ""
        while True:
            # The optional fast resume avoids stat calls through an object mount.
            condition = f"AND ({eligible})" if skip_existing_check else ""
            rows = catalog.db.execute(f"""SELECT file_id,filename,status,path,nbytes,info
                FROM files WHERE file_id>? {condition} ORDER BY file_id LIMIT 100""", (cursor,)).fetchall()
            if not rows:
                return
            cursor = rows[-1]["file_id"]
            for row in rows:
                if row["file_id"] in resumed or (row["status"] == "failed" and not retry_failed):
                    continue
                if row["status"] == "downloaded":
                    path = catalog.root / row["path"] if row["path"] else None
                    if path and path.is_file() and path.stat().st_size == row["nbytes"]:
                        continue
                    progress.total = min(progress.total + 1, limit) if limit is not None else progress.total + 1
                yield dict(row)

    rows = iter(candidates())

    def fail(row, exc, *, systemic=True):
        nonlocal consecutive
        if isinstance(exc, OSError) and exc.errno in (28, 13, 30):
            raise exc
        message = f"{type(exc).__name__}: {exc}"[:2000]
        LOG.warning("Failed %s: %s", row["filename"], message)
        catalog.db.execute("UPDATE files SET status='failed',error=?,updated_at=? WHERE file_id=?",
                           (message, now(), row["file_id"]))
        catalog.db.commit()
        progress.processed += 1
        progress.failures += 1
        consecutive = consecutive + 1 if systemic else consecutive
        if systemic and consecutive >= max_failures and fatal is None:
            raise ValueError(f"Stopped after {consecutive} consecutive failures; inspect connectivity/storage and retry.")

    def metadata_batch(batch):
        started = time.monotonic()
        result = resolve_files(network, batch, width, full_metadata)
        return result, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=1) as metadata_pool, \
            ThreadPoolExecutor(max_workers=workers) as image_pool, \
            ThreadPoolExecutor(max_workers=upload_workers) as upload_pool:
        while True:
            try:
                for future in [f for f in futures if f.done()]:
                    kind, job = futures.pop(future)
                    try:
                        result = future.result()
                    except CancelledError:
                        if kind != "metadata":
                            staging.release(job["file_id"])
                        continue
                    except (httpx.HTTPError, RetryableError, ValueError, OSError,
                            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                        if kind == "metadata":
                            raise  # A failed API batch remains pending.
                        staging.release(job["file_id"])
                        unavailable = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 410)
                        bad_image = kind == "fetch" and isinstance(
                            exc, (ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning))
                        # Pillow reports corrupt/unsupported files as OSError,
                        # whereas actual storage errors carry an errno.
                        bad_image = bad_image or (kind == "fetch" and isinstance(exc, OSError) and exc.errno is None)
                        fail(job, exc, systemic=not (unavailable or bad_image))
                        continue
                    if kind == "metadata":
                        result, seconds = result
                        progress.metadata_seconds += seconds
                        progress.metadata_batches += 1
                        progress.metadata_files += len(job)
                        for row in job:
                            info = result[row["file_id"]]
                            previous = row.pop("previous_info", None)
                            if previous and previous.get("extmetadata") and not info.get("extmetadata"):
                                # Keep the earlier attribution snapshot with its
                                # original resolution time, without requesting it again.
                                info["previous_extmetadata"] = {
                                    "resolved_at": previous.get("resolved_at"),
                                    "sha1": previous.get("sha1"),
                                    "value": previous["extmetadata"]}
                            elif previous and previous.get("previous_extmetadata"):
                                info["previous_extmetadata"] = previous["previous_extmetadata"]
                            row["info"] = info
                            catalog.db.execute("UPDATE files SET info=? WHERE file_id=?",
                                               (encode(row["info"]), row["file_id"]))
                        catalog.db.commit()
                        if fatal is None:
                            ready.extend(job)
                    elif kind == "fetch":
                        staging.fetched(job["file_id"], result["nbytes"])
                        progress.fetch_seconds += result["fetch_seconds"]
                        progress.throttle_seconds += result.get("throttle_seconds", 0.0)
                        progress.fetched += 1
                        progress.cache_hits += int(result["cache_hit"])
                        if fatal is None:
                            task = upload_pool.submit(store_image, staging.root, catalog.root, result)
                            futures[task] = ("store", job)
                        else:
                            staging.release(job["file_id"])
                    else:
                        catalog.db.execute("""UPDATE files SET status='downloaded',path=?,sha256=?,
                            nbytes=?,width=?,height=?,error=NULL,updated_at=? WHERE file_id=?""",
                            (result["path"], result["sha256"], result["nbytes"], result["width"],
                             result["height"], now(), job["file_id"]))
                        catalog.db.commit()
                        # Never discard the only verified local copy before committing.
                        staging.release(job["file_id"], discard=True)
                        progress.processed += 1
                        progress.stored += 1
                        progress.stored_bytes += result["nbytes"]
                        progress.store_seconds += result["store_seconds"]
                        consecutive = 0

                if fatal is None:
                    fetching = sum(kind == "fetch" for kind, _ in futures.values())
                    storing = sum(kind == "store" for kind, _ in futures.values())
                    for _ in range(len(ready)):
                        row = ready[0]
                        if row["info"].get("error"):
                            ready.popleft()
                            catalog.db.execute("UPDATE files SET status='pending',attempts=attempts+1 WHERE file_id=?", (row["file_id"],))
                            fail(row, ValueError(row["info"]["error"]), systemic=False)
                        elif fetching < workers and fetching + storing < window and staging.reserve(row["file_id"]):
                            ready.popleft()
                            # A previously completed but missing/truncated file must
                            # stop being "downloaded" before its key is overwritten.
                            catalog.db.execute("UPDATE files SET status='pending',attempts=attempts+1 WHERE file_id=?", (row["file_id"],))
                            catalog.db.commit()
                            task = image_pool.submit(staging.fetch, network, row)
                            futures[task] = ("fetch", row)
                            fetching += 1
                        else:
                            ready.rotate(-1)
                    metadata_active = any(kind == "metadata" for kind, _ in futures.values())
                    free = metadata_prefetch - len(ready)
                    if not exhausted and not metadata_active and free >= batch_size:
                        batch = []
                        for _ in range(min(batch_size, free)):
                            if limit is not None and selected >= limit:
                                exhausted = True
                                break
                            row = next(rows, None)
                            if row is None:
                                exhausted = True
                                break
                            selected += 1
                            cached = row["info"]
                            row["previous_info"] = json.loads(cached) if cached else None
                            row["info"] = row["previous_info"] if cached and (
                                row["status"] != "failed" or row["file_id"] in staging.sizes) else None
                            if full_metadata and row["info"] and "extmetadata" not in row["info"] and row["file_id"] not in staging.sizes:
                                row["info"] = None
                            if row["info"]:
                                row.pop("previous_info", None)
                                ready.append(row)
                            else:
                                batch.append(row)
                        if batch:
                            futures[metadata_pool.submit(metadata_batch, batch)] = ("metadata", batch)
                        # Fill download slots immediately after reading cached metadata.
                        continue
                    if not futures and ready:
                        raise ValueError("Local staging is full; retained uploads need --retry-failed "
                                         "or a larger --staging-mib. No staged images were discarded.")
                if not futures and (fatal is not None or exhausted):
                    break
                fetching = sum(kind == "fetch" for kind, _ in futures.values())
                storing = sum(kind == "store" for kind, _ in futures.values())
                progress.report(staging, len(futures), fetching=fetching, storing=storing, ready=len(ready))
                if futures:
                    waiting_for_links = (not ready and fetching < workers and fetching + storing < window
                                         and staging.used + max_bytes <= staging.capacity and any(
                                             kind == "metadata" for kind, _ in futures.values()))
                    waited_at = time.monotonic()
                    wait(futures, timeout=min(1, log_interval), return_when=FIRST_COMPLETED)
                    if waiting_for_links:
                        progress.metadata_idle_seconds += time.monotonic() - waited_at
            except BaseException as exc:
                if fatal is not None:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    LOG.warning("Additional error while stopping: %s: %s", type(exc).__name__, exc)
                    continue
                fatal = exc
                LOG.warning("Stopping new work; preserving local staged images and finishing active storage writes.")
                network.cancel.set()
                ready.clear()
                for future in futures:
                    future.cancel()
    progress.report(staging, 0, force=True)
    if fatal is not None:
        raise fatal
    return progress.processed, progress.failures


def verify(catalog, repair=False):
    good = bad = 0
    rows = catalog.db.execute("SELECT file_id,path,nbytes,sha256 FROM files WHERE status='downloaded'")
    for row in rows:
        path = catalog.root / row["path"]
        valid = False
        if path.is_file() and path.stat().st_size == row["nbytes"]:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            valid = digest.hexdigest() == row["sha256"]
        if valid:
            good += 1
        else:
            bad += 1
            LOG.warning("Missing or corrupt: %s", path)
            if repair:
                catalog.db.execute("UPDATE files SET status='pending',error='local_integrity_failed' WHERE file_id=?", (row["file_id"],))
    catalog.db.commit()
    return {"verified": good, "invalid": bad, "requeued": bad if repair else 0}


@contextmanager
def atomic_text(path):
    partial = path.with_name(path.name + ".part")
    try:
        with partial.open("w", encoding="utf-8") as stream:
            yield stream
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def export(catalog):
    with atomic_text(catalog.root / "entities.jsonl") as stream:
        for row in catalog.db.execute("SELECT data FROM entities ORDER BY qid"):
            stream.write(row[0] + "\n")
    with atomic_text(catalog.root / "pairs.jsonl") as stream:
        for row in catalog.db.execute("""SELECT e.qid,e.data,f.* FROM pairs p
            JOIN entities e USING(qid) JOIN files f USING(file_id) ORDER BY p.qid,p.file_id"""):
            entity = json.loads(row["data"])
            info = json.loads(row["info"]) if row["info"] else None
            record = {"qid": row["qid"], "name": entity["name"], "labels": entity["labels"],
                      "is_human": entity["is_human"], "instance_of": entity["instance_of"],
                      "filename": row["filename"], "file_id": row["file_id"],
                      "status": row["status"], "path": row["path"], "sha256": row["sha256"],
                      "bytes": row["nbytes"], "width": row["width"], "height": row["height"],
                      "source": info, "error": row["error"], "updated_at": row["updated_at"],
                      "downloaded_at": row["updated_at"] if row["status"] == "downloaded" else None,
                      "reference_verified": False}
            stream.write(encode(record) + "\n")
    with atomic_text(catalog.root / "summary.json") as stream:
        json.dump(catalog.summary(), stream, ensure_ascii=False, indent=2)
    LOG.info("Exported entities.jsonl, pairs.jsonl, summary.json")


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output", type=Path, default=Path("data/p18"))
    common.add_argument("--user-agent", default=os.environ.get("P18_USER_AGENT"),
                        help="Application name/version and your contact; also P18_USER_AGENT")
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("sample", parents=[common], help="Small online sample or explicit QIDs; not exhaustive")
    cmd.add_argument("--limit", type=positive, default=200)
    cmd.add_argument("--qids", nargs="+")
    cmd.add_argument("--scope", choices=("all", "humans"), default="humans")
    cmd.add_argument("--require-label", action="store_true",
                     help="Exclude entities without any Wikidata label")
    cmd = commands.add_parser("import-dump", parents=[common], help="Stream a complete local .json/.json.gz/.json.bz2 dump")
    cmd.add_argument("--dump", type=Path, required=True)
    cmd.add_argument("--limit", type=positive, help="Pause after this many matching entities in this invocation")
    cmd.add_argument("--scope", choices=("all", "humans"), default="humans")
    cmd.add_argument("--require-label", action="store_true",
                     help="Exclude entities without any Wikidata label")
    cmd = commands.add_parser("derive", help="Create a clean human-only catalog from a completed catalog")
    cmd.add_argument("--source", type=Path, required=True, help="Existing completed catalog")
    cmd.add_argument("--output", type=Path, required=True, help="New empty output directory")
    cmd.add_argument("--scope", choices=("humans",), default="humans")
    cmd.add_argument("--require-label", action="store_true",
                     help="Exclude human entities without any Wikidata label")
    cmd.add_argument("--dry-run", action="store_true", help="Only report selected counts")
    cmd = commands.add_parser("download", parents=[common], help="Download pending thumbnails; resume by repeating")
    cmd.add_argument("--width", type=int, default=None, help="Thumbnail width, default 960; 0 requests originals")
    cmd.add_argument("--workers", type=positive, default=2, help="Wikimedia image connections: 1 or 2 (default 2)")
    cmd.add_argument("--upload-workers", type=positive, default=4, help="Concurrent writes to image storage (default 4)")
    cmd.add_argument("--metadata-batch-size", type=positive, default=50,
                     help="Files per Commons lookup, at most 50 (default 50)")
    cmd.add_argument("--metadata-prefetch", type=positive, default=200,
                     help="Maximum queued image links, separate from staged bytes (default 200)")
    cmd.add_argument("--full-metadata", action="store_true",
                     help="Also request costly attribution/description fields; batches capped at 5")
    cmd.add_argument("--bandwidth-mbps", type=positive_float, default=25, help="Shared media bandwidth cap, at most 25 Mbps")
    cmd.add_argument("--staging-mib", type=positive, default=1024, help="Local staged image budget in MiB (default 1024)")
    cmd.add_argument("--skip-existing-check", action="store_true",
                     help="Trust downloaded DB entries without stat calls; use verify --repair to detect missing/corrupt files")
    cmd.add_argument("--log-interval", type=positive_float, default=15, help="Progress report interval in seconds (default 15)")
    cmd.add_argument("--verbose", action="store_true", help="Also log every HTTP request")
    cmd.add_argument("--limit", type=positive, help="Maximum unique files attempted this invocation")
    cmd.add_argument("--retry-failed", action="store_true")
    cmd.add_argument("--max-image-mib", type=positive, default=20)
    cmd.add_argument("--max-consecutive-failures", type=positive, default=10)
    commands.add_parser("status", parents=[common], help="Show counts and completion state")
    commands.add_parser("export", parents=[common], help="Export entities and name-image pairs to JSONL")
    cmd = commands.add_parser("verify", parents=[common], help="Check downloaded files against stored SHA-256")
    cmd.add_argument("--repair", action="store_true", help="Requeue missing/corrupt files for the next download")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "sample":
            if args.limit > 1000:
                raise ValueError("Online --limit is capped at 1000; use import-dump for exhaustive collection.")
            if args.qids and not all(ID_RE.fullmatch(qid) for qid in args.qids):
                raise ValueError("--qids must contain Wikidata IDs such as Q42 or P18")
        if args.command == "download":
            if args.width is not None and args.width < 0:
                raise ValueError("--width must be zero or positive")
            if args.workers > 2:
                raise ValueError("--workers must be 1 or 2 (Wikimedia media concurrency)")
            if args.bandwidth_mbps > 25:
                raise ValueError("--bandwidth-mbps must be at most 25")
            if args.staging_mib < args.max_image_mib:
                raise ValueError("--staging-mib must cover --max-image-mib")
            if args.metadata_batch_size > 50:
                raise ValueError("--metadata-batch-size must be at most 50")
            logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)
        if args.command == "derive":
            source_root = args.source.resolve()
            output_root = args.output.resolve()
            if source_root == output_root:
                raise ValueError("Derived output must differ from its source")
            if not (source_root / "state.sqlite3").exists():
                raise ValueError("Source catalog has no state.sqlite3")
            if not args.dry_run and (output_root / "state.sqlite3").exists():
                raise ValueError("Derived output already has state.sqlite3")
            with output_lock(source_root):
                source = Catalog(source_root, readonly=True)
                try:
                    if args.dry_run:
                        report = derive_human_catalog(
                            source, output_root, require_label=args.require_label, dry_run=True)
                    else:
                        with output_lock(output_root):
                            report = derive_human_catalog(
                                source, output_root, require_label=args.require_label)
                finally:
                    source.close()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"status", "export", "verify", "download"} and not (args.output / "state.sqlite3").exists():
            raise ValueError("No catalog exists here. Run sample or import-dump first.")
        if args.command == "status":
            catalog = Catalog(args.output, readonly=True)
            try:
                print(json.dumps(catalog.summary(), ensure_ascii=False, indent=2))
            finally:
                catalog.close()
            return 0
        # Validate contact before creating a network job's output directory.
        network = Network(args.user_agent) if args.command in {"sample", "download"} else None
        if args.command == "download":
            network.bandwidth = BandwidthLimiter(args.bandwidth_mbps, cancel=network.cancel)
        try:
            with output_lock(args.output):
                catalog = Catalog(args.output)
                try:
                    if args.command == "sample":
                        sample(catalog, network, args.limit, args.qids, args.scope,
                               require_label=args.require_label)
                    elif args.command == "import-dump":
                        import_dump(catalog, args.dump, args.scope, args.limit,
                                    require_label=args.require_label)
                    elif args.command == "download":
                        width = args.width if args.width is not None else catalog.get("image_width", 960)
                        download(catalog, network, width=width, workers=args.workers, limit=args.limit,
                                 retry_failed=args.retry_failed, max_bytes=args.max_image_mib * 1024**2,
                                 max_failures=args.max_consecutive_failures, upload_workers=args.upload_workers,
                                 staging_bytes=args.staging_mib * 1024**2, skip_existing_check=args.skip_existing_check,
                                 log_interval=args.log_interval, metadata_batch_size=args.metadata_batch_size,
                                 metadata_prefetch=args.metadata_prefetch, full_metadata=args.full_metadata)
                    elif args.command == "export":
                        export(catalog)
                    elif args.command == "verify":
                        report = verify(catalog, args.repair)
                        print(json.dumps(report, ensure_ascii=False, indent=2))
                        return 2 if report["invalid"] else 0
                    print(json.dumps(catalog.summary(), ensure_ascii=False, indent=2))
                    if args.command == "download" and catalog.db.execute("SELECT 1 FROM files WHERE status='failed' LIMIT 1").fetchone():
                        return 2
                finally:
                    catalog.close()
        finally:
            if network:
                network.close()
    except KeyboardInterrupt:
        LOG.warning("Interrupted; committed progress is preserved. Repeat the command to resume.")
        return 130
    except (ValueError, OSError, EOFError, sqlite3.Error, httpx.HTTPError, RetryableError) as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
