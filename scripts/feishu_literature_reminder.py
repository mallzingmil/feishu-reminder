#!/usr/bin/env python3
"""Search literature daily, save results, and send a Feishu digest.

The script searches OpenAlex and Crossref, extracts core metadata, scores papers
against the configured research topic, writes Markdown/CSV result files, and
sends the top papers to Feishu when FEISHU_WEBHOOK is available.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:
    yaml = None


BEIJING_TZ = timezone(timedelta(hours=8))
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
TIMEOUT = 20
RESULTS_MD = Path("literature_results.md")
RESULTS_CSV = Path("literature_results.csv")

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
        "lookback_days": 3650,
        "per_query_per_source": 5,
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
    recommendation_reason: str = ""


def log(message: str) -> None:
    print(f"[literature] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[literature][warning] {message}", file=sys.stderr, flush=True)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG
    for name in (
        "literature_config.yml",
        "literature_config.yaml",
        "research_profile.yml",
        "research_profile.yaml",
        "config.yml",
        "config.yaml",
    ):
        path = Path(name)
        if not path.exists():
            continue
        if yaml is None:
            warn(f"{path} exists, but pyyaml is unavailable; using defaults.")
            break
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                config = deep_merge(config, loaded)
                log(f"Loaded config: {path}")
        except Exception as exc:  # noqa: BLE001
            warn(f"Could not read {path}: {exc}")
        break
    return config


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


def normalize_doi(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    return value.lower()


def normalize_title(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def http_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    req = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "feishu-literature-reminder/1.0"},
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


def build_queries() -> list[str]:
    return [
        '"ectomycorrhizal fungi" AND drought',
        '"Cenococcum geophilum" AND drought',
        '"Suillus" AND drought',
        '"Pinus" AND drought AND mycorrhiza',
        '"Pinus massoniana" AND drought',
        '"ectomycorrhizal" AND "water use efficiency"',
        '"ectomycorrhizal" AND "non-structural carbohydrates"',
        '"drought" AND "rewatering" AND "Pinus"',
        '"post-drought recovery" AND mycorrhiza',
    ]


def fetch_openalex(query: str, per_page: int, from_date: str) -> list[Paper]:
    data = http_json(
        OPENALEX_API,
        {
            "search": query.replace(" AND ", " "),
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


def is_excluded(paper: Paper) -> bool:
    text = " ".join([paper.title, paper.abstract, paper.journal]).lower()
    if "human microbiome" in text:
        return True
    if "crop irrigation" in text and not has_any(
        text, ["pinus", "pine", "conifer", "ectomycorrhiz"]
    ):
        return True
    return False


def score_paper(paper: Paper) -> Paper | None:
    if is_excluded(paper):
        return None
    text = " ".join([paper.title, paper.abstract, paper.journal, paper.url]).lower()
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if has_any(text, ["ectomycorrhizal", "ectomycorrhiza"]):
        add(12, "ECM/ectomycorrhizal")
    if "cenococcum geophilum" in text:
        add(14, "Cenococcum geophilum")
    if "suillus" in text:
        add(10, "Suillus")
    if "pinus massoniana" in text:
        add(16, "Pinus massoniana")
    elif has_any(text, ["pinus", "pine", "conifer"]):
        add(10, "Pinus/pine/conifer")
    if has_any(text, ["drought", "water stress"]):
        add(12, "drought/water stress")
    if has_any(text, ["water use efficiency", "wue"]):
        add(8, "WUE")
    if has_any(text, ["relative water content", "rwc"]):
        add(6, "RWC")
    if has_any(text, ["non-structural carbohydrates", "nonstructural carbohydrates", "nsc"]):
        add(8, "NSC")
    if has_any(text, ["soluble sugar", "starch"]):
        add(6, "soluble sugar/starch")
    if has_any(text, ["rewatering", "re-watering", "post-drought recovery"]):
        add(10, "re-watering recovery")
    if has_any(text, ["photosynthesis", "gas exchange", "stomatal conductance"]):
        add(6, "photosynthesis/gas exchange")
    if "carbon allocation" in text:
        add(6, "carbon allocation")
    if paper.doi:
        add(3, "DOI")
    if paper.abstract:
        add(3, "abstract available")
    try:
        year = int(paper.year)
    except ValueError:
        year = 0
    if year >= 2020:
        add(4, "recent >=2020")
    if year >= 2023:
        add(3, "recent >=2023")
    if has_any(text, ["arbuscular mycorrhizal", " amf"]) and not has_any(
        text, ["ectomycorrhizal", "ectomycorrhiza"]
    ):
        score -= 15
        reasons.append("AMF-only penalty")

    paper.score = max(score, 0)
    paper.recommendation_reason = "; ".join(reasons[:8]) or "metadata relevance"
    return paper


def deduplicate(papers: list[Paper]) -> list[Paper]:
    seen: dict[str, Paper] = {}
    for paper in papers:
        keys = []
        if paper.doi:
            keys.append(f"doi:{paper.doi}")
        if paper.openalex_id:
            keys.append(f"openalex:{paper.openalex_id.lower()}")
        title_key = normalize_title(paper.title)
        if title_key:
            keys.append(f"title:{title_key}")
        if not keys:
            keys.append(f"url:{paper.url}")

        existing_key = next((key for key in keys if key in seen), None)
        if existing_key is None:
            seen[keys[0]] = paper
            continue

        existing = seen[existing_key]
        for attr in ("doi", "abstract", "url", "journal", "authors", "year", "openalex_id"):
            if not getattr(existing, attr) and getattr(paper, attr):
                setattr(existing, attr, getattr(paper, attr))
        if paper.source not in existing.source:
            existing.source = f"{existing.source}+{paper.source}"
    return list(seen.values())


def search_literature(config: dict[str, Any]) -> tuple[list[Paper], list[Paper]]:
    search_cfg = config.get("search", {})
    per_query = int(search_cfg.get("per_query_per_source", 5))
    candidate_limit = int(search_cfg.get("candidate_pool_size", 30))
    final_limit = int(search_cfg.get("final_selection_count", 5))
    lookback_days = int(search_cfg.get("lookback_days", 3650))
    from_date = (datetime.now(BEIJING_TZ) - timedelta(days=lookback_days)).date().isoformat()

    all_papers: list[Paper] = []
    for query in build_queries():
        log(f"Searching query: {query}")
        try:
            papers = fetch_openalex(query, per_query, from_date)
            log(f"OpenAlex found {len(papers)} records for: {query}")
            all_papers.extend(papers)
        except Exception as exc:  # noqa: BLE001
            warn(f"OpenAlex request failed for {query}: {exc}")
        try:
            papers = fetch_crossref(query, per_query, from_date)
            log(f"Crossref found {len(papers)} records for: {query}")
            all_papers.extend(papers)
        except Exception as exc:  # noqa: BLE001
            warn(f"Crossref request failed for {query}: {exc}")

    deduped = deduplicate(all_papers)
    log(f"Deduplicated records: {len(deduped)}")
    scored = [paper for paper in (score_paper(p) for p in deduped) if paper is not None]
    scored.sort(key=lambda paper: paper.score, reverse=True)
    candidates = scored[:candidate_limit]
    recommendations = [paper for paper in candidates if paper.score >= 10][:final_limit]
    log(f"Candidate pool kept: {len(candidates)}")
    log(f"Final recommendations: {len(recommendations)}")
    return candidates, recommendations


def truncate(text: str, limit: int = 700) -> str:
    text = clean_text(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def write_markdown(config: dict[str, Any], candidates: list[Paper], recommendations: list[Paper]) -> None:
    today = datetime.now(BEIJING_TZ).date().isoformat()
    field = config.get("research_profile", {}).get("field", FIELD)
    lines = [
        "# 每日文献检索结果",
        "",
        f"日期：{today}  ",
        f"研究主题：{field}",
        "",
        "## 今日检索关键词",
        "",
        "; ".join(DEFAULT_KEYWORDS),
        "",
        "## 今日检索结果概览",
        "",
        f"共找到 {len(candidates)} 篇候选文献，最终推荐 {len(recommendations)} 篇。",
        "",
        "## 推荐优先阅读文献",
        "",
    ]
    if not recommendations:
        lines += [
            "今日未检索到满足条件的新文献。",
            "",
            "建议：",
            "1. 扩大 lookback_days；",
            "2. 放宽关键词；",
            "3. 使用 Pinus / pine / conifer 作为补充检索词。",
        ]
    for index, paper in enumerate(recommendations, 1):
        lines += [
            f"### {index}. {paper.title or 'Untitled'}",
            "",
            f"- 年份：{paper.year}",
            f"- 作者：{paper.authors}",
            f"- 期刊：{paper.journal}",
            f"- DOI：{paper.doi}",
            f"- 链接：{paper.url}",
            f"- 相关性评分：{paper.score}",
            f"- 推荐原因：{paper.recommendation_reason}",
            f"- 摘要：{truncate(paper.abstract)}",
            "",
        ]
    RESULTS_MD.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote {RESULTS_MD}")


def write_csv(candidates: list[Paper]) -> None:
    fields = [
        "rank",
        "title",
        "authors",
        "year",
        "journal",
        "doi",
        "url",
        "source",
        "score",
        "recommendation_reason",
        "abstract",
    ]
    with RESULTS_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, paper in enumerate(candidates, 1):
            writer.writerow(
                {
                    "rank": index,
                    "title": paper.title,
                    "authors": paper.authors,
                    "year": paper.year,
                    "journal": paper.journal,
                    "doi": paper.doi,
                    "url": paper.url,
                    "source": paper.source,
                    "score": paper.score,
                    "recommendation_reason": paper.recommendation_reason,
                    "abstract": clean_text(paper.abstract),
                }
            )
    log(f"Wrote {RESULTS_CSV}")


def build_feishu_message(config: dict[str, Any], candidates: list[Paper], recommendations: list[Paper]) -> str:
    today = datetime.now(BEIJING_TZ).date().isoformat()
    field = config.get("research_profile", {}).get("field", FIELD)
    lines = [
        f"📚 每日文献检索结果 | {today}",
        "",
        "研究主题：",
        field,
        "",
        "今日检索关键词：",
        "; ".join(DEFAULT_KEYWORDS),
        "",
        "今日检索结果：",
        f"共找到 {len(candidates)} 篇候选文献，推荐优先阅读 {len(recommendations)} 篇。",
        "",
    ]
    if not recommendations:
        lines += [
            "今日未检索到满足条件的新文献。",
            "",
            "建议：",
            "1. 扩大 lookback_days；",
            "2. 放宽关键词；",
            "3. 使用 Pinus / pine / conifer 作为补充检索词。",
        ]
        return "\n".join(lines)

    for index, paper in enumerate(recommendations[:3], 1):
        lines += [
            f"{index}. 文献标题：{paper.title or 'Untitled'}",
            f"   年份：{paper.year}",
            f"   期刊：{paper.journal}",
            f"   DOI：{paper.doi or 'N/A'}",
            f"   链接：{paper.url}",
            f"   推荐原因：{paper.recommendation_reason}",
            "",
        ]
    lines += [
        "结果文件：",
        "- literature_results.md",
        "- literature_results.csv",
        "",
        "提示：",
        "完整结果请到 GitHub Actions 本次运行的 Artifacts 中下载。",
    ]
    return "\n".join(lines)


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
            log(f"Feishu response: {response.read().decode('utf-8', errors='replace')}")
        return True
    except Exception as exc:  # noqa: BLE001
        warn(f"Feishu send failed: {exc}")
        return False


def main() -> int:
    config = load_config()
    candidates, recommendations = search_literature(config)
    write_markdown(config, candidates, recommendations)
    write_csv(candidates)

    webhook = os.environ.get("FEISHU_WEBHOOK")
    if webhook:
        log(f"Feishu sent: {send_to_feishu(webhook, build_feishu_message(config, candidates, recommendations))}")
    else:
        warn("FEISHU_WEBHOOK is not set; results were generated but not sent.")

    log(f"Markdown exists: {RESULTS_MD.exists()}")
    log(f"CSV exists: {RESULTS_CSV.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
