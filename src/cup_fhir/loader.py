"""Find FHIR Bundles under an S3 prefix (or a local folder) and load them as raw JSON."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FHIR_SUFFIXES = (".json", ".ndjson", ".json.gz", ".ndjson.gz")


@dataclass
class LoadResult:
    """What came back from :func:`load_bundles`.

    ``bundles`` maps ``"<file location>#<n>"`` to the raw Bundle JSON (a dict).
    The ``#<n>`` suffix tells apart several Bundles in one NDJSON file.
    """

    bundles: dict[str, dict] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    files_without_bundles: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"LoadResult({len(self.bundles)} bundles, "
            f"{len(self.files_without_bundles)} files without bundles, "
            f"{len(self.errors)} errors)"
        )


def _parse_documents(location: str, data: bytes) -> list[dict]:
    """Parse a file's bytes as JSON, falling back to NDJSON (one JSON doc per line)."""
    if location.endswith(".gz"):
        data = gzip.decompress(data)
    text = data.decode("utf-8-sig").strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, list) else [doc]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _iter_s3(uri: str, session=None, unsigned: bool = False) -> Iterator[tuple[str, bytes]]:
    import boto3

    parsed = urlparse(uri)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    session = session or boto3.Session()
    if unsigned:
        from botocore import UNSIGNED
        from botocore.config import Config

        s3 = session.client("s3", config=Config(signature_version=UNSIGNED))
    else:
        s3 = session.client("s3")

    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(FHIR_SUFFIXES):
                location = f"s3://{bucket}/{key}"
                try:
                    yield location, s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                except Exception as e:  # surface per-file failures without stopping the scan
                    yield location, e


def _iter_local(path: str | Path) -> Iterator[tuple[str, bytes]]:
    root = Path(path).expanduser()
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    for p in files:
        if p.name.lower().endswith(FHIR_SUFFIXES):
            yield str(p), p.read_bytes()


def load_bundles(
    source: str | Path,
    *,
    profile: str | None = None,
    session=None,
    unsigned: bool = False,
) -> LoadResult:
    """Load every FHIR Bundle found under ``source``.

    Parameters
    ----------
    source:
        An ``s3://bucket/prefix/`` URI, or a local file or folder path.
        Folders are searched recursively for ``.json``, ``.ndjson`` and gzipped variants.
    profile:
        AWS profile name to use (from ``~/.aws/config``). Ignored if ``session`` is given.
    session:
        An existing ``boto3.Session``, e.g. one created with assumed-role credentials.
    unsigned:
        Set ``True`` for a public bucket that needs no credentials.

    Files that parse but contain no Bundles are listed in ``files_without_bundles``.
    Files that fail to download or parse are listed in ``errors``. Neither stops the scan.
    """
    source = str(source)
    if source.startswith("s3://"):
        if session is None and profile:
            import boto3

            session = boto3.Session(profile_name=profile)
        files = _iter_s3(source, session=session, unsigned=unsigned)
    else:
        files = _iter_local(source)

    result = LoadResult()
    for location, data in files:
        if isinstance(data, Exception):
            result.errors.append((location, repr(data)))
            continue
        try:
            docs = _parse_documents(location, data)
        except Exception as e:
            result.errors.append((location, repr(e)))
            continue
        found = 0
        for i, doc in enumerate(docs):
            if isinstance(doc, dict) and doc.get("resourceType") == "Bundle":
                result.bundles[f"{location}#{i}"] = doc
                found += 1
        if not found:
            result.files_without_bundles.append(location)

    log.info("%r from %s", result, source)
    return result


def validate_bundles(bundles: dict[str, dict]):
    """Parse raw Bundles into typed ``fhir.resources`` R4B models.

    R4B is the closest model set ``fhir.resources`` ships to R4, and accepts almost all R4 data.
    Returns ``(valid, invalid)``: ``valid`` maps key -> ``Bundle`` model,
    ``invalid`` maps key -> the validation exception.
    """
    from fhir.resources.R4B.bundle import Bundle

    valid, invalid = {}, {}
    for key, doc in bundles.items():
        try:
            valid[key] = Bundle.model_validate(doc) if hasattr(Bundle, "model_validate") else Bundle.parse_obj(doc)
        except Exception as e:
            invalid[key] = e
    return valid, invalid
