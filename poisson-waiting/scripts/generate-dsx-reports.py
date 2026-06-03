#!/usr/bin/env python3
"""Batch-generate Poisson Waiting drafts for the DSX AI Factory universe."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE = ROOT / "universe" / "dsx-ai-factory-companies.json"
DEFAULT_OUTPUT = ROOT / "reports" / f"dsx-ai-factory-{date.today().isoformat()}"
FETCH = ROOT / "scripts" / "fetch-data.py"
BUILD = ROOT / "scripts" / "build-card.js"
LOCAL_PYTHON = ROOT / ".venv" / "bin" / "python"
NODE_CANDIDATES = [
    Path("/Applications/Codex.app/Contents/Resources/node"),
    Path("/opt/homebrew/bin/node"),
    Path("/usr/local/bin/node"),
]


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_python() -> str:
    return str(LOCAL_PYTHON if LOCAL_PYTHON.exists() else Path(sys.executable))


def resolve_node(explicit: str | None) -> str:
    if explicit:
        return explicit
    env_node = os.environ.get("NODE")
    if env_node:
        return env_node
    for candidate in NODE_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    fail("node not found. Set NODE=/path/to/node.")


def slugify(text: str) -> str:
    allowed = []
    for char in text.lower():
        if char.isalnum():
            allowed.append(char)
        elif char in (" ", "-", "_", ".", "/"):
            allowed.append("-")
    slug = "".join(allowed)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def load_universe(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    companies = data.get("companies", [])
    if not isinstance(companies, list) or not companies:
        fail(f"no companies found in {path}")
    return companies


def run_json(command: list[str]) -> dict[str, Any]:
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        return {
            "_command_failed": True,
            "_returncode": proc.returncode,
            "_stderr": proc.stderr.strip(),
            "_stdout": proc.stdout.strip(),
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "_command_failed": True,
            "_returncode": proc.returncode,
            "_stderr": f"invalid JSON from command: {exc}\n{proc.stderr.strip()}",
            "_stdout": proc.stdout.strip(),
        }


def run_text(command: list[str], input_text: str) -> tuple[int, str, str]:
    proc = subprocess.run(command, input=input_text, text=True, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def bool_or_false(value: Any) -> bool:
    return bool(value) if value is not None else False


def infer_price_not_reflected(snapshot: dict[str, Any]) -> bool:
    valuation = snapshot.get("valuation", {})
    upside = valuation.get("implied_upside_pct")
    forward_pe = valuation.get("forward_pe")
    ps = valuation.get("price_to_sales_ttm")
    if isinstance(upside, (int, float)):
        return upside >= 20
    if isinstance(forward_pe, (int, float)) and forward_pe <= 35:
        return True
    if isinstance(ps, (int, float)) and ps <= 6:
        return True
    return False


def infer_lambda(evidence: dict[str, bool], snapshot: dict[str, Any]) -> str:
    count = sum(1 for value in evidence.values() if value)
    revenue_yoy = (snapshot.get("financial_anchors") or {}).get("revenue_yoy_pct")
    upside = (snapshot.get("valuation") or {}).get("implied_upside_pct")
    if count >= 5 and isinstance(upside, (int, float)) and upside >= 20:
        return "高"
    if count >= 3:
        return "上升"
    if isinstance(revenue_yoy, (int, float)) and revenue_yoy < 0:
        return "下降"
    return "低"


def percent_or_unknown(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else "未知"


def row_from_json(path: Path, output_dir: Path) -> dict[str, Any] | None:
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    rel_json = path.relative_to(output_dir)
    markdown = Path("markdown") / f"{path.stem}.md"
    if not (output_dir / markdown).exists():
        markdown = Path("markdown") / f"{path.stem.replace('_', '-')}.md"
    snapshot = card.get("_data_snapshot") or {}
    return {
        "ticker": card.get("ticker"),
        "company": card.get("company_name"),
        "lambda_direction": card.get("lambda_direction"),
        "action": card.get("action"),
        "json": str(rel_json),
        "markdown": str(markdown),
        "warnings": snapshot.get("_warnings") or [],
    }


def collect_existing_rows(output_dir: Path) -> list[dict[str, Any]]:
    json_dir = output_dir / "json"
    if not json_dir.exists():
        return []
    rows = []
    for path in sorted(json_dir.glob("*.json")):
        row = row_from_json(path, output_dir)
        if row:
            rows.append(row)
    return rows


def create_filled_card(raw_card: dict[str, Any], company: dict[str, Any]) -> dict[str, Any]:
    snapshot = raw_card.get("_data_snapshot") or {}
    financial = snapshot.get("financial_anchors") or {}
    valuation = snapshot.get("valuation") or {}
    categories = company.get("categories") or []
    category_text = " / ".join(categories)
    ticker = company.get("ticker") or raw_card.get("ticker")
    company_name = company.get("company") or raw_card.get("company_name") or ticker
    revenue_accel = bool_or_false(financial.get("revenue_accelerating_hint"))
    margin_intact = bool_or_false(financial.get("margin_intact_hint"))
    price_not_reflected = infer_price_not_reflected(snapshot)
    evidence = {
        "customer_adoption": True,
        "revenue_accelerating": revenue_accel,
        "margin_intact": margin_intact,
        "backlog_growing": False,
        "industry_tailwind": True,
        "price_not_reflected": price_not_reflected,
    }
    lambda_direction = infer_lambda(evidence, snapshot)
    upside = valuation.get("implied_upside_pct")
    downside = 20 if lambda_direction in ("高", "上升") else 25
    if isinstance(upside, (int, float)):
        upside_odds = max(0, round(upside, 1))
    else:
        upside_odds = 20 if price_not_reflected else 10
    warnings = snapshot.get("_warnings") or []
    news = snapshot.get("recent_news") or []
    news_titles = [item.get("title") for item in news if isinstance(item, dict) and item.get("title")]
    lambda_evidence = [
        f"被纳入 NVIDIA DSX AI Factory 图谱的 {category_text} 环节，说明其业务与 AI Factory 供给链存在直接相关性。",
        f"Yahoo Finance 最新快照：收入 YoY {percent_or_unknown(financial.get('revenue_yoy_pct'))}，毛利率 {percent_or_unknown(financial.get('gross_margin_pct'))}，隐含上行 {percent_or_unknown(upside)}。",
    ]
    if news_titles:
        lambda_evidence.append(f"近期 Yahoo 新闻锚点：{news_titles[0]}")
    if warnings:
        lambda_evidence.append(f"数据缺口警告：{'; '.join(str(w) for w in warnings[:2])}")
    card = dict(raw_card)
    card.update(
        {
            "ticker": ticker,
            "company_name": company_name,
            "date": date.today().isoformat(),
            "waiting_event": f"等待 {company_name} 在 {category_text} 环节的 AI 数据中心需求，继续转化为收入加速、订单/backlog 或利润率韧性。",
            "lambda_direction": lambda_direction,
            "lambda_evidence": lambda_evidence,
            "evidence": evidence,
            "evidence_notes": {
                "customer_adoption": f"DSX 图谱将其归入 {category_text}，作为 AI Factory 产业链采用信号；仍需用客户订单或管理层指引继续交叉验证。",
                "revenue_accelerating": f"Yahoo 财务锚点 revenue_accelerating_hint={financial.get('revenue_accelerating_hint')}，当前收入 YoY={financial.get('revenue_yoy_pct')}。",
                "margin_intact": f"Yahoo 财务锚点 margin_intact_hint={financial.get('margin_intact_hint')}，当前毛利率={financial.get('gross_margin_pct')}。",
                "backlog_growing": "批量脚本未读取公司订单/backlog 原文，暂按未确认处理；后续应以财报、RPO、订单或产能指引验证。",
                "industry_tailwind": "AI Factory 资本开支、数据中心电力/散热/计算系统需求构成行业顺风；该图谱本身是行业链条确认。",
                "price_not_reflected": f"Yahoo 隐含上行={upside}，forward PE={valuation.get('forward_pe')}，PS={valuation.get('price_to_sales_ttm')}。",
            },
            "upside_odds": upside_odds,
            "downside_loss": downside,
            "falsifiers": {
                "thesis_wrong": "AI 数据中心相关收入、订单或客户采用未能在后续财报中体现，且管理层下修相关需求预期。",
                "state_transition_failed": "连续两个复盘周期没有新增订单、收入加速、毛利率改善或客户扩张信号。",
                "capital_stagnation": "λ 下降且同一期间其他 AI Factory 标的出现更密集、更可验证的证据网络。",
            },
            "risk_costs": {
                "carry": "中",
                "fragility": "中",
                "opportunity": "中" if lambda_direction in ("高", "上升") else "高",
                "false_positive": "中",
            },
            "narrative_check": {
                "no_evidence_update": bool(warnings),
                "no_falsifier": False,
                "cost_basis_anchoring": False,
                "waited_long_enough_fallacy": False,
                "price_as_evidence": False,
            },
            "action": "持有等待" if lambda_direction in ("高", "上升") else "仅观察",
            "reason": f"λ 判断为{lambda_direction}，依据是 DSX 产业链位置与 Yahoo 财务/估值锚点的交叉验证；订单/backlog 仍需后续证据补强。",
            "review_clock": "下次财报发布后，或出现大客户订单、数据中心 capex 指引、毛利率明显恶化/改善时复盘。",
        }
    )
    return card


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--news", type=int, default=5)
    parser.add_argument("--throttle", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=10.0)
    parser.add_argument("--node", default=None)
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="Allow writing into an existing output directory; existing per-company files are skipped, not overwritten.",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Write draft reports even when Yahoo Finance core price data is unavailable.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    companies = load_universe(args.universe)
    selected = companies[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    if not selected:
        fail("selected company list is empty")
    if args.out.exists() and any(args.out.iterdir()) and not args.allow_existing:
        fail(f"output directory already exists and is not empty: {args.out}")
    json_dir = args.out / "json"
    md_dir = args.out / "markdown"
    json_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    py = resolve_python()
    node = resolve_node(args.node)
    index_rows = collect_existing_rows(args.out) if args.allow_existing else []
    indexed_json = {row["json"] for row in index_rows}
    for number, company in enumerate(selected, start=args.offset + 1):
        ticker = company["ticker"]
        slug = f"{number:02d}-{slugify(ticker)}-{slugify(company['company'])}"
        json_path = json_dir / f"{slug}.json"
        md_path = md_dir / f"{slug}.md"
        if json_path.exists() or md_path.exists():
            if args.allow_existing:
                print(f"[{number}/{len(companies)}] skipping existing {ticker} {company['company']}", file=sys.stderr)
                continue
            fail(f"refusing to overwrite existing report files for {ticker}: {json_path} / {md_path}")
        print(f"[{number}/{len(companies)}] fetching {ticker} {company['company']}", file=sys.stderr)
        raw = run_json(
            [
                py,
                str(FETCH),
                ticker,
                "--news",
                str(args.news),
                "--throttle",
                str(args.throttle),
                "--retries",
                str(args.retries),
                "--backoff",
                str(args.backoff),
            ]
        )
        snapshot = raw.get("_data_snapshot") or {}
        price = (snapshot.get("price") or {}).get("current")
        if raw.get("_command_failed"):
            fail(f"fetch-data command failed for {ticker}: {raw.get('_stderr') or raw.get('_stdout')}")
        if price is None and not args.allow_incomplete:
            warnings = snapshot.get("_warnings") or []
            fail(
                f"Yahoo Finance core price data unavailable for {ticker}; "
                f"not writing incomplete report. Warnings: {'; '.join(str(w) for w in warnings) or 'none'}"
            )
        card = create_filled_card(raw, company)
        json_text = json.dumps(card, ensure_ascii=False, indent=2)
        json_path.write_text(json_text + "\n", encoding="utf-8")
        rc, markdown, stderr = run_text([node, str(BUILD)], json_text)
        if rc != 0:
            rc, markdown, stderr = run_text([node, str(BUILD), "--allow-draft"], json_text)
        if rc != 0:
            fail(f"build-card failed for {ticker}: {stderr}")
        md_path.write_text(markdown, encoding="utf-8")
        snapshot = card.get("_data_snapshot") or {}
        row = {
            "ticker": ticker,
            "company": company["company"],
            "lambda_direction": card["lambda_direction"],
            "action": card["action"],
            "json": str(json_path.relative_to(args.out)),
            "markdown": str(md_path.relative_to(args.out)),
            "warnings": snapshot.get("_warnings") or [],
        }
        if row["json"] not in indexed_json:
            index_rows.append(row)
            indexed_json.add(row["json"])
        if args.throttle and number != args.offset + len(selected):
            time.sleep(args.throttle)
    (args.out / "index.json").write_text(json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# DSX AI Factory Poisson Reports", ""]
    for row in index_rows:
        warn = " ⚠️" if row["warnings"] else ""
        lines.append(f"- `{row['ticker']}` {row['company']}：{row['lambda_direction']} / {row['action']} - [{row['markdown']}]({row['markdown']}){warn}")
    (args.out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(index_rows)} reports to {args.out}")


if __name__ == "__main__":
    main()
