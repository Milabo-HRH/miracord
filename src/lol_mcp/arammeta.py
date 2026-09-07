"""Read-only, revision-pinned public arammeta aggregates and item identities."""

import copy
import json
import math
import re
import threading
import urllib.request
import uuid
from pathlib import Path

DEFAULT_REVISION = "025ac7c688b3cdd9109d40e8fa35bd800836b9b7"
ROOT = "https://raw.githubusercontent.com/Lanternko/ARAM-Mayhem-Database"


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MIRA.CORD/arammeta"}), timeout=8) as response:
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError("resource_too_large")
    return json.loads(raw)


def norm(value):
    return re.sub(r"[\s'’._-]", "", str(value)).casefold()


class AramMetaClient:
    def __init__(self, *, revision=DEFAULT_REVISION, cache_dir=Path("logs/arammeta_cache"), fetch=fetch_json):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("An exact repository commit is required")
        self.revision, self.cache_dir, self.fetch = revision, cache_dir, fetch
        self._memory, self._lock = {}, threading.RLock()

    def _load(self, resource):
        if not re.fullmatch(r"(?:tier-list|names-zh-cn|champions/[0-9]+)\.json", resource):
            raise ValueError("invalid_resource")
        with self._lock:
            if resource in self._memory:
                return copy.deepcopy(self._memory[resource])
            path = self.cache_dir / self.revision / resource if self.cache_dir is not None else None
            value = None
            if path is not None and path.exists():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            if value is None:
                value = self.fetch(f"{ROOT}/{self.revision}/docs/api/{resource}")
                if not isinstance(value, dict):
                    raise ValueError("invalid_resource_schema")
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                        temp.replace(path)
                    finally:
                        temp.unlink(missing_ok=True)
            if not isinstance(value, dict):
                raise ValueError("invalid_resource_schema")
            self._memory[resource] = value
            return copy.deepcopy(value)

    def _source(self, index, names):
        return {"name": "arammeta", "repository": "https://github.com/Lanternko/ARAM-Mayhem-Database",
                "revision": self.revision, "patch": index.get("patch_prefix"),
                "dataVersion": index.get("detailVersion"), "localizationVersion": names.get("ver"),
                "queue": 2400, "snapshotPolicy": "pinned_revision_not_live_latest"}

    def _entities(self, index, names, kind):
        source = index["itemLut"] if kind == "item" else index["augs"]
        translated = names["items"] if kind == "item" else names["augs"]
        result = []
        for key, row in source.items():
            result.append({"id": int(key), "namespace": f"arammeta_mayhem_{kind}",
                           "rarity": {"kSilver": "silver", "kGold": "gold", "kPrismatic": "prismatic"}.get(row.get("rarity")),
                           "nameEn": row.get("e" if kind == "item" else "name_en"),
                           "nameZh": translated.get(key, {}).get("n") if isinstance(translated.get(key), dict) else translated.get(key),
                           "nameTw": row.get("z" if kind == "item" else "name_zh"),
                           "descriptionEn": row.get("de" if kind == "item" else "desc_en"),
                           "descriptionZh": names.get("itemDescs", {}).get(key) if kind == "item" else (translated.get(key, {}).get("d") if isinstance(translated.get(key), dict) else None),
                           "descriptionTw": row.get("dz" if kind == "item" else "desc_zh")})
        return result

    def _match(self, entities, query, ids):
        aliases = {"饮血": "Bloodthirster", "破败": "Blade of The Ruined King", "狂妄": "Hubris"}
        needle = norm(aliases.get(query, query))
        if ids is not None:
            entities = [r for r in entities if r["id"] in ids]
        if needle:
            exact = [r for r in entities if any(needle == norm(r.get(k, "")) for k in ("id", "nameEn", "nameZh", "nameTw"))]
            entities = exact or [r for r in entities if any(needle in norm(r.get(k) or "") for k in ("nameEn", "nameZh", "nameTw"))]
        return entities

    def lookup_item(self, query):
        try:
            if not isinstance(query, str) or not query.strip() or len(query) > 100:
                return {"status": "invalid_arguments"}
            index, names = self._load("tier-list.json"), self._load("names-zh-cn.json")
            rows = self._match(self._entities(index, names, "item"), query, None)
            return {"status": "ok" if len(rows) == 1 else "ambiguous" if rows else "not_found",
                    "source": self._source(index, names), "items": rows[:8],
                    "note": "Item identity/effects, not an offered augment or a recommendation. Localization may be older than stats."}
        except Exception:
            return {"status": "unavailable", "reason": "arammeta_fetch_or_schema_error"}

    def get_stats(self, champion, kind, query="", ids=None, limit=6, rarity=None):
        try:
            if rarity is not None and (kind != "augment" or rarity not in {"silver", "gold", "prismatic"}):
                return {"status": "invalid_arguments"}
            if kind not in {"champion", "item", "augment"} or type(limit) is not int or not 1 <= limit <= 12:
                return {"status": "invalid_arguments"}
            if not isinstance(champion, str) or not isinstance(query, str) or len(query) > 100:
                return {"status": "invalid_arguments"}
            if ids is not None and (not isinstance(ids, list) or len(ids) > 12 or any(type(i) is not int or i <= 0 for i in ids)):
                return {"status": "invalid_arguments"}
            index, names = self._load("tier-list.json"), self._load("names-zh-cn.json")
            alias = {"芸阿娜": "Yunara", "冰鸟": "Anivia", "螳螂": "Khazix"}.get(champion, champion)
            matches = [(cid, r) for cid, r in index["champs"].items()
                       if norm(alias) in {norm(cid), norm(r.get("alias")), norm(r.get("name_en")),
                                          norm(r.get("name_zh")), norm(names["champs"].get(cid))}]
            if len(matches) != 1:
                return {"status": "not_found", "reason": "champion_name_unresolved"}
            cid, champ = matches[0]
            if kind == "champion":
                if query or ids is not None or rarity is not None:
                    return {"status": "invalid_arguments", "reason": "overall_champion_stats_have_no_item_or_augment_filter"}
                metrics = {k: champ[k] for k in ("g", "wr", "rawWr") if k in champ}
                wr = metrics.get("rawWr", metrics.get("wr"))
                if type(wr) not in (int, float) or not math.isfinite(wr) or not 0 <= wr <= 1 or type(metrics.get("g")) is not int or metrics["g"] <= 0:
                    return {"status": "not_found", "reason": "no_valid_overall_statistics"}
                return {"status": "ok", "kind": "champion", "champion": champ.get("alias"),
                        "championId": int(cid), "source": self._source(index, names), "metrics": metrics,
                        "winRatePercent": round(wr*100, 2), "winRateField": "rawWr" if "rawWr" in metrics else "wr",
                        "interpretation": "Overall champion historical game statistic, not conditioned on equipment/augment; not this match's win probability. rawWr is raw when supplied; wr may be adjusted."}
            detail = self._load(f"champions/{cid}.json")
            entities = self._match(self._entities(index, names, kind), query, ids)
            if rarity is not None:
                entities = [r for r in entities if r.get("rarity") == rarity]
            selected = {r["id"]: r for r in entities}
            rows = []
            groups = [detail.get("singleItems", {})] if kind == "item" else [champ.get("top", {}), detail.get("bot", {})]
            seen = set()
            for group in groups:
                for values in group.values():
                    if not isinstance(values, list):
                        continue
                    for row in values:
                        key = row.get("id") if kind == "augment" else row.get("slug")
                        if not str(key).isdigit():
                            continue
                        key = int(key)
                        if key not in selected or key in seen:
                            continue
                        if (type(row.get("g")) is not int or row["g"] < 0
                                or type(row.get("wr")) not in (int, float)
                                or not math.isfinite(row["wr"]) or not 0 <= row["wr"] <= 1):
                            raise ValueError("invalid_statistic")
                        seen.add(key)
                        rows.append({"entity": selected[key], "metrics": {k: row[k] for k in
                                     ("g", "wr", "rawWr", "score", "lift", "lcb", "pick", "slots") if k in row}})
            rows.sort(key=lambda r: r["metrics"].get("wr", -1), reverse=True)
            return {"status": "ok" if rows else "not_found", "reason": None if rows else "no_published_stats",
                    "source": self._source(index, names), "champion": champ.get("alias"), "championId": int(cid),
                    "missingIds": sorted(set(ids or []) - set(selected)),
                    "kind": kind, "rarityFilter": rarity, "records": rows[:limit], "totalMatched": len(rows),
                    "knownEntities": [{k: v for k, v in e.items() if not k.startswith("description")} for e in entities[:12]],
                    "coverage": "Published top/bottom selections, not exhaustive. Absence is not an in-game restriction.",
                    "metricDefinitions": "g=source game count; wr=publisher win-rate statistic (may be adjusted); rawWr only when supplied. score/lift are NOT OP.GG performance. Historical association is not causal purchase benefit or this match's win probability."}
        except Exception:
            return {"status": "unavailable", "reason": "arammeta_fetch_or_schema_error"}
