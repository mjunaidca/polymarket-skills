#!/usr/bin/env python3
"""Detect correlated exposure in the paper trading portfolio.

Positions that look diversified by token_id can actually be concentrated
on a single topic.  For example, three separate "insider trading" markets
on different crypto exchanges are effectively one bet on whether exchanges
will face insider-trading accusations.

This script:
  1. Loads open positions from the paper-trading SQLite database.
  2. Clusters positions by topic using keyword extraction from
     market_question (no ML, no external dependencies).
  3. Calculates combined cluster exposure as a fraction of portfolio.
  4. Generates INFO / WARN / ALERT messages based on thresholds.
  5. Computes a diversification score (0-100).

Uses the same patterns as find_edges.py and momentum_scanner.py:
  - argparse CLI, sqlite3 for DB, re for keyword extraction
  - JSON output to stdout when --json, human-readable tables by default
  - No external dependencies beyond the standard library
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_DIR = Path.home() / ".polymarket-paper"
DB_PATH = DB_DIR / "portfolio.db"

# Risk limit from CLAUDE.md -- max single-market exposure
MAX_SINGLE_MARKET_PCT = 0.20

# ---------------------------------------------------------------------------
# Topic categories and keyword rules
# ---------------------------------------------------------------------------

# Broad category definitions: category name -> list of keyword patterns.
# Each pattern is matched case-insensitively against the full market question.
# Order matters: first match wins for category assignment.
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("US Politics / Elections", [
        r"\bpresident\b", r"\belection\b", r"\bsenate\b", r"\bcongress\b",
        r"\brepublican\b", r"\bdemocrat\b", r"\btrump\b", r"\bbiden\b",
        r"\bgop\b", r"\bwhite\s+house\b", r"\bprimary\b", r"\bgovernor\b",
        r"\bmidterm\b", r"\belectoral\b", r"\bcandidate\b", r"\bnomination\b",
        r"\bvote\b", r"\bballot\b", r"\bimpeach\b",
    ]),
    ("Geopolitics / War", [
        r"\bwar\b", r"\bstrike\b", r"\binvasion\b", r"\bmilitary\b",
        r"\bnato\b", r"\bsanction\b", r"\bnuclear\b", r"\bceasefire\b",
        r"\brussia\b", r"\bukraine\b", r"\bchina\b", r"\btaiwan\b",
        r"\biran\b", r"\bisrael\b", r"\bgaza\b", r"\bnorth\s+korea\b",
        r"\bconflict\b", r"\bweapon\b", r"\bairstr\w*\b", r"\bbomb\b",
    ]),
    ("Crypto / Blockchain", [
        r"\bcrypto\b", r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b",
        r"\beth\b", r"\bsolana\b", r"\bsol\b", r"\btoken\b",
        r"\bblockchain\b", r"\bdefi\b", r"\bnft\b", r"\bstablecoin\b",
        r"\bexchange\b", r"\bbinance\b", r"\bcoinbase\b", r"\bkraken\b",
        r"\brobinhood\b", r"\baxiom\b", r"\bmexc\b", r"\bbybit\b",
        r"\bftx\b", r"\bweb3\b", r"\bwallet\b",
    ]),
    ("Sports / NBA", [
        r"\bnba\b", r"\bbasketball\b", r"\blakers\b", r"\bceltics\b",
        r"\bwarriors\b", r"\bmvp\b.*\b(?:season|award)\b",
        r"\bplayoff\b", r"\bfinals\b.*\bnba\b",
    ]),
    ("Sports / Football", [
        r"\bnfl\b", r"\bsuper\s+bowl\b", r"\bfootball\b",
        r"\btouchdown\b", r"\bquarterback\b",
    ]),
    ("Sports / Soccer", [
        r"\bfifa\b", r"\bworld\s+cup\b", r"\bsoccer\b", r"\bpremier\s+league\b",
        r"\bchampions\s+league\b", r"\bla\s+liga\b", r"\bbundesliga\b",
    ]),
    ("Sports / Other", [
        r"\bmlb\b", r"\bnhl\b", r"\bmma\b", r"\bufc\b", r"\bboxing\b",
        r"\btennis\b", r"\bgolf\b", r"\bolympic\b", r"\bf1\b",
        r"\bformula\s+1\b", r"\brace\b.*\bgrand\s+prix\b",
    ]),
    ("Entertainment", [
        r"\boscar\b", r"\bacademy\s+award\b", r"\bgrammy\b", r"\bemmy\b",
        r"\bbox\s+office\b", r"\bmovie\b", r"\bfilm\b", r"\bnetflix\b",
        r"\bdisney\b", r"\bmusic\b", r"\balbum\b", r"\bconcert\b",
        r"\bcelebrity\b",
    ]),
    ("Technology", [
        r"\bai\b", r"\bartificial\s+intelligence\b", r"\bopenai\b",
        r"\bgoogle\b", r"\bapple\b", r"\bmicrosoft\b", r"\btesla\b",
        r"\bspacex\b", r"\bmeta\b", r"\bamazon\b", r"\bnvidia\b",
        r"\bchip\b", r"\bsemiconductor\b", r"\brobot\b",
    ]),
    ("Economy / Finance", [
        r"\bfed\b", r"\binterest\s+rate\b", r"\binflation\b",
        r"\brecession\b", r"\bgdp\b", r"\bstock\s+market\b",
        r"\bs&p\b", r"\bnasdaq\b", r"\bipo\b", r"\btariff\b",
        r"\btrade\s+war\b", r"\bdebt\s+ceiling\b",
    ]),
    ("Weather / Climate", [
        r"\bhurricane\b", r"\btemperature\b", r"\bweather\b",
        r"\bclimate\b", r"\bflood\b", r"\bwildfire\b", r"\bdrought\b",
        r"\btornado\b", r"\bsnow\b", r"\bheat\s+wave\b",
    ]),
    ("Legal / Regulatory", [
        r"\bcourt\b", r"\blawsuit\b", r"\btrial\b", r"\bindict\b",
        r"\bsec\b", r"\bregulat\w*\b", r"\bban\b", r"\blegislat\b",
        r"\bbill\b.*\bpass\b", r"\bsupreme\s+court\b", r"\binsider\s+trading\b",
        r"\bfraud\b", r"\bconvict\b", r"\bguilty\b",
    ]),
    ("Science / Health", [
        r"\bcovid\b", r"\bvaccine\b", r"\bpandemic\b", r"\bvirus\b",
        r"\bfda\b", r"\bdrug\b", r"\bclinical\s+trial\b", r"\bspace\b",
        r"\bmars\b", r"\bmoon\b", r"\bnasa\b",
    ]),
]

# "Shared qualifier" phrases that create tight correlation regardless of
# broad category.  If two positions share one of these phrases, they belong
# to the same fine-grained cluster even if their categories differ.
QUALIFIER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("insider trading",    re.compile(r"insider\s+trading", re.IGNORECASE)),
    ("win the 2024",       re.compile(r"win\s+(?:the\s+)?2024", re.IGNORECASE)),
    ("win the 2025",       re.compile(r"win\s+(?:the\s+)?2025", re.IGNORECASE)),
    ("win the 2026",       re.compile(r"win\s+(?:the\s+)?2026", re.IGNORECASE)),
    ("win the 2027",       re.compile(r"win\s+(?:the\s+)?2027", re.IGNORECASE)),
    ("win the 2028",       re.compile(r"win\s+(?:the\s+)?2028", re.IGNORECASE)),
    ("FIFA World Cup",     re.compile(r"fifa\s+world\s+cup", re.IGNORECASE)),
    ("Super Bowl",         re.compile(r"super\s+bowl", re.IGNORECASE)),
    ("NBA Finals",         re.compile(r"nba\s+finals", re.IGNORECASE)),
    ("NBA MVP",            re.compile(r"nba\s+mvp", re.IGNORECASE)),
    ("Academy Award",      re.compile(r"academy\s+award|oscar", re.IGNORECASE)),
    ("interest rate cut",  re.compile(r"interest\s+rate\s+cut", re.IGNORECASE)),
    ("interest rate hike", re.compile(r"interest\s+rate\s+(?:hike|raise|increase)", re.IGNORECASE)),
    ("government shutdown", re.compile(r"government\s+shutdown", re.IGNORECASE)),
    ("debt ceiling",       re.compile(r"debt\s+ceiling", re.IGNORECASE)),
    ("TikTok ban",         re.compile(r"tiktok\s+ban", re.IGNORECASE)),
    ("recession",          re.compile(r"\brecession\b", re.IGNORECASE)),
    ("nuclear",            re.compile(r"\bnuclear\b", re.IGNORECASE)),
    ("ceasefire",          re.compile(r"\bceasefire\b", re.IGNORECASE)),
]

# Stop words removed before extracting significant keywords for overlap.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "will", "would", "could", "should", "shall", "may", "might", "can",
    "do", "does", "did", "has", "have", "had", "having", "in", "on", "at",
    "to", "for", "of", "with", "by", "from", "as", "into", "about",
    "between", "through", "during", "before", "after", "above", "below",
    "and", "or", "but", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "not", "no", "yes", "any", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such",
    "only", "own", "so", "very", "just", "also", "how", "what", "which",
    "who", "whom", "when", "where", "why", "there", "here", "up", "out",
    "over", "under", "again", "further", "once", "market", "price",
    "end", "date", "by", "before", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november",
    "december", "january", "2024", "2025", "2026", "2027", "2028",
    "2029", "2030",
})

# Minimum keyword length to consider significant
_MIN_KEYWORD_LEN = 3


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    """Open the paper-trading database read-only."""
    if not os.path.isfile(db_path):
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        print(
            "Initialize a portfolio first:\n"
            "  python polymarket-paper-trader/scripts/paper_engine.py --action init",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_portfolio(conn: sqlite3.Connection, portfolio_name: str) -> dict:
    """Load the active portfolio and its open positions.

    Returns a dict with keys: portfolio (row dict), positions (list of row
    dicts), cash_balance, total_value.
    """
    pf = conn.execute(
        "SELECT * FROM portfolios WHERE name = ? ORDER BY id DESC LIMIT 1",
        (portfolio_name,),
    ).fetchone()

    if not pf:
        print(
            f"ERROR: No portfolio named '{portfolio_name}' found.",
            file=sys.stderr,
        )
        sys.exit(1)

    pf = dict(pf)
    pid = pf["id"]

    rows = conn.execute(
        "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
        (pid,),
    ).fetchall()

    positions = [dict(r) for r in rows]

    # Compute total portfolio value
    positions_value = sum(
        p["shares"] * p["current_price"] for p in positions
    )
    total_value = pf["cash_balance"] + positions_value

    return {
        "portfolio": pf,
        "positions": positions,
        "cash_balance": pf["cash_balance"],
        "positions_value": round(positions_value, 4),
        "total_value": round(total_value, 4),
    }


# ---------------------------------------------------------------------------
# Keyword extraction and categorization
# ---------------------------------------------------------------------------

def _extract_keywords(question: str) -> set[str]:
    """Extract significant lowercased keywords from a market question."""
    # Tokenize: split on non-alphanumeric, keep apostrophes inside words
    tokens = re.findall(r"[a-zA-Z][a-zA-Z']*[a-zA-Z]|[a-zA-Z]", question)
    keywords = set()
    for tok in tokens:
        low = tok.lower().strip("'")
        if low not in _STOP_WORDS and len(low) >= _MIN_KEYWORD_LEN:
            keywords.add(low)
    return keywords


def _extract_entities(question: str) -> set[str]:
    """Extract capitalized multi-word entities (proper nouns, company names).

    Looks for sequences of capitalized words (2+ words) or single
    capitalized words that are likely names (not sentence-initial).
    Returns lowercased entity strings for comparison.
    """
    entities = set()

    # Multi-word capitalized sequences (e.g., "Insider Trading", "North Korea")
    for match in re.finditer(r"(?<!\. )(?<!\A)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", question):
        entity = match.group(1).lower()
        if entity not in _STOP_WORDS:
            entities.add(entity)

    # Also capture single capitalized words that appear mid-sentence
    # (likely proper nouns)
    words = question.split()
    for i, word in enumerate(words):
        clean = re.sub(r"[^a-zA-Z]", "", word)
        if (
            len(clean) >= 3
            and clean[0].isupper()
            and i > 0  # skip sentence-initial
            and clean.lower() not in _STOP_WORDS
        ):
            entities.add(clean.lower())

    return entities


def _detect_qualifiers(question: str) -> list[str]:
    """Detect shared qualifier phrases in a market question."""
    found = []
    for label, pattern in QUALIFIER_PATTERNS:
        if pattern.search(question):
            found.append(label)
    return found


def categorize_position(question: str) -> dict:
    """Categorize a single position by its market question.

    Returns: {
        "category": str,           # broad category name
        "qualifiers": list[str],   # shared qualifier phrases detected
        "keywords": list[str],     # significant keywords
        "entities": list[str],     # extracted entity names
    }
    """
    if not question:
        return {
            "category": "Uncategorized",
            "qualifiers": [],
            "keywords": [],
            "entities": [],
        }

    # Broad category (first match wins)
    category = "Uncategorized"
    for cat_name, patterns in CATEGORY_RULES:
        for pat in patterns:
            if re.search(pat, question, re.IGNORECASE):
                category = cat_name
                break
        if category != "Uncategorized":
            break

    qualifiers = _detect_qualifiers(question)
    keywords = sorted(_extract_keywords(question))
    entities = sorted(_extract_entities(question))

    return {
        "category": category,
        "qualifiers": qualifiers,
        "keywords": keywords,
        "entities": entities,
    }


# ---------------------------------------------------------------------------
# Correlation clustering
# ---------------------------------------------------------------------------

def _keyword_overlap(kw_a: set[str], kw_b: set[str]) -> float:
    """Jaccard similarity between two keyword sets."""
    if not kw_a or not kw_b:
        return 0.0
    intersection = kw_a & kw_b
    union = kw_a | kw_b
    return len(intersection) / len(union)


def build_clusters(
    positions: list[dict],
    categorizations: list[dict],
) -> list[dict]:
    """Build correlation clusters from categorized positions.

    Clustering rules (in priority order):
    1. Positions sharing ANY qualifier phrase -> same cluster.
    2. Positions in the same broad category AND sharing 2+ significant
       keywords or 1+ entity -> same cluster.
    3. Positions sharing 40%+ keyword overlap (Jaccard) -> same cluster.

    Uses union-find to merge transitive connections.

    Returns a list of cluster dicts, each with:
        - cluster_id: int
        - label: str (human-readable cluster name)
        - reason: str (why these are correlated)
        - positions: list[dict] (position records with categorization)
        - total_exposure: float
        - exposure_pct: float (of total portfolio)
    """
    n = len(positions)
    if n == 0:
        return []

    # Union-find structure
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    # Build keyword sets for each position
    kw_sets = [set(c["keywords"]) for c in categorizations]
    ent_sets = [set(c["entities"]) for c in categorizations]

    # Build qualifier sets
    qual_sets = [set(c["qualifiers"]) for c in categorizations]

    # Merge reasons tracking: (i, j) -> reason string
    merge_reasons: dict[tuple[int, int], str] = {}

    # Pass 1: qualifier overlap
    for i in range(n):
        for j in range(i + 1, n):
            shared_quals = qual_sets[i] & qual_sets[j]
            if shared_quals:
                key = (min(i, j), max(i, j))
                merge_reasons[key] = (
                    f"shared qualifier: {', '.join(sorted(shared_quals))}"
                )
                union(i, j)

    # Pass 2: same category + keyword/entity overlap
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue  # already merged

            if categorizations[i]["category"] == categorizations[j]["category"]:
                cat = categorizations[i]["category"]
                if cat == "Uncategorized":
                    continue

                shared_kw = kw_sets[i] & kw_sets[j]
                shared_ent = ent_sets[i] & ent_sets[j]

                if len(shared_kw) >= 2 or len(shared_ent) >= 1:
                    key = (min(i, j), max(i, j))
                    if shared_ent:
                        reason = (
                            f"same category ({cat}), "
                            f"shared entities: {', '.join(sorted(shared_ent))}"
                        )
                    else:
                        reason = (
                            f"same category ({cat}), "
                            f"shared keywords: {', '.join(sorted(shared_kw))}"
                        )
                    merge_reasons[key] = reason
                    union(i, j)

    # Pass 3: high keyword overlap regardless of category
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue  # already merged

            overlap = _keyword_overlap(kw_sets[i], kw_sets[j])
            if overlap >= 0.40:
                key = (min(i, j), max(i, j))
                merge_reasons[key] = (
                    f"keyword overlap {overlap:.0%}"
                )
                union(i, j)

    # Collect clusters
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    clusters = []
    for cluster_id, (_, members) in enumerate(sorted(groups.items())):
        # Compute cluster exposure
        cluster_positions = []
        total_exposure = 0.0

        for idx in members:
            pos = positions[idx]
            cat = categorizations[idx]
            exposure = pos["shares"] * pos["current_price"]
            total_exposure += exposure
            cluster_positions.append({
                **pos,
                "exposure": round(exposure, 4),
                "category": cat["category"],
                "qualifiers": cat["qualifiers"],
            })

        # Determine cluster label
        if len(members) == 1:
            label = categorizations[members[0]]["category"]
        else:
            # Use the most specific reason available
            reasons = []
            for i in members:
                for j in members:
                    if i < j:
                        key = (i, j)
                        if key in merge_reasons:
                            reasons.append(merge_reasons[key])

            # Prefer qualifier-based labels
            qual_reasons = [r for r in reasons if "qualifier" in r]
            cat_reasons = [r for r in reasons if "category" in r]

            if qual_reasons:
                label = qual_reasons[0].replace("shared qualifier: ", "").title()
            elif cat_reasons:
                label = categorizations[members[0]]["category"]
            else:
                label = categorizations[members[0]]["category"]

        reason_summary = "; ".join(sorted(set(
            merge_reasons.get((min(i, j), max(i, j)), "")
            for i in members for j in members if i < j
        ))) or "single position"

        clusters.append({
            "cluster_id": cluster_id,
            "label": label,
            "reason": reason_summary,
            "num_positions": len(members),
            "positions": cluster_positions,
            "total_exposure": round(total_exposure, 4),
        })

    # Sort clusters by exposure descending
    clusters.sort(key=lambda c: c["total_exposure"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# Risk analysis
# ---------------------------------------------------------------------------

def analyze_risk(
    clusters: list[dict],
    total_value: float,
    warn_threshold: float,
    alert_threshold: float,
) -> dict:
    """Analyze correlation risk and generate warnings.

    Returns: {
        "warnings": list[dict],   # {level, cluster_label, exposure_pct, message}
        "diversification_score": int,   # 0-100
        "max_cluster_pct": float,
        "num_clusters": int,
        "num_multi_position_clusters": int,
    }
    """
    warnings = []

    if total_value <= 0:
        return {
            "warnings": [{
                "level": "ALERT",
                "cluster_label": "PORTFOLIO",
                "exposure_pct": 0.0,
                "message": "Portfolio value is zero or negative.",
            }],
            "diversification_score": 0,
            "max_cluster_pct": 0.0,
            "num_clusters": 0,
            "num_multi_position_clusters": 0,
        }

    # Add exposure_pct to each cluster
    for cluster in clusters:
        cluster["exposure_pct"] = round(
            cluster["total_exposure"] / total_value * 100, 2
        )

    max_cluster_pct = 0.0
    multi_pos_clusters = 0

    for cluster in clusters:
        pct = cluster["exposure_pct"]
        frac = cluster["total_exposure"] / total_value

        if pct > max_cluster_pct:
            max_cluster_pct = pct

        if cluster["num_positions"] > 1:
            multi_pos_clusters += 1

        # Only warn about clusters with multiple positions (single positions
        # are checked by the per-market risk limit already)
        if cluster["num_positions"] > 1:
            if frac > alert_threshold:
                warnings.append({
                    "level": "ALERT",
                    "cluster_label": cluster["label"],
                    "exposure_pct": pct,
                    "message": (
                        f"Correlated cluster '{cluster['label']}' has "
                        f"{cluster['num_positions']} positions totaling "
                        f"{pct:.1f}% of portfolio (>{alert_threshold*100:.0f}% limit). "
                        f"Reason: {cluster['reason']}"
                    ),
                })
            elif frac > warn_threshold:
                warnings.append({
                    "level": "WARN",
                    "cluster_label": cluster["label"],
                    "exposure_pct": pct,
                    "message": (
                        f"Correlated cluster '{cluster['label']}' has "
                        f"{cluster['num_positions']} positions totaling "
                        f"{pct:.1f}% of portfolio (>{warn_threshold*100:.0f}% threshold). "
                        f"Reason: {cluster['reason']}"
                    ),
                })

        # INFO for all clusters (including single-position) is handled in output

    # Diversification score: 0-100
    #
    # Scoring method:
    #   - Start at 100 (perfectly diversified)
    #   - Penalize for concentration: subtract based on HHI
    #     (Herfindahl-Hirschman Index) of cluster exposures
    #   - Penalize for multi-position clusters (hidden correlation)
    #
    # HHI ranges from 1/N (perfectly equal) to 1.0 (single cluster).
    # We normalize so that equal-weight positions across N clusters = 100,
    # and single-cluster = 0.

    num_positions = sum(c["num_positions"] for c in clusters)
    num_clusters = len(clusters)

    if num_positions <= 1:
        # 0 or 1 position: diversification is not applicable
        div_score = 100 if num_positions == 0 else 50
    else:
        total_exposure = sum(c["total_exposure"] for c in clusters)
        if total_exposure <= 0:
            div_score = 100
        else:
            # Calculate HHI over cluster weights
            weights = [c["total_exposure"] / total_exposure for c in clusters]
            hhi = sum(w * w for w in weights)

            # Perfect diversification: HHI = 1/N_clusters
            # Full concentration: HHI = 1.0
            if num_clusters > 1:
                min_hhi = 1.0 / num_clusters
                # Normalize: 0 (concentrated) to 1 (diversified)
                normalized = (1.0 - hhi) / (1.0 - min_hhi) if min_hhi < 1.0 else 0.0
            else:
                # All in one cluster
                normalized = 0.0

            # Penalty for having multi-position clusters (hidden correlation)
            # Each multi-position cluster reduces score by up to 10 points
            correlation_penalty = min(
                multi_pos_clusters * 10,
                30,  # cap penalty at 30 points
            )

            div_score = max(0, min(100, int(normalized * 100 - correlation_penalty)))

    return {
        "warnings": warnings,
        "diversification_score": div_score,
        "max_cluster_pct": round(max_cluster_pct, 2),
        "num_clusters": num_clusters,
        "num_multi_position_clusters": multi_pos_clusters,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_human(
    positions: list[dict],
    categorizations: list[dict],
    clusters: list[dict],
    risk: dict,
    portfolio_data: dict,
) -> str:
    """Format results as a human-readable report."""
    lines = []

    # Header
    lines.append("=" * 72)
    lines.append("  CORRELATION TRACKER -- Portfolio Exposure Analysis")
    lines.append("=" * 72)
    lines.append("")
    lines.append(
        f"  Portfolio value:     ${portfolio_data['total_value']:>10,.2f}"
    )
    lines.append(
        f"  Cash balance:        ${portfolio_data['cash_balance']:>10,.2f}"
    )
    lines.append(
        f"  Positions value:     ${portfolio_data['positions_value']:>10,.2f}"
    )
    lines.append(
        f"  Open positions:      {len(positions):>10d}"
    )
    lines.append("")

    # Position list with categories
    lines.append("-" * 72)
    lines.append("  POSITION CATEGORIZATION")
    lines.append("-" * 72)
    lines.append("")

    if not positions:
        lines.append("  No open positions.")
    else:
        for i, (pos, cat) in enumerate(zip(positions, categorizations)):
            exposure = pos["shares"] * pos["current_price"]
            pct = (
                exposure / portfolio_data["total_value"] * 100
                if portfolio_data["total_value"] > 0 else 0
            )
            question = pos.get("market_question") or "Unknown"
            lines.append(f"  [{i+1}] {question}")
            lines.append(
                f"      Side: {pos['side']}  |  "
                f"Shares: {pos['shares']:.2f}  |  "
                f"Price: ${pos['current_price']:.4f}  |  "
                f"Exposure: ${exposure:,.2f} ({pct:.1f}%)"
            )
            lines.append(f"      Category: {cat['category']}")
            if cat["qualifiers"]:
                lines.append(
                    f"      Qualifiers: {', '.join(cat['qualifiers'])}"
                )
            lines.append("")

    # Correlation clusters
    lines.append("-" * 72)
    lines.append("  CORRELATION CLUSTERS")
    lines.append("-" * 72)
    lines.append("")

    if not clusters:
        lines.append("  No clusters to analyze.")
    else:
        for cluster in clusters:
            num = cluster["num_positions"]
            pct = cluster.get("exposure_pct", 0)
            icon = " " if num == 1 else ">"

            lines.append(
                f"  {icon} Cluster: {cluster['label']}  "
                f"({num} position{'s' if num != 1 else ''})  "
                f"Exposure: ${cluster['total_exposure']:,.2f} ({pct:.1f}%)"
            )
            if num > 1:
                lines.append(f"    Correlation reason: {cluster['reason']}")

            for cp in cluster["positions"]:
                question = cp.get("market_question") or "Unknown"
                lines.append(
                    f"    - {cp['side']} ${cp['exposure']:,.2f}  "
                    f"{question[:55]}"
                )
            lines.append("")

    # Risk warnings
    lines.append("-" * 72)
    lines.append("  RISK WARNINGS")
    lines.append("-" * 72)
    lines.append("")

    if not risk["warnings"]:
        lines.append("  No correlation risk warnings. All clusters within limits.")
    else:
        for w in risk["warnings"]:
            lines.append(f"  [{w['level']}] {w['message']}")
        lines.append("")

    # Diversification score
    lines.append("")
    lines.append("-" * 72)
    lines.append("  DIVERSIFICATION SUMMARY")
    lines.append("-" * 72)
    lines.append("")
    score = risk["diversification_score"]

    if score >= 80:
        grade = "EXCELLENT"
    elif score >= 60:
        grade = "GOOD"
    elif score >= 40:
        grade = "MODERATE"
    elif score >= 20:
        grade = "POOR"
    else:
        grade = "CONCENTRATED"

    bar_filled = score // 5
    bar_empty = 20 - bar_filled
    bar = "#" * bar_filled + "-" * bar_empty

    lines.append(f"  Diversification Score:  {score}/100  [{bar}]  {grade}")
    lines.append(f"  Unique clusters:        {risk['num_clusters']}")
    lines.append(
        f"  Correlated clusters:    {risk['num_multi_position_clusters']}"
    )
    lines.append(
        f"  Largest cluster:        {risk['max_cluster_pct']:.1f}% of portfolio"
    )
    lines.append("")
    lines.append(
        "  Score = 100 (perfect diversification) to 0 (fully concentrated)."
    )
    lines.append(
        "  Based on HHI of cluster exposures with penalties for hidden correlation."
    )
    lines.append("")

    return "\n".join(lines)


def build_json_output(
    positions: list[dict],
    categorizations: list[dict],
    clusters: list[dict],
    risk: dict,
    portfolio_data: dict,
) -> dict:
    """Build the complete JSON output structure."""
    categorized_positions = []
    for pos, cat in zip(positions, categorizations):
        exposure = pos["shares"] * pos["current_price"]
        pct = (
            exposure / portfolio_data["total_value"] * 100
            if portfolio_data["total_value"] > 0 else 0
        )
        categorized_positions.append({
            "token_id": pos["token_id"],
            "market_question": pos.get("market_question") or "Unknown",
            "side": pos["side"],
            "shares": pos["shares"],
            "avg_entry": pos["avg_entry"],
            "current_price": pos["current_price"],
            "exposure": round(exposure, 4),
            "exposure_pct": round(pct, 2),
            "category": cat["category"],
            "qualifiers": cat["qualifiers"],
            "keywords": cat["keywords"],
            "entities": cat["entities"],
        })

    # Clean up cluster positions for JSON (remove sqlite Row artifacts)
    json_clusters = []
    for cluster in clusters:
        json_cluster = {
            "cluster_id": cluster["cluster_id"],
            "label": cluster["label"],
            "reason": cluster["reason"],
            "num_positions": cluster["num_positions"],
            "total_exposure": cluster["total_exposure"],
            "exposure_pct": cluster.get("exposure_pct", 0),
            "positions": [
                {
                    "token_id": p["token_id"],
                    "market_question": p.get("market_question") or "Unknown",
                    "side": p["side"],
                    "exposure": p["exposure"],
                    "category": p["category"],
                    "qualifiers": p.get("qualifiers", []),
                }
                for p in cluster["positions"]
            ],
        }
        json_clusters.append(json_cluster)

    return {
        "portfolio": {
            "total_value": portfolio_data["total_value"],
            "cash_balance": portfolio_data["cash_balance"],
            "positions_value": portfolio_data["positions_value"],
            "num_open_positions": len(positions),
        },
        "positions": categorized_positions,
        "clusters": json_clusters,
        "risk": {
            "warnings": risk["warnings"],
            "diversification_score": risk["diversification_score"],
            "max_cluster_pct": risk["max_cluster_pct"],
            "num_clusters": risk["num_clusters"],
            "num_multi_position_clusters": risk["num_multi_position_clusters"],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Detect correlated exposure in the paper trading portfolio. "
            "Groups positions by topic and warns when hidden concentration "
            "exceeds risk thresholds."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --json
  %(prog)s --threshold 0.10 --portfolio aggressive
  %(prog)s --portfolio-db /path/to/portfolio.db --json
        """,
    )
    parser.add_argument(
        "--portfolio-db",
        type=str,
        default=str(DB_PATH),
        help=f"Path to the portfolio SQLite database (default: {DB_PATH})",
    )
    parser.add_argument(
        "--portfolio",
        type=str,
        default="default",
        help="Portfolio name (default: 'default')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help=(
            "Correlation warning threshold as fraction of portfolio "
            "(default: 0.15 = 15%%). ALERT triggers at the max-single-market "
            "limit of 20%%."
        ),
    )

    args = parser.parse_args()

    warn_threshold = args.threshold
    alert_threshold = MAX_SINGLE_MARKET_PCT  # 0.20 from CLAUDE.md

    # Validate thresholds
    if not (0.0 < warn_threshold <= 1.0):
        print(
            "ERROR: --threshold must be between 0 and 1.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load portfolio
    conn = _open_db(args.portfolio_db)
    try:
        portfolio_data = load_portfolio(conn, args.portfolio)
    finally:
        conn.close()

    positions = portfolio_data["positions"]

    if not positions:
        if args.json:
            print(json.dumps({
                "portfolio": {
                    "total_value": portfolio_data["total_value"],
                    "cash_balance": portfolio_data["cash_balance"],
                    "positions_value": 0,
                    "num_open_positions": 0,
                },
                "positions": [],
                "clusters": [],
                "risk": {
                    "warnings": [],
                    "diversification_score": 100,
                    "max_cluster_pct": 0,
                    "num_clusters": 0,
                    "num_multi_position_clusters": 0,
                },
            }, indent=2))
        else:
            print("No open positions in portfolio. Nothing to analyze.")
        return

    # Categorize each position
    categorizations = [
        categorize_position(pos.get("market_question") or "")
        for pos in positions
    ]

    # Build correlation clusters
    clusters = build_clusters(positions, categorizations)

    # Analyze risk
    risk = analyze_risk(
        clusters,
        portfolio_data["total_value"],
        warn_threshold,
        alert_threshold,
    )

    # Output
    if args.json:
        output = build_json_output(
            positions, categorizations, clusters, risk, portfolio_data
        )
        print(json.dumps(output, indent=2))
    else:
        report = format_human(
            positions, categorizations, clusters, risk, portfolio_data
        )
        print(report)


if __name__ == "__main__":
    main()
