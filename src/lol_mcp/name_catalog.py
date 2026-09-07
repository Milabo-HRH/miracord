"""Offline, source-backed champion and augment names with exact alias matching.

Refresh is explicit through scripts/refresh_name_catalog.py. Constructing or
querying this class never performs a network request.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from typing import Any

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[2] / "logs/name_catalog/catalog.json"
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_CHAMPIONS = 512
MAX_AUGMENTS = 4096
SCHEMA_VERSION = 1
TENCENT_URL = "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
RIOT_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
TENCENT_AUGMENTS_URL = "https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json"


def normalize_name(value: Any) -> str:
    """Fold case, spaces and punctuation, without transliteration or guessing."""
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    if any(unicodedata.category(char)[0] in {"C", "S"} for char in value):
        return ""
    letters = "".join(char for char in value if char.isalnum())
    # Some source-defined names consist entirely of punctuation (e.g. ???).
    # Keep their exact NFKC punctuation spelling instead of erasing the name.
    return letters or "".join(char for char in value if not char.isspace())


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _strings(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("invalid_alias_list")
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in values):
        raise ValueError("invalid_alias")
    return list(dict.fromkeys(values))


def _validate(data: Any) -> dict:
    if not isinstance(data, dict) or data.get("schemaVersion", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError("unsupported_catalog_schema")
    champions, augments = data.get("champions", []), data.get("augments", [])
    if not isinstance(champions, list) or len(champions) > MAX_CHAMPIONS:
        raise ValueError("invalid_champion_list")
    if not isinstance(augments, list) or len(augments) > MAX_AUGMENTS:
        raise ValueError("invalid_augment_list")
    for records, kind in ((champions, "champion"), (augments, "augment")):
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("invalid_catalog_record")
            identifier = record.get(f"{kind}Id")
            if type(identifier) is not int or identifier <= 0 or identifier in seen:
                raise ValueError("invalid_or_duplicate_identifier")
            seen.add(identifier)
            for field in ("nameZh", "nameEn"):
                if not isinstance(record.get(field), str) or len(record[field]) > 128:
                    raise ValueError("invalid_name")
            if not (record["nameZh"] or record["nameEn"]):
                raise ValueError("missing_name")
            _strings(record.get("aliasesZh", []))
            _strings(record.get("aliases", []))
            names_by_locale = record.get("namesByLocale", {})
            aliases_by_locale = record.get("aliasesByLocale", {})
            if not isinstance(names_by_locale, dict) or len(names_by_locale) > 8:
                raise ValueError("invalid_locale_names")
            for locale, name in names_by_locale.items():
                if (not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", locale)
                        or not isinstance(name, str) or not name or len(name) > 128):
                    raise ValueError("invalid_locale_name")
            if not isinstance(aliases_by_locale, dict) or not aliases_by_locale.keys() <= names_by_locale.keys():
                raise ValueError("invalid_locale_aliases")
            for aliases in aliases_by_locale.values():
                _strings(aliases)
            names_by_region = record.get("namesByRegion", {})
            if not isinstance(names_by_region, dict) or len(names_by_region) > 8:
                raise ValueError("invalid_region_names")
            for region, name in names_by_region.items():
                if (not isinstance(region, str) or not re.fullmatch(r"[A-Z]{2,3}", region)
                        or not isinstance(name, str) or not name or len(name) > 128):
                    raise ValueError("invalid_region_name")
            if kind == "champion":
                if not isinstance(record.get("titleZh"), str) or len(record["titleZh"]) > 128:
                    raise ValueError("invalid_title")
                if not isinstance(record.get("internalName"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,63}", record["internalName"]):
                    raise ValueError("invalid_internal_name")
                if not isinstance(record.get("opggSlug"), str) or not re.fullmatch(r"[a-z]{2,64}", record["opggSlug"]):
                    raise ValueError("invalid_opgg_slug")
            elif (record.get("namespace") != "aram_mayhem"
                  or not isinstance(record.get("apiName"), str)
                  or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,127}", record["apiName"])):
                raise ValueError("invalid_augment_namespace_or_key")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("invalid_metadata")
    expected_hash = metadata.get("contentHash")
    if expected_hash and expected_hash != content_hash({"champions": champions, "augments": augments}):
        raise ValueError("catalog_hash_mismatch")
    return copy.deepcopy({"schemaVersion": SCHEMA_VERSION, "metadata": metadata,
                          "champions": champions, "augments": augments})


class NameCatalog:
    """Load a bounded local snapshot and return unambiguous exact matches only."""

    def __init__(self, cache_path: Path | str = DEFAULT_CACHE_PATH, *, data: dict | None = None):
        self.cache_path = Path(cache_path)
        if data is None:
            try:
                with self.cache_path.open("rb") as source:
                    raw = source.read(MAX_CATALOG_BYTES + 1)
                if len(raw) > MAX_CATALOG_BYTES:
                    raise ValueError("catalog_too_large")
                loaded = json.loads(raw)
                if not isinstance(loaded, dict) or loaded.get("schemaVersion") != SCHEMA_VERSION:
                    raise ValueError("missing_catalog_schema")
                data = _validate(loaded)
            except (OSError, ValueError, TypeError):
                data = {"metadata": {"status": "unavailable", "reason": "missing_or_invalid_local_catalog"},
                        "champions": [], "augments": []}
        else:
            data = _validate(data)
        self.metadata = copy.deepcopy(data.get("metadata", {}))
        self._champions = data.get("champions", [])
        self._augments = data.get("augments", [])
        self._champion_index = self._index(self._champions, "champion")
        self._augment_index = self._index(self._augments, "augment")

    @staticmethod
    def _index(records: list[dict], kind: str) -> dict[str, list[dict]]:
        index: dict[str, list[dict]] = {}
        for record in records:
            aliases = [record.get(f"{kind}Id"), record.get("nameZh"), record.get("nameEn"),
                       record.get("titleZh"), record.get("internalName"), record.get("opggSlug"), record.get("apiName"),
                       *record.get("aliasesZh", []), *record.get("aliases", []),
                       *record.get("namesByLocale", {}).values(),
                       *record.get("namesByRegion", {}).values(),
                       *(alias for values in record.get("aliasesByLocale", {}).values() for alias in values)]
            if kind == "augment":
                # Spoken/ASR English often drops possessive 's. Preserve the
                # exact spelling too; colliding aliases remain ambiguous.
                english = record.get("nameEn", "")
                aliases.append(re.sub(r"['’]s\b", "", english, flags=re.IGNORECASE))
            for key in {normalize_name(alias) for alias in aliases} - {""}:
                index.setdefault(key, []).append(record)
        return index

    @staticmethod
    def _resolve(query: Any, index: dict, kind: str) -> dict:
        matches = index.get(normalize_name(query), [])
        if not matches:
            return {"status": "not_found"}
        if len(matches) > 1:
            result = {"status": "ambiguous", "candidates": copy.deepcopy(matches[:20])}
            if len(matches) > 20:
                result.update(candidateCount=len(matches), truncated=True)
            return result
        return {"status": "ok", kind: copy.deepcopy(matches[0])}

    def resolve_champion(self, query: str | int) -> dict:
        return self._resolve(query, self._champion_index, "champion")

    def resolve_augment(self, query: str | int) -> dict:
        return self._resolve(query, self._augment_index, "augment")

    def champion_records_for(self, queries: list[str | int]) -> list[dict]:
        """Return distinct known roster champions; never invent unknown entries."""
        if not isinstance(queries, (list, tuple)) or len(queries) > 20:
            raise ValueError("roster_query_limit")
        records = {}
        for query in queries:
            result = self.resolve_champion(query)
            if result["status"] == "ok":
                record = result["champion"]
                records.setdefault(record["championId"], record)
        return list(records.values())


def build_catalog(tencent: dict, riot: dict, *, sources: list[dict], built_at: str,
                  opgg: dict | None = None, augments: list[dict] | None = None) -> dict:
    """Join source names by numeric champion ID, never by a display-name guess."""
    heroes, riot_rows = tencent.get("hero"), riot.get("data")
    if not isinstance(heroes, list) or not heroes or len(heroes) > MAX_CHAMPIONS:
        raise ValueError("invalid_tencent_catalog")
    if not isinstance(riot_rows, dict) or not riot_rows or len(riot_rows) > MAX_CHAMPIONS:
        raise ValueError("invalid_riot_catalog")
    chinese, english = {}, {}
    for row in heroes:
        identifier = int(row["heroId"])
        if identifier in chinese:
            raise ValueError("duplicate_tencent_champion")
        chinese[identifier] = row
    for row in riot_rows.values():
        identifier = int(row["key"])
        if identifier in english:
            raise ValueError("duplicate_riot_champion")
        english[identifier] = row
    if chinese.keys() - english.keys():
        raise ValueError("tencent_ids_missing_in_riot_catalog")
    opgg_rows = (opgg or {}).get("data", opgg or {}).get("champions", [])
    slugs = {row["championId"]: row["champion"] for row in opgg_rows}
    records = []
    for identifier, row in sorted(english.items()):
        zh = chinese.get(identifier, {})
        keywords = [word.strip() for word in re.split(r"[,，]", zh.get("keywords", "")) if word.strip()]
        aliases = list(dict.fromkeys([*keywords, *([zh["alias"]] if zh.get("alias") else [])]))
        records.append({
            "championId": identifier, "nameZh": zh.get("title", ""),
            "titleZh": zh.get("name", ""), "nameEn": row["name"],
            "internalName": row["id"], "opggSlug": slugs.get(identifier, row["id"].lower()),
            "aliasesZh": [word for word in aliases if re.search(r"[\u3400-\u9fff]", word)],
            "aliases": aliases,
        })
    payload = {"champions": records, "augments": augments or []}
    data = {"schemaVersion": SCHEMA_VERSION, **payload, "metadata": {
        "builtAt": built_at, "sources": sources, "contentHash": content_hash(payload),
        "championCount": len(records), "augmentCount": len(payload["augments"]),
        "chineseNameCount": len(chinese), "opggVerifiedCount": len(slugs),
        "opggFallback": "lowercase_riot_internal_name", "matching": "exact_normalized_alias_only",
        "augmentScope": "KIWI_list_or_same_patch_verified_OPGG_Mayhem_ID",
        "augmentLocales": sorted({locale for row in payload["augments"] for locale in row.get("namesByLocale", {})}),
        "augmentLocaleSource": "Riot_client_locale_files_via_CommunityDragon",
        "nameZhCompatibility": "zh_CN_mainland_client_locale",
        "mainlandVerification": "Tencent_official_catalog_by_numeric_ID_and_API_key_where_available",
        "augmentRegionVerificationCounts": {
            "CN": sum("CN" in row.get("namesByRegion", {}) for row in payload["augments"]),
        },
        "augmentEvidenceCounts": {
            "KIWI": sum("KIWI" in row.get("modeEvidence", []) for row in payload["augments"]),
            "opgg_mayhem_cache": sum("opgg_mayhem_cache" in row.get("modeEvidence", []) for row in payload["augments"]),
        },
    }}
    return _validate(data)


def write_catalog_atomic(path: Path | str, data: dict) -> None:
    """Validate before replacement; a failed refresh keeps the last good file."""
    data = _validate(data)
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("catalog_too_large")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".catalog-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


_singleton_lock = threading.Lock()
_singleton: NameCatalog | None = None
_singleton_stamp = None


def get_name_catalog() -> NameCatalog:
    """Reload an atomically refreshed local file; never fetch on the hot path."""
    global _singleton, _singleton_stamp
    try:
        stat = DEFAULT_CACHE_PATH.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    with _singleton_lock:
        if _singleton is None or stamp != _singleton_stamp:
            candidate = NameCatalog(DEFAULT_CACHE_PATH)
            if candidate.metadata.get("status") != "unavailable" or _singleton is None:
                _singleton = candidate
            _singleton_stamp = stamp
        return _singleton
