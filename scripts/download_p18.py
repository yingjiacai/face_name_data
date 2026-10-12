#!/usr/bin/env python3
"""Collect Wikidata names/P18 pairs and resumable Commons thumbnails.

Python 3.10+, Linux/macOS. Run --help; see README.md for the full-dump workflow.
The catalog is a snapshot, not a claim that an image has been identity-verified.
"""

from __future__ import annotations

import argparse
import bz2
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
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
SPARQL = "https://query.wikidata.org/sparql"
SCHEMA_VERSION = 1
ID_RE = re.compile(r"[QP][1-9][0-9]*\Z")
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_API = {"maxlag", "ratelimited", "readonly", "internal_api_error_DBQueryError"}
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif",
              "TIFF": ".tif", "BMP": ".bmp", "AVIF": ".avif"}


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


def entity_record(entity, scope, source):
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


def import_dump(catalog, path, scope="all", limit=None):
    path = Path(path).resolve()
    if path.name.endswith(".part"):
        raise ValueError("Finish the dump download before importing its .part file.")
    source = {"kind": "dump", "scope": scope, "file": fingerprint(path)}
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
                    record = entity_record(entity, scope, {"kind": "dump", "filename": path.name})
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


class RetryableError(Exception):
    def __init__(self, message, delay=0):
        super().__init__(message)
        self.delay = delay


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
    def __init__(self, api_interval=1.0, image_interval=0.5):
        self.intervals = {"api": api_interval, "image": image_interval}
        self.next_at = {"api": 0.0, "image": 0.0}
        self.lock = threading.Lock()

    def wait(self, group):
        while True:
            with self.lock:
                delay = self.next_at[group] - time.monotonic()
                if delay <= 0:
                    self.next_at[group] = time.monotonic() + self.intervals[group]
                    return
            time.sleep(delay)

    def pause(self, group, delay):
        with self.lock:
            self.next_at[group] = max(self.next_at[group], time.monotonic() + delay)


class Network:
    def __init__(self, user_agent, *, retries=5, client=None, limiter=None):
        if not user_agent or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("Provide --user-agent or P18_USER_AGENT with application name and contact.")
        self.client = client or httpx.Client(headers={"User-Agent": user_agent},
                                             timeout=httpx.Timeout(90, connect=20),
                                             follow_redirects=True)
        self.limiter = limiter or RateLimiter()
        self.retries = retries

    def close(self):
        self.client.close()

    def run(self, group, operation):
        for attempt in range(self.retries + 1):
            self.limiter.wait(group)
            try:
                return operation()
            except (httpx.TransportError, RetryableError) as exc:
                if attempt == self.retries:
                    raise
                delay = max(min(2 ** (attempt + 1), 60), getattr(exc, "delay", 0))
                LOG.warning("Retry %d/%d in %.1fs: %s", attempt + 1, self.retries, delay, exc)
                self.limiter.pause(group, delay)

    @staticmethod
    def check_response(response):
        if response.status_code in RETRY_STATUS:
            raise RetryableError(f"HTTP {response.status_code}", retry_after(response.headers.get("retry-after")))
        response.raise_for_status()

    def json(self, url, params):
        def operation():
            response = self.client.get(url, params=params)
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
        if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org":
            raise ValueError(f"Unexpected Commons image host: {parsed.hostname}")
        directory = root / "images" / file_id[:2]
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f"{file_id}.part"

        def operation():
            digest = hashlib.sha256()
            count = 0
            try:
                with self.client.stream("GET", url) as response:
                    self.check_response(response)
                    if int(response.headers.get("content-length", 0)) > max_bytes:
                        raise ValueError("Image exceeds --max-image-mib")
                    with partial.open("wb") as stream:
                        for block in response.iter_bytes(128 * 1024):
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
                        "nbytes": count, "width": width, "height": height}
            finally:
                partial.unlink(missing_ok=True)
        return self.run("image", operation)


def sample(catalog, network, limit=200, qids=None, scope="all"):
    source = {"kind": "online_sample", "scope": scope,
              "qids": sorted(set(qids)) if qids else None, "limit": None if qids else limit}
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
            record = entity_record(entities[qid], scope, {"kind": "wikidata_api"})
            if record:
                catalog.add(record)
            else:
                LOG.warning("Entity absent, outside scope, or without non-deprecated P18: %s", qid)
        catalog.set("sample_offset", start + len(batch))
        catalog.db.commit()
    catalog.set("sample_complete", True)
    catalog.db.commit()


def resolve_files(network, rows, width):
    params = {"action": "query", "format": "json", "formatversion": 2,
              "titles": "|".join("File:" + row["filename"] for row in rows),
              "redirects": 1, "prop": "imageinfo", "iilimit": 1,
              "iiprop": "url|size|mime|thumbmime|sha1|timestamp|extmetadata",
              "iiextmetadatafilter": "Artist|Credit|LicenseShortName|LicenseUrl|UsageTerms|AttributionRequired|Copyrighted|ImageDescription",
              "iiextmetadatalanguage": "en", "maxlag": 5}
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
                     "resolved_at": now(), "requested_width": width})
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


def download(catalog, network, *, width=960, workers=2, limit=None,
             retry_failed=False, max_bytes=20 * 1024**2, max_failures=10):
    catalog.bind("image_width", width)
    catalog.db.commit()
    cursor = ""
    processed = failures = consecutive = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            # Include completed rows to cheaply detect missing/truncated local files.
            rows = catalog.db.execute("""SELECT file_id,filename,status,path,nbytes
                FROM files WHERE file_id>? ORDER BY file_id LIMIT 100""", (cursor,)).fetchall()
            if not rows:
                break
            cursor = rows[-1]["file_id"]
            pending = []
            for row in rows:
                if row["status"] == "failed" and not retry_failed:
                    continue
                if row["status"] == "downloaded" and row["path"]:
                    path = catalog.root / row["path"]
                    if path.is_file() and path.stat().st_size == row["nbytes"]:
                        continue
                pending.append(dict(row))
            if limit is not None:
                pending = pending[:max(0, limit - processed)]
            for start in range(0, len(pending), 5):
                batch = pending[start:start + 5]
                need_info = []
                for row in batch:
                    cached = catalog.db.execute("SELECT info FROM files WHERE file_id=?", (row["file_id"],)).fetchone()[0]
                    row["info"] = json.loads(cached) if cached and row["status"] != "failed" else None
                    if not row["info"]:
                        need_info.append(row)
                if need_info:
                    # A whole API-batch failure stops this run, preserving pending work.
                    resolved = resolve_files(network, need_info, width)
                    for row in need_info:
                        row["info"] = resolved[row["file_id"]]
                        catalog.db.execute("UPDATE files SET info=? WHERE file_id=?",
                                           (encode(row["info"]), row["file_id"]))
                    catalog.db.commit()
                futures = {}
                for row in batch:
                    catalog.db.execute("UPDATE files SET attempts=attempts+1 WHERE file_id=?", (row["file_id"],))
                    info = row["info"]
                    if info.get("error"):
                        catalog.db.execute("UPDATE files SET status='failed',error=?,updated_at=? WHERE file_id=?",
                                           (info["error"], now(), row["file_id"]))
                        failures += 1
                        consecutive += 1
                        processed += 1
                    else:
                        future = pool.submit(network.image, info["download_url"], catalog.root, row["file_id"], max_bytes)
                        futures[future] = row
                catalog.db.commit()
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        result = future.result()
                    except (httpx.HTTPError, RetryableError, ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                        if isinstance(exc, OSError) and exc.errno in (28, 13, 30):
                            raise  # Disk full/permissions/read-only filesystem are run failures.
                        message = f"{type(exc).__name__}: {exc}"[:2000]
                        LOG.warning("Failed %s: %s", row["filename"], message)
                        catalog.db.execute("UPDATE files SET status='failed',error=?,updated_at=? WHERE file_id=?",
                                           (message, now(), row["file_id"]))
                        failures += 1
                        consecutive += 1
                    else:
                        catalog.db.execute("""UPDATE files SET status='downloaded',path=?,sha256=?,
                            nbytes=?,width=?,height=?,error=NULL,updated_at=? WHERE file_id=?""",
                            (result["path"], result["sha256"], result["nbytes"], result["width"],
                             result["height"], now(), row["file_id"]))
                        consecutive = 0
                    processed += 1
                    catalog.db.commit()
                LOG.info("This run: %d processed, %d failed", processed, failures)
                if consecutive >= max_failures:
                    raise ValueError(f"Stopped after {consecutive} consecutive failures; inspect connectivity/quota and retry.")
            if limit is not None and processed >= limit:
                break
    return processed, failures


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
    cmd.add_argument("--scope", choices=("all", "humans"), default="all")
    cmd = commands.add_parser("import-dump", parents=[common], help="Stream a complete local .json/.json.gz/.json.bz2 dump")
    cmd.add_argument("--dump", type=Path, required=True)
    cmd.add_argument("--limit", type=positive, help="Pause after this many matching entities in this invocation")
    cmd.add_argument("--scope", choices=("all", "humans"), default="all")
    cmd = commands.add_parser("download", parents=[common], help="Download pending thumbnails; resume by repeating")
    cmd.add_argument("--width", type=int, default=None, help="Thumbnail width, default 960; 0 requests originals")
    cmd.add_argument("--workers", type=positive, default=2)
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
            if args.workers > 4:
                raise ValueError("--workers must be at most 4; API/CDN rate limits still apply")
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
        try:
            with output_lock(args.output):
                catalog = Catalog(args.output)
                try:
                    if args.command == "sample":
                        sample(catalog, network, args.limit, args.qids, args.scope)
                    elif args.command == "import-dump":
                        import_dump(catalog, args.dump, args.scope, args.limit)
                    elif args.command == "download":
                        width = args.width if args.width is not None else catalog.get("image_width", 960)
                        download(catalog, network, width=width, workers=args.workers, limit=args.limit,
                                 retry_failed=args.retry_failed, max_bytes=args.max_image_mib * 1024**2,
                                 max_failures=args.max_consecutive_failures)
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
