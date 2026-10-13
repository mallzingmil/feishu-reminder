#!/usr/bin/env python3
"""Download open-access PDFs listed in literature CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from http.client import IncompleteRead
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlencode
from urllib.request import Request, urlopen


ROOT = Path(r"C:\r\核心文献下载")
PDF_DIR = ROOT / "raw" / "pdf"
REPORT = ROOT / "raw" / "daily" / "pdf_download_report.csv"
SCI_DOWNLOAD = Path.home() / ".codex" / "skills" / "sci-download" / "sci_download.py"
INPUTS = [
    ROOT / "literature_results.csv",
    ROOT / "raw" / "daily" / "2026-07-08" / "literature_results.csv",
]
OPENALEX = "https://api.openalex.org/works"
TIMEOUT = 30


@dataclass
class Row:
    title: str
    doi: str
    year: str
    journal: str
    url: str
    source_csv: str


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_doi(value: str) -> str:
    value = clean(value)
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    return value.lower()


def slugify(text: str, limit: int = 90) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return (text[:limit].strip("_") or "paper")


def request_bytes(url: str, accept: str = "application/pdf,*/*") -> tuple[bytes, str]:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 literature-pdf-downloader/1.0",
            "Accept": accept,
        },
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        content_type = response.headers.get("Content-Type", "")
        try:
            data = response.read()
        except IncompleteRead as exc:
            data = exc.partial
    return data, content_type


def request_text(url: str) -> tuple[str, str]:
    data, content_type = request_bytes(url, "text/html,application/xhtml+xml,*/*")
    return data.decode("utf-8", errors="replace"), content_type


def request_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    data, _ = request_bytes(f"{url}?{urlencode(params)}", "application/json")
    return json.loads(data.decode("utf-8", errors="replace"))


def is_pdf(data: bytes, content_type: str) -> bool:
    return len(data) > 1024 and data.startswith(b"%PDF") and b"%%EOF" in data[-4096:]


def read_rows(inputs: list[Path] | None = None) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    for path in inputs or INPUTS:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for item in reader:
                title = clean(item.get("title"))
                doi = normalize_doi(item.get("doi", ""))
                if not title and not doi:
                    continue
                key = doi or title.lower()
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    Row(
                        title=title,
                        doi=doi,
                        year=clean(item.get("year")),
                        journal=clean(item.get("journal")),
                        url=clean(item.get("url")),
                        source_csv=str(path),
                    )
                )
    return rows


def known_existing(row: Row) -> Path | None:
    existing_patterns = []
    if row.doi == "10.1186/s12870-021-02945-3":
        existing_patterns.append("Wang2021_Suillus_variegatus_BMCPlantBiology.pdf")
    for name in existing_patterns:
        path = ROOT / name
        if path.exists():
            return path
    return None


def mdpi_pdf_from_doi(doi: str) -> str | None:
    mdpi_map = {
        "10.3390/jof9040471": "https://mdpi-res.com/d_attachment/jof/jof-09-00471/article_deploy/jof-09-00471.pdf",
        "10.3390/f12030332": "https://mdpi-res.com/d_attachment/forests/forests-12-00332/article_deploy/forests-12-00332.pdf",
        "10.3390/f15111997": "https://mdpi-res.com/d_attachment/forests/forests-15-01997/article_deploy/forests-15-01997.pdf",
    }
    if doi in mdpi_map:
        return mdpi_map[doi]
    if doi.startswith("10.3390/"):
        return f"https://www.mdpi.com/{doi}/pdf?download=1"
    return None


def frontiers_pdf_from_doi(doi: str) -> str | None:
    if doi.startswith("10.3389/"):
        return f"https://www.frontiersin.org/articles/{doi}/pdf"
    return None


def bmc_pdf_from_doi(doi: str) -> str | None:
    if doi.startswith("10.1186/"):
        return f"https://link.springer.com/content/pdf/{doi}.pdf"
    return None


def preprints_pdf_from_doi(doi: str) -> str | None:
    match = re.match(r"10\.20944/preprints(\d{6})\.(\d+)\.v(\d+)", doi)
    if match:
        ym, manuscript, version = match.groups()
        return f"https://www.preprints.org/manuscript/{ym}.{manuscript}/v{version}/download"
    return None


def iop_pdf_from_doi(doi: str) -> str | None:
    if doi.startswith("10.1088/"):
        return f"https://iopscience.iop.org/article/{doi}/pdf"
    return None


def openalex_pdf_urls(row: Row) -> list[str]:
    urls: list[str] = []
    filters = {}
    if row.doi:
        filters["filter"] = f"doi:{row.doi}"
    else:
        filters["search"] = row.title
    try:
        data = request_json(OPENALEX, {**filters, "per-page": "1"})
    except Exception:
        return urls
    for work in data.get("results", []) or []:
        best = work.get("best_oa_location") or {}
        primary = work.get("primary_location") or {}
        for location in [best, primary, *(work.get("locations") or [])]:
            if not isinstance(location, dict):
                continue
            pdf_url = clean(location.get("pdf_url"))
            landing = clean(location.get("landing_page_url"))
            if pdf_url and pdf_url not in urls:
                urls.append(pdf_url)
            if landing.endswith(".pdf") and landing not in urls:
                urls.append(landing)
    return urls


def landing_pdf_urls(row: Row) -> list[str]:
    urls: list[str] = []
    landing_pages = []
    if row.doi:
        landing_pages.append(f"https://doi.org/{row.doi}")
    if row.url:
        landing_pages.append(row.url)
    for landing in landing_pages:
        try:
            html_text, content_type = request_text(landing)
        except Exception:
            continue
        if "pdf" in content_type.lower():
            urls.append(landing)
            continue
        patterns = [
            r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
            r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
            r'href=["\']([^"\']*/pdf(?:\?[^"\']*)?)["\']',
            r'href=["\']([^"\']*/download(?:\?[^"\']*)?)["\']',
        ]
        for pattern in patterns:
            for match in re.findall(pattern, html_text, flags=re.I):
                url = urljoin(landing, match.replace("&amp;", "&"))
                if url not in urls:
                    urls.append(url)
    return urls


def candidate_urls(row: Row) -> Iterator[str]:
    urls: list[str] = []
    if row.url.endswith(".pdf") or row.url.rstrip("/").endswith("/pdf"):
        urls.append(row.url)
    for builder in [
        mdpi_pdf_from_doi,
        frontiers_pdf_from_doi,
        bmc_pdf_from_doi,
        preprints_pdf_from_doi,
        iop_pdf_from_doi,
    ]:
        if row.doi:
            url = builder(row.doi)
            if url:
                urls.append(url)
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            yield url
    for source in (openalex_pdf_urls, landing_pdf_urls):
        for url in source(row):
            if url and url not in seen:
                seen.add(url)
                yield url


def sci_download_fallback(row: Row, pdf_dir: Path) -> dict[str, str] | None:
    if not row.doi or not SCI_DOWNLOAD.exists():
        return None
    before = {path.resolve() for path in pdf_dir.glob("*.pdf")} if pdf_dir.exists() else set()
    attempts = []
    for source in ("semantic_scholar", "unpaywall"):
        cmd = [
            sys.executable,
            str(SCI_DOWNLOAD),
            row.doi,
            "--source",
            source,
            "--output-dir",
            str(pdf_dir),
        ]
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=90, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            attempts.append(f"{source}: timed out after 90 seconds")
            continue
        attempts.append(f"{source}: exit={proc.returncode}; {proc.stdout.strip() or proc.stderr.strip()}")
        after = {path.resolve() for path in pdf_dir.glob("*.pdf")} if pdf_dir.exists() else set()
        new_files = sorted(path for path in after - before if is_pdf(path.read_bytes(), ""))
        if proc.returncode == 0 and new_files:
            return {
                "status": "downloaded_by_sci_download",
                "title": row.title,
                "doi": row.doi,
                "pdf_path": str(new_files[-1]),
                "source_url": source,
                "note": " | ".join(attempts)[-800:],
            }
    return {
        "status": "not_found_or_closed",
        "title": row.title,
        "doi": row.doi,
        "pdf_path": "",
        "source_url": "sci-download",
        "note": " | ".join(attempts)[-1200:],
    }


def download_one(row: Row, pdf_dir: Path = PDF_DIR) -> dict[str, str]:
    existing = known_existing(row)
    base = f"{row.year or 'unknown'}_{slugify(row.title)}"
    destination = pdf_dir / f"{base}.pdf"
    if destination.exists() and is_pdf(destination.read_bytes(), ""):
        return {
            "status": "already_downloaded",
            "title": row.title,
            "doi": row.doi,
            "pdf_path": str(destination),
            "source_url": "",
            "note": "",
        }
    if not existing and ROOT.exists():
        existing = next((p for p in ROOT.rglob(destination.name)
                         if p != destination and is_pdf(p.read_bytes(), "")), None)
    if existing:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(existing, destination)
        return {
            "status": "copied_existing",
            "title": row.title,
            "doi": row.doi,
            "pdf_path": str(destination),
            "source_url": str(existing),
            "note": "already existed in archive root",
        }

    last_error = ""
    for url in candidate_urls(row):
        try:
            data, content_type = request_bytes(url)
            if not is_pdf(data, content_type):
                last_error = f"not a pdf: {content_type}"
                continue
            pdf_dir.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return {
                "status": "downloaded",
                "title": row.title,
                "doi": row.doi,
                "pdf_path": str(destination),
                "source_url": url,
                "note": f"{len(data)} bytes",
            }
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}: {url}"
        except (URLError, TimeoutError, OSError, IncompleteRead) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)

    fallback = sci_download_fallback(row, pdf_dir)
    if fallback:
        return fallback

    return {
        "status": "not_found_or_closed",
        "title": row.title,
        "doi": row.doi,
        "pdf_path": "",
        "source_url": "",
        "note": last_error or "no open PDF URL found",
    }


def write_manifest(pdf_dir: Path, manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["name", "size_mb", "path"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for path in sorted(pdf_dir.glob("*.pdf")):
            writer.writerow(
                {
                    "name": path.name,
                    "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
                    "path": str(path),
                }
            )


def download_from_csv(
    inputs: list[Path],
    pdf_dir: Path,
    report: Path,
    manifest: Path | None = None,
) -> list[dict[str, str]]:
    rows = read_rows(inputs)
    results = []
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["status", "title", "doi", "pdf_path", "source_url", "note"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            try:
                result = download_one(row, pdf_dir)
            except Exception as exc:
                result = {"status": "pdf_fetch_failed", "title": row.title, "doi": row.doi,
                          "pdf_path": "", "source_url": "", "note": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            writer.writerow(result)
            handle.flush()
            print(f"[pdf] {result['status']}: {row.title[:90]}", flush=True)
    if manifest:
        write_manifest(pdf_dir, manifest)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download open-access PDFs from literature CSV files.")
    parser.add_argument("--input", action="append", dest="inputs", help="Input CSV path. Can be repeated.")
    parser.add_argument("--pdf-dir", default=str(PDF_DIR), help="Output PDF directory.")
    parser.add_argument("--report", default=str(REPORT), help="Download report CSV path.")
    parser.add_argument("--manifest", default="", help="PDF manifest CSV path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = [Path(path) for path in args.inputs] if args.inputs else INPUTS
    manifest = Path(args.manifest) if args.manifest else None
    results = download_from_csv(inputs, Path(args.pdf_dir), Path(args.report), manifest)
    downloaded = sum(
        1
        for item in results
        if item["status"] in {"downloaded", "copied_existing", "already_downloaded", "downloaded_by_sci_download"}
    )
    print(f"processed={len(results)} downloaded_or_existing={downloaded} report={args.report}")
    for item in results:
        print(f"{item['status']}: {item['title'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

