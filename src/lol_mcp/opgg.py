"""Cached, read-only OP.GG Mayhem data without the remote MCP's tier filter."""

from __future__ import annotations

import copy
import html
import math
import json
import math
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = "https://op.gg/lol/modes/aram-mayhem"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "logs" / "opgg_cache"
MAX_PAGE_BYTES = 8 * 1024 * 1024
CACHE_VERSION = 1


class OpggUnavailable(RuntimeError):
    """Raised for unavailable pages or an unrecognized upstream schema."""


def champion_slug(champion: str) -> str:
    """Accept an English champion name or slug, never an arbitrary URL."""
    if not isinstance(champion, str) or not re.fullmatch(
        r"[A-Za-z .'-]{2,40}", champion
    ):
        raise ValueError("Use an English champion name or OP.GG slug, such as Samira.")
    slug = re.sub(r"[^a-z]", "", champion.lower())
    return {"nunuwillump": "nunu", "nunuandwillump": "nunu"}.get(slug, slug)


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())


class _PageParser(HTMLParser):
    """Read public metadata, Flight strings, and rendered item tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical = ""
        self.description = ""
        self.scripts: list[str] = []
        self.script: list[str] | None = None
        self.tables: list[dict[str, Any]] = []
        self.table: dict[str, Any] | None = None
        self.in_header = False
        self.row: list[dict[str, Any]] | None = None
        self.div_classes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "link" and attr.get("rel") == "canonical":
            self.canonical = attr.get("href") or ""
        if tag == "meta" and attr.get("name") == "description":
            self.description = attr.get("content") or ""
        if tag == "script":
            self.script = []
        if tag == "table":
            self.table = {"header": "", "rows": []}
        elif self.table is not None:
            if tag == "thead":
                self.in_header = True
            elif tag == "tr":
                self.row = []
                self.div_classes = []
            elif tag == "div":
                self.div_classes.append(attr.get("class") or "")
            elif tag == "img" and self.row is not None:
                item_id = re.search(r"/item/(\d+)\.png(?:[?]|$)", attr.get("src") or "")
                if item_id:
                    self.row.append(
                        {
                            "id": int(item_id[1]),
                            "name": attr.get("alt"),
                            "count": 1,
                        }
                    )

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if self.table is None:
            return
        if tag == "thead":
            self.in_header = False
        elif tag == "tr":
            if self.row:
                self.table["rows"].append(self.row)
            self.row = None
            self.div_classes = []
        elif tag == "div" and self.div_classes:
            self.div_classes.pop()
        elif tag == "table":
            self.tables.append(self.table)
            self.table = None
            self.row = None
            self.in_header = False

    def handle_data(self, data: str) -> None:
        if self.script is not None:
            self.script.append(data)
        if self.table is not None and self.in_header:
            self.table["header"] += data
        if (
            self.row
            and self.div_classes
            and "absolute" in self.div_classes[-1].split()
            and data.strip().isdigit()
        ):
            self.row[-1]["count"] = int(data.strip())


def _parse_page(page: str, slug: str, section: str) -> tuple[_PageParser, str]:
    parser = _PageParser()
    parser.feed(page)
    expected = f"{BASE_URL}/{slug}/{section}"
    if parser.canonical.rstrip("/") != expected:
        raise OpggUnavailable("unexpected_page")
    patch = re.search(r"\bPatch\s+(\d+\.\d+)\b", parser.description)
    if not patch:
        raise OpggUnavailable("missing_patch")
    return parser, patch[1]


def _flight_data(parser: _PageParser) -> str:
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    for script in parser.scripts:
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                value, _ = decoder.raw_decode(script, match.end())
            except ValueError:
                continue
            if (
                isinstance(value, list)
                and len(value) == 2
                and value[0] == 1
                and isinstance(value[1], str)
            ):
                chunks.append(value[1])
    return "".join(chunks)


def parse_augments(page: str, slug: str) -> dict[str, Any]:
    """Keep every tier and expose metrics literally, without inventing win rates."""
    parser, patch = _parse_page(page, slug, "augments")
    flight = _flight_data(parser)
    decoder = json.JSONDecoder()
    candidates: list[list[dict[str, Any]]] = []
    for match in re.finditer(r'"data"\s*:\s*(?=\[)', flight):
        try:
            rows, _ = decoder.raw_decode(flight, match.end())
        except ValueError:
            continue
        if (
            isinstance(rows, list)
            and rows
            and isinstance(rows[0], dict)
            and {"id", "tier", "performance", "popular"} <= rows[0].keys()
        ):
            candidates.append(rows)
    if not candidates or any(rows != candidates[0] for rows in candidates[1:]):
        raise OpggUnavailable("augment_schema_changed")
    result = []
    ids: set[int] = set()
    for row in candidates[0]:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] in ids
            or type(row.get("tier")) is not int
            or not {"performance", "popular"} <= row.keys()
            or (row.get("rarity") is not None and type(row["rarity"]) is not int)
            or (row.get("key") is not None and not isinstance(row["key"], str))
        ):
            raise OpggUnavailable("augment_schema_changed")
        for field in ("performance", "popular"):
            metric = row.get(field)
            if metric is not None and (
                type(metric) not in (int, float) or not math.isfinite(metric)
            ):
                raise OpggUnavailable("augment_schema_changed")
        ids.add(row["id"])
        result.append(
            {
                "id": row["id"],
                "key": row.get("key"),
                "name": _plain_text(row.get("name")),
                "rarity": row.get("rarity"),
                "tier": row["tier"],
                "performance": row.get("performance"),
                "popular": row.get("popular"),
                "description": _plain_text(row.get("desc")),
            }
        )
    return {"patch": patch, "augments": result}


def parse_champion_tiers(page: str) -> dict[str, Any]:
    """Read the champion catalog, never a nested augment-to-champion table."""
    parser = _PageParser()
    parser.feed(page)
    if parser.canonical.rstrip("/") != BASE_URL:
        raise OpggUnavailable("unexpected_page")
    flight = _flight_data(parser)
    patches = set(re.findall(r'"patch"\s*:\s*"(\d+\.\d+)"', flight))
    if len(patches) != 1:
        raise OpggUnavailable("missing_or_ambiguous_patch")
    candidates = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"champions"\s*:\s*(?=\[)', flight):
        try:
            rows, _ = decoder.raw_decode(flight, match.end())
        except ValueError:
            continue
        if (isinstance(rows, list) and rows and isinstance(rows[0], dict)
                and {"key", "name", "champion_id", "tier", "rank"} <= rows[0].keys()):
            candidates.append(rows)
    if not candidates or any(rows != candidates[0] for rows in candidates[1:]):
        raise OpggUnavailable("champion_tier_schema_changed")
    result, ids, slugs = [], set(), set()
    for row in candidates[0]:
        if not isinstance(row, dict):
            raise OpggUnavailable("champion_tier_schema_changed")
        identifier, slug = row.get("champion_id"), row.get("key")
        tier, rank = row.get("tier"), row.get("rank")
        if (type(identifier) is not int or identifier <= 0 or identifier in ids
                or not isinstance(slug, str) or not re.fullmatch(r"[a-z]+", slug)
                or slug in slugs or not isinstance(row.get("name"), str)
                or not {"tier", "rank"} <= row.keys()
                or (tier is not None and (type(tier) is not int or not 0 <= tier <= 5))
                or (rank is not None and (type(rank) is not int or rank < 1))):
            raise OpggUnavailable("champion_tier_schema_changed")
        ids.add(identifier)
        slugs.add(slug)
        result.append({
            "championId": identifier, "champion": slug,
            "championName": _plain_text(row["name"]),
            "championTier": tier,
            "tierLabel": ("OP" if tier == 0 else f"T{tier}") if tier is not None else None,
            "championRank": rank,
        })
    return {"patch": next(iter(patches)), "champions": result}


def parse_build(page: str, slug: str) -> dict[str, Any]:
    """Return the displayed build alternatives in source order, not a recommendation."""
    parser, patch = _parse_page(page, slug, "build")
    names = {
        "Starter items": "starterItems",
        "Core builds": "coreBuilds",
        "Boots": "boots",
    }
    builds: dict[str, Any] = {}
    for table in parser.tables:
        name = names.get(" ".join(table["header"].split()))
        if name:
            if name in builds or not table["rows"]:
                raise OpggUnavailable("build_schema_changed")
            builds[name] = table["rows"]
    if set(builds) != set(names.values()):
        raise OpggUnavailable("build_schema_changed")
    return {"patch": patch, **builds}


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MIRA.CORD-Mayhem-MCP/1 (+https://github.com/Milabo-HRH/miracord)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=6) as response:
        if response.geturl().rstrip("/") != url:
            raise OpggUnavailable("unexpected_redirect")
        body = response.read(MAX_PAGE_BYTES + 1)
    if len(body) > MAX_PAGE_BYTES:
        raise OpggUnavailable("page_too_large")
    return body.decode("utf-8")


class OpggMayhemClient:
    """Single-flight per page, persistent TTL cache, and bounded stale fallback."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
        ttl_seconds: float = 21600,
        max_stale_seconds: float = 86400,
        fetch: Callable[[str], str] = _fetch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = cache_dir
        self.ttl = ttl_seconds
        self.max_stale = max_stale_seconds
        self.fetch = fetch
        self.clock = clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._retry_after: dict[str, float] = {}

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        cached = self._cache.get(key)
        if cached is None and self.cache_dir is not None:
            try:
                value = json.loads(
                    (self.cache_dir / f"{key}.json").read_text(encoding="utf-8")
                )
                if (
                    value.get("version") == CACHE_VERSION
                    and value.get("key") == key
                    and type(value.get("fetchedAtEpoch")) in (int, float)
                    and math.isfinite(value["fetchedAtEpoch"])
                    and isinstance(value.get("data"), dict)
                    and isinstance(value["data"].get("patch"), str)
                ):
                    cached = value
                    self._cache[key] = value
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        return cached

    def _write_cache(self, key: str, value: dict[str, Any]) -> None:
        self._cache[key] = value
        if self.cache_dir is not None:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                # A unique temporary path also tolerates multiple MCP processes.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.cache_dir,
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(value, stream, ensure_ascii=False)
                try:
                    temporary.replace(self.cache_dir / f"{key}.json")
                finally:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _get(self, champion: str, section: str) -> dict[str, Any]:
        slug = champion_slug(champion)
        catalog = section == "champion_tiers"
        url = BASE_URL if catalog else f"{BASE_URL}/{slug}/{section}"
        key = "global-champion-tiers" if catalog else f"{slug}-{section}"
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = self.clock()
            cached = self._read_cache(key)
            age = max(0, now - cached["fetchedAtEpoch"]) if cached else math.inf
            hit = cached is not None and age < self.ttl
            stale = False
            reason = None
            if not hit:
                try:
                    if now < self._retry_after.get(key, 0):
                        raise OpggUnavailable("refresh_backoff")
                    page = self.fetch(url)
                    data = (
                        parse_champion_tiers(page) if catalog else
                        (parse_augments if section == "augments" else parse_build)(page, slug)
                    )
                    cached = {
                        "version": CACHE_VERSION,
                        "key": key,
                        "fetchedAtEpoch": self.clock(),
                        "data": data,
                    }
                    self._write_cache(key, cached)
                    self._retry_after.pop(key, None)
                    age = 0
                except (
                    OSError,
                    ValueError,
                    urllib.error.URLError,
                    OpggUnavailable,
                ) as exc:
                    self._retry_after.setdefault(key, now + 60)
                    if now >= self._retry_after[key]:
                        self._retry_after[key] = now + 60
                    reason = (
                        str(exc) if isinstance(exc, OpggUnavailable) else "fetch_failed"
                    )
                    if cached is None or age > self.max_stale:
                        return {
                            "status": "unavailable",
                            "source": {"url": url},
                            "reason": reason,
                            "message": "OP.GG Mayhem data unavailable; no ARAM fallback.",
                        }
                    stale = True
                    hit = True
            assert cached is not None
            return {
                "status": "ok",
                **({} if catalog else {"champion": slug}),
                "mode": "aram-mayhem",
                "source": {
                    "name": "OP.GG public Mayhem page",
                    "url": url,
                    "patch": cached["data"]["patch"],
                    "fetchedAt": datetime.fromtimestamp(
                        cached["fetchedAtEpoch"], timezone.utc
                    ).isoformat(),
                },
                "cache": {
                    "hit": hit,
                    "stale": stale,
                    "ageSeconds": round(age, 1),
                    "refreshError": reason,
                },
                **copy.deepcopy(cached["data"]),
            }

    def get_champion_tier(self, champion: str) -> dict[str, Any]:
        """Filter one champion from the shared cached Mayhem champion catalog."""
        slug = champion_slug(champion)
        result = self._get(slug, "champion_tiers")
        if result["status"] != "ok":
            return result
        rows = result.pop("champions")
        record = next((row for row in rows if row["champion"] == slug), None)
        result.update(champion=slug, totalChampions=len(rows), tierType="champion")
        if record is None:
            result.update(status="not_found", reason="champion_not_in_catalog")
        else:
            result.update(record)
            result["interpretation"] = (
                "championTier/tierLabel rate the CHAMPION in OP.GG ARAM Mayhem, "
                "not any augment. championRank is the source champion ranking, "
                "not a player rank. Report the source rating literally. "
                "No win rate, sample size, or rank-bracket filter is provided. "
                "Null fields mean unrated/unknown; do not infer a tier."
            )
        return result

    def get_build(self, champion: str) -> dict[str, Any]:
        result = self._get(champion, "build")
        if result["status"] == "ok":
            result["interpretation"] = (
                "Displayed build alternatives, not proof of optimality; no win rates supplied."
            )
        return result

    def get_augments(
        self,
        champion: str,
        *,
        augment_ids: list[int] | None = None,
        query: str = "",
        limit: int = 12,
        offset: int = 0,
        include_descriptions: bool = False,
        rarity: str | None = None,
        sort_by: str = "source",
        min_popular: float | None = None,
        all_matches: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200 or offset < 0 or len(query) > 100:
            raise ValueError(
                "Use limit 1..200, offset >= 0, and a query of at most 100 characters."
            )
        if augment_ids is not None and (
            len(augment_ids) > 200
            or any(type(i) is not int or i <= 0 for i in augment_ids)
        ):
            raise ValueError("Use at most 200 positive integer augment IDs.")
        rarity_codes = {"silver": 1, "gold": 4, "prismatic": 8}
        if rarity is not None and rarity not in rarity_codes:
            raise ValueError("rarity must be silver, gold or prismatic")
        if sort_by not in {"source", "performance"}:
            raise ValueError("sort_by must be source or performance")
        if min_popular is not None and (type(min_popular) not in (int, float)
                or not math.isfinite(min_popular) or not 0 <= min_popular <= 100):
            raise ValueError("min_popular must be finite and between 0 and 100")
        # Preserve explicit identity/effect lookups, but default candidate lists
        # to a positive published usage floor. This does not certify availability.
        threshold = min_popular if min_popular is not None else (0 if query.strip() or augment_ids is not None else 0.01)
        result = self._get(champion, "augments")
        if result["status"] != "ok":
            return result
        # Preserve original list positions across name/ID filtering without
        # changing the cached records or turning source order into a rating.
        all_rows = [
            {**row, "sourceOrder": index + 1}
            for index, row in enumerate(result.pop("augments"))
        ]
        # Derive ranks within each rarity BEFORE query/page
        # filters. Source order and raw performance are not percentiles.
        rank_pool = [row for row in all_rows
                     if isinstance(row.get("name"), str) and row["name"].strip()
                     and type(row.get("popular")) in (int, float)
                     and math.isfinite(row["popular"]) and row["popular"] >= .01
                     and type(row.get("performance")) in (int, float)
                     and math.isfinite(row["performance"])
                     and row.get("rarity") in (1, 4, 8)]
        for row in rank_pool:
            score = row["performance"]
            peers = [r for r in rank_pool if r["rarity"] == row["rarity"]]
            row["performanceRank"] = 1 + sum(r["performance"] > score for r in peers)
            row["rankedAugmentCount"] = len(peers)
            row["rankTiedCount"] = sum(r["performance"] == score for r in peers)
            row["rankScope"] = "same_champion_same_rarity_positive_usage_snapshot"
        rows = all_rows
        if augment_ids is not None:
            rows = [row for row in rows if row["id"] in augment_ids]
            result["missingIds"] = sorted(
                set(augment_ids) - {row["id"] for row in all_rows}
            )
        if query.strip():
            needle = query.strip().casefold()
            rows = [
                row
                for row in rows
                if needle in (row["name"] or "").casefold()
                or needle in (row["key"] or "").casefold()
            ]
        if query.strip():
            exact = [row for row in rows if needle in {(row.get("name") or "").casefold(), (row.get("key") or "").casefold()}]
            if exact:
                rows = exact
        if rarity is not None:
            rows = [row for row in rows if row.get("rarity") == rarity_codes[rarity]]
        before_quality = len(rows)
        if threshold > 0 or sort_by == "performance":
            rows = [row for row in rows if isinstance(row.get("name"), str) and row["name"].strip()
                    and type(row.get("popular")) in (int, float) and math.isfinite(row["popular"])
                    and row["popular"] > 0 and row["popular"] >= threshold
                    and type(row.get("performance")) in (int, float) and math.isfinite(row["performance"])]
        for row in rows:
            row["availability"] = "unverified"
            if not row.get("popular"):
                row["recommendationWarning"] = "No positive published popularity; identity/history only, not a strongest recommendation."
        if sort_by == "performance":
            rows.sort(key=lambda row: (row.get("performance") is not None, row.get("performance") or 0), reverse=True)
        selected = rows if all_matches else rows[offset : offset + limit]
        if not include_descriptions:
            selected = [
                {key: value for key, value in row.items() if key != "description"}
                for row in selected
            ]
        result.update(
            {
                "totalAvailable": len(all_rows),
                "totalMatched": len(rows),
                "rarityFilter": rarity, "sortBy": sort_by,
                "minPopular": threshold, "excludedByQuality": before_quality-len(rows),
                "availabilityWarning": "Page patch and positive popularity do not certify current eligibility; retained/disabled entries may exist. Do not contradict a user reporting removal without independent verification.",
                "augments": selected,
                "nextOffset": None if all_matches else (offset + limit if offset + limit < len(rows) else None),
                "allMatches": all_matches,
                "interpretation": "performanceRank is a locally derived descending competition rank (1 is best, ties share rank) among this champion's same-rarity source snapshot, with finite scores and popular>=0.01. rankedAugmentCount is that pool size, not the offered options or confirmed eligible pool. Rank is computed before name/pagination filters, separately for silver/gold/prismatic; unknown rarity has no rank. Never compare ranks across different colors. Prefer saying 同色强化排第N/共M个有数据的强化; do not convert performance into percentile or win rate. Raw performance is retained for audit. sourceOrder is only original list position. Tier is not rarity. No sample counts supplied.",
            }
        )
        return result
