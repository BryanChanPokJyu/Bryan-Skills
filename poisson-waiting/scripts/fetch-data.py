#!/usr/bin/env python3
"""Fetch a fresh Yahoo Finance snapshot for a Poisson Waiting Card."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Iterable


NETWORK_RETRIES = 3
BACKOFF_SECONDS = 2.0


def preload_macos_system_configuration() -> None:
    """Make curl_cffi work on macOS wheels that omit this framework linkage."""
    if sys.platform != "darwin":
        return
    framework = "/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration"
    try:
        ctypes.CDLL(framework, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass


def fail(message: str, exit_code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


preload_macos_system_configuration()


try:
    import yfinance as yf
except ImportError:
    local_python = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    if local_python.exists() and Path(sys.executable).resolve() != local_python.resolve():
        os.execv(str(local_python), [str(local_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    fail(
        "missing dependency 'yfinance'. Install it with: "
        "python3 -m venv .venv && .venv/bin/python -m pip install yfinance",
        2,
    )


def clean(value: Any) -> Any:
    """Convert pandas, numpy, datetime, and NaN values into JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 6)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().isoformat()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return clean(value.item())
        except Exception:
            pass
    try:
        if math.isnan(value):
            return None
    except Exception:
        pass
    return str(value)


def first(*values: Any) -> Any:
    for value in values:
        value = clean(value)
        if value is not None:
            return value
    return None


def safe_get(mapping: Any, key: str, default: Any = None) -> Any:
    try:
        return mapping.get(key, default)
    except Exception:
        return default


def with_retry(label: str, operation: Any) -> Any:
    last_error = None
    for attempt in range(NETWORK_RETRIES):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < NETWORK_RETRIES:
                delay = BACKOFF_SECONDS * (2**attempt) + random.uniform(0.0, 1.0)
                print(
                    f"warning: Yahoo Finance {label} failed ({exc}); retrying in {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
    raise RuntimeError(f"{label} failed after {NETWORK_RETRIES} attempts: {last_error}")


def safe_fast_info(ticker: Any) -> dict[str, Any]:
    try:
        return with_retry("fast_info", lambda: dict(ticker.fast_info))
    except Exception as exc:
        return {"_error": str(exc)}


def safe_info(ticker: Any) -> dict[str, Any]:
    try:
        return with_retry("info", lambda: dict(ticker.info))
    except Exception as exc:
        return {"_error": str(exc)}


def safe_history(ticker: Any) -> dict[str, Any]:
    try:
        history = with_retry(
            "5-day history",
            lambda: ticker.history(period="5d", interval="1d", auto_adjust=False),
        )
        if history is None or history.empty:
            return {"rows": [], "_warning": "Yahoo Finance returned no 5-day history."}
        rows = []
        for index, row in history.tail(5).iterrows():
            rows.append(
                {
                    "date": clean(index),
                    "open": clean(row.get("Open")),
                    "high": clean(row.get("High")),
                    "low": clean(row.get("Low")),
                    "close": clean(row.get("Close")),
                    "volume": clean(row.get("Volume")),
                }
            )
        return {"rows": rows}
    except Exception as exc:
        return {"rows": [], "_error": str(exc)}


def pick_statement_row(statement: Any, labels: Iterable[str]) -> list[float | None]:
    if statement is None or getattr(statement, "empty", True):
        return []
    for label in labels:
        try:
            if label in statement.index:
                values = statement.loc[label]
                return [clean(value) for value in values.tolist()]
        except Exception:
            continue
    return []


def safe_financial_anchors(ticker: Any, info: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "revenue_yoy_pct": clean_percent(safe_get(info, "revenueGrowth")),
        "gross_margin_pct": clean_percent(safe_get(info, "grossMargins")),
        "revenue_accelerating_hint": None,
        "margin_intact_hint": None,
    }
    try:
        quarterly = with_retry("quarterly financials", lambda: ticker.quarterly_financials)
        revenue = pick_statement_row(quarterly, ("Total Revenue", "Operating Revenue"))
        gross_profit = pick_statement_row(quarterly, ("Gross Profit",))
        result["quarterly_revenue"] = revenue[:6]
        result["quarterly_gross_profit"] = gross_profit[:6]

        current_yoy = growth_pct(revenue, 0, 4)
        previous_yoy = growth_pct(revenue, 1, 5)
        if current_yoy is not None:
            result["revenue_yoy_pct"] = current_yoy
        result["previous_quarter_revenue_yoy_pct"] = previous_yoy
        if current_yoy is not None and previous_yoy is not None:
            result["revenue_accelerating_hint"] = current_yoy > previous_yoy

        gross_margins = []
        for gross, sales in zip(gross_profit, revenue):
            if gross is None or not sales:
                gross_margins.append(None)
            else:
                gross_margins.append(round(100 * gross / sales, 4))
        result["quarterly_gross_margin_pct"] = gross_margins[:6]
        if gross_margins and gross_margins[0] is not None:
            result["gross_margin_pct"] = gross_margins[0]
        if len(gross_margins) >= 2 and gross_margins[0] is not None and gross_margins[1] is not None:
            result["margin_intact_hint"] = gross_margins[0] >= gross_margins[1] - 2.0
    except Exception as exc:
        result["_warning"] = f"quarterly financial anchors unavailable: {exc}"
    return result


def growth_pct(values: list[Any], current_index: int, old_index: int) -> float | None:
    if len(values) <= old_index:
        return None
    current = values[current_index]
    old = values[old_index]
    if current is None or not old:
        return None
    return round(100 * (current / old - 1), 4)


def clean_percent(value: Any) -> float | None:
    value = clean(value)
    if isinstance(value, (int, float)):
        return round(100 * value, 4)
    return None


def safe_calendar(ticker: Any, info: dict[str, Any]) -> dict[str, Any]:
    calendar: dict[str, Any] = {}
    try:
        raw = with_retry("earnings calendar", lambda: ticker.calendar)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    calendar[str(key)] = [clean(item) for item in value]
                else:
                    calendar[str(key)] = clean(value)
    except Exception as exc:
        calendar["_warning"] = str(exc)
    calendar["earnings_timestamp"] = first(
        safe_get(info, "earningsTimestamp"),
        safe_get(info, "earningsTimestampStart"),
    )
    return calendar


def normalize_news_item(item: dict[str, Any]) -> dict[str, Any]:
    content = safe_get(item, "content", {}) or {}
    provider = safe_get(content, "provider", {}) or {}
    canonical = safe_get(content, "canonicalUrl", {}) or {}
    clickthrough = safe_get(content, "clickThroughUrl", {}) or {}
    return {
        "title": first(safe_get(item, "title"), safe_get(content, "title")),
        "publisher": first(safe_get(item, "publisher"), safe_get(provider, "displayName")),
        "published_at": first(
            safe_get(item, "providerPublishTime"),
            safe_get(content, "pubDate"),
            safe_get(content, "displayTime"),
        ),
        "url": first(
            safe_get(item, "link"),
            safe_get(canonical, "url"),
            safe_get(clickthrough, "url"),
        ),
        "summary": first(safe_get(item, "summary"), safe_get(content, "summary")),
    }


def safe_news(ticker: Any, limit: int) -> list[dict[str, Any]]:
    try:
        news = with_retry("recent news", lambda: ticker.news)
        return [normalize_news_item(item) for item in news[:limit]]
    except Exception as exc:
        return [{"_warning": f"recent news unavailable: {exc}"}]


def implied_upside(price: Any, target: Any) -> float | None:
    if not isinstance(price, (int, float)) or not isinstance(target, (int, float)) or price == 0:
        return None
    return round(100 * (target / price - 1), 4)


def make_snapshot(ticker_symbol: str, news_limit: int) -> dict[str, Any]:
    ticker = yf.Ticker(ticker_symbol)
    fast = safe_fast_info(ticker)
    info = safe_info(ticker)
    history = safe_history(ticker)
    current_price = first(
        safe_get(fast, "last_price"),
        safe_get(fast, "lastPrice"),
        safe_get(info, "currentPrice"),
        safe_get(info, "regularMarketPrice"),
    )
    if current_price is None:
        rows = safe_get(history, "rows", [])
        if rows:
            current_price = clean(rows[-1].get("close"))
    target_mean = clean(safe_get(info, "targetMeanPrice"))
    financials = safe_financial_anchors(ticker, info)
    return {
        "source": "Yahoo Finance via yfinance",
        "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "freshness_note": (
            "Price is Yahoo Finance's latest available quote and may be delayed by the exchange. "
            "Fundamentals are based on Yahoo Finance's latest reported period."
        ),
        "ticker": ticker_symbol,
        "identity": {
            "company_name": first(safe_get(info, "longName"), safe_get(info, "shortName"), ticker_symbol),
            "quote_type": clean(safe_get(info, "quoteType")),
            "exchange": first(safe_get(info, "exchange"), safe_get(fast, "exchange")),
            "currency": first(safe_get(info, "currency"), safe_get(fast, "currency")),
            "timezone": first(safe_get(info, "exchangeTimezoneName"), safe_get(fast, "timezone")),
            "industry": clean(safe_get(info, "industry")),
            "sector": clean(safe_get(info, "sector")),
        },
        "price": {
            "current": current_price,
            "previous_close": first(
                safe_get(fast, "previous_close"),
                safe_get(fast, "previousClose"),
                safe_get(info, "previousClose"),
            ),
            "day_high": first(safe_get(fast, "day_high"), safe_get(fast, "dayHigh"), safe_get(info, "dayHigh")),
            "day_low": first(safe_get(fast, "day_low"), safe_get(fast, "dayLow"), safe_get(info, "dayLow")),
            "year_high": first(
                safe_get(fast, "year_high"),
                safe_get(fast, "yearHigh"),
                safe_get(info, "fiftyTwoWeekHigh"),
            ),
            "year_low": first(
                safe_get(fast, "year_low"),
                safe_get(fast, "yearLow"),
                safe_get(info, "fiftyTwoWeekLow"),
            ),
            "market_state": clean(safe_get(info, "marketState")),
        },
        "valuation": {
            "market_cap": first(safe_get(fast, "market_cap"), safe_get(fast, "marketCap"), safe_get(info, "marketCap")),
            "enterprise_value": clean(safe_get(info, "enterpriseValue")),
            "trailing_pe": clean(safe_get(info, "trailingPE")),
            "forward_pe": clean(safe_get(info, "forwardPE")),
            "price_to_sales_ttm": clean(safe_get(info, "priceToSalesTrailing12Months")),
            "enterprise_to_revenue": clean(safe_get(info, "enterpriseToRevenue")),
            "target_mean_price": target_mean,
            "implied_upside_pct": implied_upside(current_price, target_mean),
        },
        "financial_anchors": financials,
        "earnings_calendar": safe_calendar(ticker, info),
        "recent_history": history,
        "recent_news": safe_news(ticker, news_limit),
        "_warnings": [
            warning
            for warning in (
                safe_get(fast, "_error"),
                safe_get(info, "_error"),
            )
            if warning
        ],
    }


def make_card_template(snapshot: dict[str, Any]) -> dict[str, Any]:
    identity = snapshot["identity"]
    return {
        "ticker": snapshot["ticker"],
        "company_name": identity["company_name"],
        "date": dt.date.today().isoformat(),
        "waiting_event": "FILL: name the concrete state-transition event",
        "lambda_direction": "FILL: 低 / 上升 / 高 / 下降",
        "lambda_evidence": ["FILL: cite multiple independent signals"],
        "evidence": {
            "customer_adoption": None,
            "revenue_accelerating": None,
            "margin_intact": None,
            "backlog_growing": None,
            "industry_tailwind": None,
            "price_not_reflected": None,
        },
        "evidence_notes": {
            "customer_adoption": "FILL:",
            "revenue_accelerating": "FILL: compare Yahoo revenue YoY anchors",
            "margin_intact": "FILL: compare Yahoo gross-margin anchors",
            "backlog_growing": "FILL:",
            "industry_tailwind": "FILL:",
            "price_not_reflected": "FILL: use valuation and implied-upside anchors",
        },
        "upside_odds": None,
        "downside_loss": None,
        "falsifiers": {
            "thesis_wrong": "FILL:",
            "state_transition_failed": "FILL:",
            "capital_stagnation": "FILL:",
        },
        "risk_costs": {
            "carry": "FILL: 低 / 中 / 高",
            "fragility": "FILL: 低 / 中 / 高",
            "opportunity": "FILL: 低 / 中 / 高",
            "false_positive": "FILL: 低 / 中 / 高",
        },
        "narrative_check": {
            "no_evidence_update": False,
            "no_falsifier": False,
            "cost_basis_anchoring": False,
            "waited_long_enough_fallacy": False,
            "price_as_evidence": False,
        },
        "action": "FILL: 准备建仓 / 加仓 / 持有等待 / 减仓 / 退出 / 仅观察",
        "reason": "FILL: one sentence grounded in lambda and evidence",
        "review_clock": "FILL: next review date or trigger",
        "_data_snapshot": snapshot,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="Yahoo Finance ticker, for example NVDA or 6501.T")
    parser.add_argument("--news", type=int, default=5, help="Number of recent Yahoo Finance news items")
    parser.add_argument(
        "--throttle",
        type=float,
        default=2.0,
        help="Initial delay in seconds before Yahoo calls; useful for polite batch fetching",
    )
    parser.add_argument("--retries", type=int, default=3, help="Retries for Yahoo calls after transient failures")
    parser.add_argument("--backoff", type=float, default=2.0, help="Base seconds for exponential retry backoff")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    return parser.parse_args()


def main() -> None:
    global NETWORK_RETRIES, BACKOFF_SECONDS
    args = parse_args()
    if args.news < 0:
        fail("--news must be zero or greater")
    if args.throttle < 0:
        fail("--throttle must be zero or greater")
    if args.retries < 1:
        fail("--retries must be one or greater")
    if args.backoff < 0:
        fail("--backoff must be zero or greater")
    NETWORK_RETRIES = args.retries
    BACKOFF_SECONDS = args.backoff
    ticker = args.ticker.strip().upper()
    if not ticker:
        fail("ticker must not be empty")
    if args.throttle:
        time.sleep(args.throttle + random.uniform(0.0, min(1.0, args.throttle)))
    card = make_card_template(make_snapshot(ticker, args.news))
    json.dump(card, sys.stdout, ensure_ascii=False, indent=None if args.compact else 2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
