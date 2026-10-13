#!/usr/bin/env python3
"""Daily literature discovery, Feishu delivery, and local archival.

Default research focus:
不同基因型外生菌根真菌调控马尾松干旱胁迫与复水恢复研究

The script intentionally avoids paid APIs. It queries OpenAlex and Crossref,
scores and deduplicates papers, writes a daily archive under
``C:/r/核心文献下载/raw/daily/YYYY-MM-DD/``, updates an index, and sends a concise
digest to a Feishu bot webhook when ``FEISHU_WEBHOOK`` is available.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover - GitHub Actions installs pyyaml.
    yaml = None


BEIJING_TZ = timezone(timedelta(hours=8))
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
TIMEOUT = 25

DEFAULT_ARCHIVE_ROOT = Path(r"C:\r\核心文献下载")
DEFAULT_CONFIG_PATHS = [
    Path("literature_pipeline_config.yaml"),
    Path("literature_pipeline_config.yml"),
    DEFAULT_ARCHIVE_ROOT / "literature_pipeline_config.yaml",
    DEFAULT_ARCHIVE_ROOT / "literature_pipeline_config.yml",
]

FIELD = "不同基因型外生菌根真菌调控马尾松干旱胁迫与复水恢复研究"
DEFAULT_KEYWORDS = [
    "ectomycorrhizal fungi",
    "Cenococcum geophilum",
    "Pinus massoniana",
    "drought stress",
    "water use efficiency",
    "non-structural carbohydrates",
    "re-watering recovery",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "research_profile": {
        "field": FIELD,
        "organism": "Pinus massoniana",
        "symbionts": [
            "ectomycorrhizal fungi (ECM)",
            "Cenococcum geophilum (Cg)",
            "Suillus (Sl)",
        ],
    },
    "search": {
        "candidate_pool_size": 30,
        "final_selection_count": 5,
        "lookback_days": 14,
        "per_query_per_source": 8,
    },
    "keywords": {
        "include": DEFAULT_KEYWORDS
        + [
            "ectomycorrhiza",
            "Suillus",
            "Pinus",
            "pine seedlings",
            "conifer",
            "WUE",
            "RWC",
            "NSC",
            "soluble sugar",
            "starch",
            "rewatering",
            "post-drought recovery",
            "carbon allocation",
            "photosynthetic gas exchange",
        ],
        "exclude": [
            "human microbiome",
            "crop irrigation only",
            "arbuscular mycorrhizal fungi only",
        ],
    },
    "delivery": {
        "target_name": "飞书群：每日文献",
        "format": "short_digest",
    },
    "archive": {
        "enabled": True,
        "root": str(DEFAULT_ARCHIVE_ROOT),
        "write_wiki": False,
    },
}


@dataclass
class Paper:
    title: str = ""
    authors: str = ""
    year: str = ""
    journal: str = ""
    doi: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""
    openalex_id: str = ""
    score: int = 0
    score_10: float = 0.0
    tier: str = "E_暂存观察"
    reading_depth: str = "Abstract only"
    recommendation_reason: str = ""
    stable_key: str = ""


def log(message: str) -> None:
    print(f"[literature] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[literature][warning] {message}", file=sys.stderr, flush=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def first(value: Any) -> str:
    if isinstance(value, list) and value:
        return clean_text(value[0])
    return clean_text(value)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def simple_yaml_load(text: str) -> dict[str, Any]:
    """Small YAML subset parser for this repository's config file.

    It supports nested mappings and list items written as ``- "value"``. The
    script still prefers PyYAML when installed; this fallback keeps the local
    Windows task dependency-free.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    def scalar(value: str) -> Any:
        value = value.strip()
        if value in {"null", "Null", "NULL", "~"}:
            return None
        if value in {"true", "True", "TRUE"}:
            return True
        if value in {"false", "False", "FALSE"}:
            return False
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        try:
            return int(value)
        except ValueError:
            return value

    raw_lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    def next_is_list(current_index: int, current_indent: int) -> bool:
        for future in raw_lines[current_index + 1 :]:
            future_indent = len(future) - len(future.lstrip(" "))
            if future_indent <= current_indent:
                return False
            return future.strip().startswith("- ")
        return False

    for index, raw_line in enumerate(raw_lines):
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            value = scalar(line[2:])
            if isinstance(parent, list):
                parent.append(value)
            continue

        if ":" not in line or not isinstance(parent, dict):
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = scalar(value)
        else:
            child: dict[str, Any] | list[Any] = [] if next_is_list(index, indent) else {}
            parent[key] = child
            stack.append((indent, child))
    return root


def load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG
    explicit = os.environ.get("LITERATURE_CONFIG")
    paths = [Path(explicit)] if explicit else DEFAULT_CONFIG_PATHS
    for path in paths:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        loaded = (yaml.safe_load(raw) if yaml is not None else simple_yaml_load(raw)) or {}
        if isinstance(loaded, dict):
            config = deep_merge(config, loaded)
            log(f"Loaded config: {path}")
        break
    return config


def archive_root(config: dict[str, Any]) -> Path:
    override = os.environ.get("LITERATURE_ARCHIVE_ROOT")
    configured = config.get("archive", {}).get("root") or str(DEFAULT_ARCHIVE_ROOT)
    return Path(override or configured)


def normalize_doi(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    return value.lower()


def normalize_title(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def stable_key(paper: Paper) -> str:
    if paper.doi:
        return f"doi:{paper.doi}"
    if paper.openalex_id:
        return f"openalex:{paper.openalex_id.lower()}"
    title = normalize_title(paper.title)
    if title:
        return "title:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
    return "url:" + hashlib.sha1((paper.url or paper.title).encode("utf-8")).hexdigest()[:16]


def http_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    req = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "feishu-literature-reminder/2.0"},
    )
    with urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def openalex_abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    words: dict[int, str] = {}
    for word, positions in index.items():
        if isinstance(positions, list):
            for position in positions:
                if isinstance(position, int):
                    words[position] = str(word)
    return " ".join(words[i] for i in sorted(words))


def crossref_year(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "created"):
        parts = item.get(key, {}).get("date-parts")
        if parts and parts[0]:
            return str(parts[0][0])
    return ""


def build_queries(config: dict[str, Any]) -> list[str]:
    explicit = config.get("search", {}).get("queries")
    if explicit:
        return list(dict.fromkeys(str(q).strip() for q in explicit if str(q).strip()))
    configured = config.get("keywords", {}).get("include") or DEFAULT_KEYWORDS
    base_queries = [
        '"ectomycorrhizal fungi" drought',
        '"Cenococcum geophilum" drought',
        '"Suillus" drought Pinus',
        '"Pinus massoniana" drought',
        '"ectomycorrhizal" "water use efficiency"',
        '"ectomycorrhizal" "non-structural carbohydrates"',
        '"drought" "rewatering" Pinus',
        '"post-drought recovery" mycorrhiza',
    ]
    for term in configured:
        term = str(term).strip()
        if term and all(term.lower() not in q.lower() for q in base_queries):
            base_queries.append(term)
    return base_queries[:14]


def fetch_openalex(query: str, per_page: int, from_date: str) -> list[Paper]:
    data = http_json(
        OPENALEX_API,
        {
            "search": query,
            "per-page": per_page,
            "filter": f"from_publication_date:{from_date}",
            "sort": "relevance_score:desc",
        },
    )
    papers: list[Paper] = []
    for item in data.get("results", []) or []:
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in item.get("authorships", []) or []
        ]
        source = (
            item.get("primary_location", {})
            .get("source", {})
            .get("display_name", "")
        )
        doi = normalize_doi(item.get("doi", ""))
        papers.append(
            Paper(
                title=clean_text(item.get("title")),
                authors=", ".join([a for a in authors if a][:8]),
                year=str(item.get("publication_year") or ""),
                journal=clean_text(source),
                doi=doi,
                abstract=clean_text(openalex_abstract(item.get("abstract_inverted_index"))),
                url=item.get("doi") or item.get("id") or "",
                source="OpenAlex",
                openalex_id=str(item.get("id") or ""),
            )
        )
    return papers


def fetch_crossref(query: str, rows: int, from_date: str) -> list[Paper]:
    data = http_json(
        CROSSREF_API,
        {
            "query.bibliographic": query,
            "rows": rows,
            "filter": f"from-pub-date:{from_date}",
            "sort": "relevance",
            "order": "desc",
        },
    )
    papers: list[Paper] = []
    for item in data.get("message", {}).get("items", []) or []:
        authors = []
        for author in item.get("author", []) or []:
            name = " ".join(
                part
                for part in (author.get("given", ""), author.get("family", ""))
                if part
            )
            if name:
                authors.append(name)
        doi = normalize_doi(item.get("DOI", ""))
        papers.append(
            Paper(
                title=first(item.get("title")),
                authors=", ".join(authors[:8]),
                year=crossref_year(item),
                journal=first(item.get("container-title")),
                doi=doi,
                abstract=clean_text(item.get("abstract")),
                url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                source="Crossref",
            )
        )
    return papers


def has_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def is_excluded(paper: Paper, config: dict[str, Any]) -> bool:
    text = " ".join([paper.title, paper.abstract, paper.journal]).lower()
    for term in config.get("keywords", {}).get("exclude", []):
        normalized = str(term).lower().replace(" only", "")
        if normalized and normalized in text:
            return True
    if "human microbiome" in text:
        return True
    if "arbuscular mycorrhizal" in text and "ectomycorrhiz" not in text:
        return True
    return False


def score_paper(paper: Paper, config: dict[str, Any]) -> Paper | None:
    if is_excluded(paper, config):
        return None
    text = " ".join([paper.title, paper.abstract, paper.journal, paper.url]).lower()
    if config.get("search", {}).get("require_research_context", False):
        ecm = has_any(text, ["ectomycorrhiz", "cenococcum", "suillus"])
        host = "pinus massoniana" in text
        woody = has_any(text, ["pinus", "pine", "conifer", "seedling", "tree", "beech", "spruce", "quercus", "poplar"])
        drought = has_any(text, ["drought", "water deficit", "water stress", "rewatering", "re-watering"])
        mechanism = has_any(text, ["nitrogen", "carbon allocation", "non-structural carbohydrate", "nonstructural carbohydrate", "photosynth", "water use efficiency"])
        if not (ecm or host or (woody and drought and mechanism)):
            return None
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if has_any(text, ["ectomycorrhizal", "ectomycorrhiza"]):
        add(16, "ECM/外生菌根")
    if "cenococcum geophilum" in text:
        add(18, "Cenococcum geophilum")
    if "suillus" in text:
        add(12, "Suillus")
    if "pinus massoniana" in text:
        add(18, "Pinus massoniana")
    elif has_any(text, ["pinus", "pine", "conifer"]):
        add(10, "Pinus/pine/conifer")
    if has_any(text, ["drought", "water stress", "water deficit"]):
        add(14, "干旱/水分胁迫")
    if has_any(text, ["water use efficiency", "wue"]):
        add(8, "WUE")
    if has_any(text, ["relative water content", "rwc"]):
        add(6, "RWC")
    if has_any(text, ["non-structural carbohydrates", "nonstructural carbohydrates", "nsc"]):
        add(9, "NSC")
    if has_any(text, ["soluble sugar", "starch", "sugar-starch"]):
        add(7, "可溶性糖/淀粉")
    if has_any(text, ["rewatering", "re-watering", "post-drought recovery", "recovery"]):
        add(10, "复水恢复")
    if has_any(text, ["photosynthesis", "gas exchange", "stomatal conductance"]):
        add(7, "光合/气体交换")
    if has_any(text, ["carbon allocation", "carbon metabolism"]):
        add(7, "碳分配/碳代谢")
    if has_any(text, ["nitrogen uptake", "nitrogen acquisition", "nitrogen allocation", "nitrogen partition", "nitrogen assimilation", "ammonium", "nitrate"]):
        add(10, "氮获取/同化/分配")
    if has_any(text, ["photosynthetic nitrogen use efficiency", "pnue", "rubisco", "chlorophyll"]):
        add(7, "光合氮利用/光合机构")
    if has_any(text, ["13c", "¹³c", "15n", "¹⁵n", "isotope", "pulse label", "qpcr", "transcriptom"]):
        add(4, "示踪/分子机制方法")
    if paper.doi:
        add(3, "DOI")
    if paper.abstract:
        add(3, "摘要可读")
    try:
        year = int(paper.year)
    except ValueError:
        year = 0
    if year >= 2021:
        add(4, "近五年")
    if year >= 2024:
        add(3, "近期")

    paper.score = min(max(score, 0), 100)
    paper.score_10 = round(paper.score / 10, 1)
    if paper.score >= 75:
        paper.tier = "A_核心主线"
    elif paper.score >= 55:
        paper.tier = "B_重要支撑"
    elif paper.score >= 35:
        paper.tier = "C_背景扩展"
    elif paper.score >= 20:
        paper.tier = "D_低优先级"
    else:
        paper.tier = "E_暂存观察"
    paper.reading_depth = "Abstract only" if paper.abstract else "Metadata only"
    paper.recommendation_reason = "；".join(reasons[:9]) or "元数据相关"
    paper.stable_key = stable_key(paper)
    return paper


def deduplicate(papers: list[Paper]) -> list[Paper]:
    seen: dict[str, Paper] = {}
    title_seen: dict[str, str] = {}
    for paper in papers:
        key = stable_key(paper)
        title_key = normalize_title(paper.title)
        existing_key = key if key in seen else title_seen.get(title_key)
        if existing_key is None:
            paper.stable_key = key
            seen[key] = paper
            if title_key:
                title_seen[title_key] = key
            continue

        existing = seen[existing_key]
        for attr in ("doi", "abstract", "url", "journal", "authors", "year", "openalex_id"):
            if not getattr(existing, attr) and getattr(paper, attr):
                setattr(existing, attr, getattr(paper, attr))
        if paper.source and paper.source not in existing.source:
            existing.source = f"{existing.source}+{paper.source}"
    return list(seen.values())


def search_literature(config: dict[str, Any]) -> tuple[list[Paper], list[Paper]]:
    search_cfg = config.get("search", {})
    per_query = int(search_cfg.get("per_query_per_source", 8))
    candidate_limit = int(search_cfg.get("candidate_pool_size", 30))
    final_limit = int(search_cfg.get("final_selection_count", 5))
    lookback_days = int(search_cfg.get("lookback_days", 14))
    from_date = (datetime.now(BEIJING_TZ) - timedelta(days=lookback_days)).date().isoformat()

    all_papers: list[Paper] = []
    for query in build_queries(config):
        log(f"Searching query: {query}")
        try:
            found = fetch_openalex(query, per_query, from_date)
            log(f"OpenAlex found {len(found)} records")
            all_papers.extend(found)
        except Exception as exc:  # noqa: BLE001
            warn(f"OpenAlex failed for {query}: {exc}")
        time.sleep(0.25)
        try:
            found = fetch_crossref(query, per_query, from_date)
            log(f"Crossref found {len(found)} records")
            all_papers.extend(found)
        except Exception as exc:  # noqa: BLE001
            warn(f"Crossref failed for {query}: {exc}")
        time.sleep(0.25)

    deduped = deduplicate(all_papers)
    scored = [paper for paper in (score_paper(p, config) for p in deduped) if paper is not None]
    scored.sort(key=lambda paper: paper.score, reverse=True)
    candidates = scored[:candidate_limit]
    minimum_score = int(search_cfg.get("minimum_recommendation_score", 20))
    recommendations = [paper for paper in candidates if paper.score >= minimum_score][:final_limit]
    log(f"Deduplicated records: {len(deduped)}")
    log(f"Candidate pool kept: {len(candidates)}")
    log(f"Final recommendations: {len(recommendations)}")
    return candidates, recommendations


def truncate(text: str, limit: int = 650) -> str:
    text = clean_text(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def paper_link(paper: Paper) -> str:
    if paper.doi:
        return f"https://doi.org/{paper.doi}"
    return paper.url or paper.openalex_id or "N/A"


def ensure_dirs(root: Path, today: str) -> dict[str, Path]:
    dirs = {
        "root": root,
        "daily": root / "raw" / "daily" / today,
        "date_package": root / today,
        "date_pdf": root / today / "pdf",
        "notes": root / "raw" / "notes",
        "index": root / "index",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def write_markdown(path: Path, config: dict[str, Any], candidates: list[Paper], recommendations: list[Paper]) -> None:
    today = datetime.now(BEIJING_TZ).date().isoformat()
    field = config.get("research_profile", {}).get("field", FIELD)
    lines = [
        f"# 每日文献检索结果 | {today}",
        "",
        f"研究主题：{field}",
        "",
        "## 今日检索关键词",
        "",
        "；".join(str(x) for x in config.get("keywords", {}).get("include", DEFAULT_KEYWORDS)),
        "",
        "## 检索概况",
        "",
        f"候选文献：{len(candidates)} 篇；推荐优先阅读：{len(recommendations)} 篇。",
        "",
        "## 推荐优先阅读文献",
        "",
    ]
    if not recommendations:
        lines += [
            "今日未检索到满足阈值的高相关新文献。",
            "",
            "可考虑临时扩大 lookback_days，或放宽 Pinus / pine / conifer / ECM 相关检索词。",
            "",
        ]
    for index, paper in enumerate(recommendations, 1):
        lines += [
            f"### {index}. {paper.title or 'Untitled'}",
            "",
            f"- 期刊/来源：{paper.journal or 'N/A'}",
            f"- 年份：{paper.year or 'N/A'}",
            f"- 作者：{paper.authors or 'N/A'}",
            f"- DOI：{paper.doi or 'N/A'}",
            f"- 链接：{paper_link(paper)}",
            f"- 评分：{paper.score}/100（{paper.score_10}/10）；分流：{paper.tier}",
            f"- 阅读深度：{paper.reading_depth}",
            f"- 推荐原因：{paper.recommendation_reason}",
            f"- 摘要：{truncate(paper.abstract)}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote Markdown: {path}")


def write_csv(path: Path, papers: list[Paper]) -> None:
    fields = [
        "rank",
        "stable_key",
        "title",
        "authors",
        "year",
        "journal",
        "doi",
        "url",
        "source",
        "score",
        "score_10",
        "tier",
        "reading_depth",
        "recommendation_reason",
        "abstract",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, paper in enumerate(papers, 1):
            row = asdict(paper)
            row["rank"] = index
            row["abstract"] = clean_text(paper.abstract)
            writer.writerow({field: row.get(field, "") for field in fields})
    log(f"Wrote CSV: {path}")


def append_index(path: Path, recommendations: list[Paper], today: str) -> None:
    existing_keys: set[str] = set()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            existing_keys = {row.get("stable_key", "") for row in reader if row.get("stable_key")}

    fields = [
        "date",
        "stable_key",
        "title",
        "year",
        "journal",
        "doi",
        "url",
        "score",
        "tier",
        "source",
    ]
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        for paper in recommendations:
            if paper.stable_key in existing_keys:
                continue
            writer.writerow(
                {
                    "date": today,
                    "stable_key": paper.stable_key,
                    "title": paper.title,
                    "year": paper.year,
                    "journal": paper.journal,
                    "doi": paper.doi,
                    "url": paper_link(paper),
                    "score": paper.score,
                    "tier": paper.tier,
                    "source": paper.source,
                }
            )
    log(f"Updated index: {path}")


def write_note_files(notes_dir: Path, recommendations: list[Paper], today: str) -> None:
    for index, paper in enumerate(recommendations, 1):
        slug = normalize_title(paper.title)[:70].strip().replace(" ", "_") or f"paper_{index}"
        path = notes_dir / f"{today}_{index:02d}_{slug}.md"
        lines = [
            "---",
            f"date: {today}",
            f"score: {paper.score}",
            f"tier: {paper.tier}",
            f"doi: {paper.doi or ''}",
            f"source: {paper.source}",
            "---",
            "",
            f"# {paper.title or 'Untitled'}",
            "",
            f"- Authors: {paper.authors or 'N/A'}",
            f"- Journal: {paper.journal or 'N/A'}",
            f"- Year: {paper.year or 'N/A'}",
            f"- Link: {paper_link(paper)}",
            f"- Reading depth: {paper.reading_depth}",
            f"- Relevance: {paper.recommendation_reason}",
            "",
            "## Abstract",
            "",
            truncate(paper.abstract, 1800) or "N/A",
            "",
            "## 与本研究方向的关联",
            "",
            "待精读后补充：重点关注 ECM/Cg/Suillus 对 Pinus 干旱胁迫、复水恢复、WUE、RWC 与 NSC/SS-St 碳代谢指标的解释价值。",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote note files: {notes_dir}")


def download_recommended_pdfs(recommendations_csv: Path, pdf_dir: Path, package_dir: Path) -> None:
    try:
        from download_literature_pdfs import download_from_csv

        report = package_dir / "pdf_download_report.csv"
        manifest = package_dir / "pdf_manifest.csv"
        results = download_from_csv([recommendations_csv], pdf_dir, report, manifest)
        ok = sum(
            1
            for item in results
            if item["status"] in {
                "downloaded",
                "copied_existing",
                "already_downloaded",
                "downloaded_by_sci_download",
            }
        )
        log(f"Recommended PDF download: {ok}/{len(results)} available; report: {report}")
    except Exception as exc:  # noqa: BLE001
        warn(f"Recommended PDF download failed but digest will continue: {exc}")


def build_feishu_message(config: dict[str, Any], candidates: list[Paper], recommendations: list[Paper], archive_dir: Path) -> str:
    today = datetime.now(BEIJING_TZ).date().isoformat()
    field = config.get("research_profile", {}).get("field", FIELD)
    lines = [
        f"📚 每日文献 | {today} | {config.get('delivery', {}).get('target_name', '每日文献')}",
        "",
        f"研究主题：{field}",
        f"候选池：{len(candidates)} 篇 → 今日推荐：{len(recommendations)} 篇",
        f"本地归档：{archive_dir}",
        "",
    ]
    if not recommendations:
        lines += [
            "今日未检索到满足阈值的高相关新文献。",
            "建议：必要时扩大 lookback_days 或放宽 Pinus / pine / ECM 检索词。",
        ]
        return "\n".join(lines)

    for index, paper in enumerate(recommendations, 1):
        lines += [
            f"🏅 #{index} | {paper.title or 'Untitled'}",
            f"{paper.journal or 'N/A'}, {paper.year or 'N/A'} | ⭐ {paper.score_10}/10 | {paper.tier} | {paper.reading_depth}",
            f"DOI：{paper.doi or 'N/A'}",
            f"一句话：{paper.recommendation_reason}",
            f"摘要要点：{truncate(paper.abstract, 260) or '暂无摘要，仅有元数据。'}",
            f"链接：{paper_link(paper)}",
            "",
        ]
    return "\n".join(lines).strip()


def send_to_feishu(webhook: str, text: str) -> bool:
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(req, timeout=TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        code = int(result.get("code", result.get("StatusCode", -1)))
        if code != 0:
            warn(f"Feishu rejected message: code={code}")
            return False
        log("Feishu accepted message: code=0")
        return True
    except Exception as exc:  # noqa: BLE001
        warn(f"Feishu send failed: {exc}")
        return False


def main() -> int:
    load_dotenv()
    config = load_config()
    today = datetime.now(BEIJING_TZ).date().isoformat()
    dirs = ensure_dirs(archive_root(config), today)

    candidates, recommendations = search_literature(config)

    daily_md = dirs["daily"] / "literature_results.md"
    daily_csv = dirs["daily"] / "literature_results.csv"
    daily_recommendations_csv = dirs["daily"] / "recommended_literature.csv"
    package_md = dirs["date_package"] / "literature_results.md"
    package_csv = dirs["date_package"] / "literature_results.csv"
    package_recommendations_csv = dirs["date_package"] / "recommended_literature.csv"
    root_md = Path("literature_results.md")
    root_csv = Path("literature_results.csv")

    write_markdown(daily_md, config, candidates, recommendations)
    write_csv(daily_csv, candidates)
    write_csv(daily_recommendations_csv, recommendations)
    write_markdown(package_md, config, candidates, recommendations)
    write_csv(package_csv, candidates)
    write_csv(package_recommendations_csv, recommendations)
    write_markdown(root_md, config, candidates, recommendations)
    write_csv(root_csv, candidates)
    append_index(dirs["index"] / "literature_index.csv", recommendations, today)
    write_note_files(dirs["notes"], recommendations, today)
    download_recommended_pdfs(package_recommendations_csv, dirs["date_pdf"], dirs["date_package"])

    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if os.environ.get("LITERATURE_SKIP_FEISHU") == "1":
        log("Feishu delivery skipped for this local download run; cloud workflow handles delivery.")
    elif webhook:
        message = build_feishu_message(config, candidates, recommendations, dirs["date_package"])
        sent = send_to_feishu(webhook, message)
        log(f"Feishu sent: {sent}")
        if not sent:
            return 1
    else:
        warn("FEISHU_WEBHOOK is not set; archive was generated but Feishu was not sent.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

