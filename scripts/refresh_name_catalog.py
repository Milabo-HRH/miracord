"""Explicit, bounded refresh of local champion and ARAM Mayhem name snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.lol_mcp.name_catalog import (
    DEFAULT_CACHE_PATH, MAX_CATALOG_BYTES, TENCENT_URL, RIOT_VERSIONS_URL, TENCENT_AUGMENTS_URL,
    build_catalog, content_hash, write_catalog_atomic,
)

ALLOWED_HOSTS = {"game.gtimg.cn", "ddragon.leagueoflegends.com", "raw.communitydragon.org"}


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class _SourceRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if not _allowed_url(newurl):
            raise ValueError("unapproved_source_redirect")
        return super().redirect_request(request, response, code, message, headers, newurl)


def fetch_json(url: str, *, timeout: float = 10, max_bytes: int = MAX_CATALOG_BYTES):
    """Read only fixed public source hosts, with bounded size/time and no retries."""
    if not _allowed_url(url):
        raise ValueError("unapproved_source_url")
    timeout = min(30, max(1, timeout))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SourceRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "MiraCordNameCatalog/1.0", "Accept": "application/json"})
    deadline = time.monotonic() + timeout
    with opener.open(request, timeout=timeout) as response:
        if not _allowed_url(response.geturl()):
            raise ValueError("unapproved_source_redirect")
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError("source_too_large")
        raw = bytearray()
        read = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("source_read_deadline")
            chunk = read(min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise ValueError("source_too_large")
    return json.loads(raw.decode("utf-8-sig"))


def read_snapshot(path: Path):
    with path.open("rb") as source:
        raw = source.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("snapshot_too_large")
    return json.loads(raw)


def build_augment_records(english: list, chinese: list, mode_lists: list,
                          *, opgg_augment_ids: set[int] | None = None,
                          locale_sources: dict[str, list] | None = None,
                          mainland_source: list | None = None) -> list[dict]:
    """Import KIWI or verified OP.GG Mayhem IDs; never sweep in the Arena pool."""
    verified_ids = opgg_augment_ids or set()
    if not all(isinstance(value, list) and len(value) <= 4096 for value in (english, chinese, mode_lists)):
        raise ValueError("invalid_augment_sources")
    kiwi = [row for row in mode_lists if row.get("modeName") == "KIWI"]
    if len(kiwi) != 1 or not isinstance(kiwi[0].get("augmentList"), list):
        raise ValueError("missing_or_ambiguous_kiwi_mode")
    keys = {value.rsplit("/", 1)[-1] for value in kiwi[0]["augmentList"] if isinstance(value, str)}
    if not keys:
        raise ValueError("empty_kiwi_pool")
    locales = {"en_US": english, "zh_CN": chinese}
    if locale_sources:
        if locales.keys() & locale_sources.keys():
            raise ValueError("duplicate_locale_source")
        locales.update(locale_sources)
    localized = {}
    for locale, rows in locales.items():
        if (not re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}", locale)
                or not isinstance(rows, list) or not rows or len(rows) > 4096):
            raise ValueError("invalid_locale_source")
        localized[locale] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("augmentNameId"), str):
                raise ValueError("invalid_localized_augment")
            key = row["augmentNameId"]
            if key in localized[locale]:
                raise ValueError("duplicate_localized_augment_key")
            localized[locale][key] = row
    mainland = {}
    if mainland_source is not None:
        if not isinstance(mainland_source, list) or not mainland_source or len(mainland_source) > 4096:
            raise ValueError("invalid_tencent_augment_source")
        for row in mainland_source:
            if (not isinstance(row, dict) or type(row.get("augmentID")) is not int
                    or not isinstance(row.get("name_en"), str)
                    or not isinstance(row.get("name_cn"), str) or not row["name_cn"]
                    or "isPBE" not in row):
                raise ValueError("invalid_tencent_augment_record")
            if row["isPBE"] in (1, True, "1"):
                continue
            if row["isPBE"] not in (0, False, "0"):
                raise ValueError("invalid_tencent_pbe_flag")
            identifier = row["augmentID"]
            if identifier in mainland:
                raise ValueError("duplicate_tencent_augment_id")
            mainland[identifier] = row
    selected = {}
    for row in english:
        key = row.get("augmentNameId")
        if key not in keys and row.get("id") not in verified_ids:
            continue
        if key in selected:
            raise ValueError("missing_or_duplicate_augment_locale")
        names, aliases = {}, {}
        for locale, localized_rows in localized.items():
            if key not in localized_rows:
                raise ValueError("missing_or_duplicate_augment_locale")
            translated = localized_rows[key]
            if row.get("id") != translated.get("id"):
                raise ValueError("augment_locale_identifier_mismatch")
            name = translated.get("nameTRA")
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError("invalid_locale_name")
            names[locale] = name
            simple_name = translated.get("simpleNameTRA")
            aliases[locale] = [simple_name] if simple_name and simple_name != name else []
        identifier = row.get("id")
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("invalid_kiwi_augment_id")
        region_names = {}
        verification = "not_in_tencent_catalog" if mainland_source is not None else "not_provided"
        mainland_row = mainland.get(identifier)
        if mainland_row:
            if mainland_row["name_en"] == key:
                region_names["CN"] = mainland_row["name_cn"]
                verification = ("identity_and_name_verified" if mainland_row["name_cn"] == names["zh_CN"]
                                else "identity_verified_name_variant")
            else:
                verification = "identifier_key_mismatch"
        selected[key] = {
            "augmentId": identifier, "namespace": "aram_mayhem",
            "idNamespace": "communitydragon", "apiName": key,
            "nameZh": names["zh_CN"], "nameEn": names["en_US"],
            "namesByLocale": names, "aliasesByLocale": aliases,
            "localeSource": "riot_client_data_via_communitydragon",
            "namesByRegion": region_names, "regionVerification": {"CN": verification},
            "aliasesZh": aliases["zh_CN"], "aliases": aliases["en_US"],
            "sourceModes": ["KIWI"] if key in keys else [],
            "modeEvidence": (["KIWI"] if key in keys else [])
                            + (["opgg_mayhem_cache"] if identifier in verified_ids else []),
        }
    if not keys <= selected.keys():
        raise ValueError("kiwi_keys_missing_from_catalog")
    return sorted(selected.values(), key=lambda row: row["augmentId"])


def collect_opgg_augment_evidence(cache_dir: Path, patch: str) -> tuple[set[int], list[tuple[str, dict]]]:
    """Use same-patch, validated local Mayhem cache wrappers as mode evidence."""
    identifiers, evidence = set(), []
    total_bytes = 0
    for path in sorted(cache_dir.glob("*-augments.json"))[:64]:
        if not re.fullmatch(r"[a-z]{2,64}-augments\.json", path.name):
            continue
        total_bytes += path.stat().st_size
        if total_bytes > MAX_CATALOG_BYTES:
            raise ValueError("opgg_evidence_size_limit")
        payload = read_snapshot(path)
        data = payload.get("data", {})
        if payload.get("version") != 1 or payload.get("key") != path.stem or data.get("patch") != patch:
            continue
        rows = data.get("augments")
        if not isinstance(rows, list) or len(rows) > 4096:
            continue
        row_ids = {row.get("id") for row in rows if isinstance(row, dict)
                   and type(row.get("id")) is int and row["id"] > 0}
        if row_ids:
            identifiers.update(row_ids)
            evidence.append((path.stem, payload))
    return identifiers, evidence


def refresh_catalog(*, output: Path = DEFAULT_CACHE_PATH, version: str | None = None,
                    tencent_snapshot: Path | None = None, opgg_snapshot: Path | None = None,
                    champions_only: bool = False, timeout: float = 10, fetcher=None) -> dict:
    fetch = fetcher or (lambda url: fetch_json(url, timeout=timeout))
    built_at = datetime.now(timezone.utc).isoformat()
    sources = []

    def source(name, url, payload, source_version=None, *, local=False):
        sources.append({"name": name, "url": url, "version": source_version,
                        "sha256": content_hash(payload), "hashFormat": "canonical_json",
                        "retrievedAt": None if local else built_at,
                        "loadedFrom": "local_snapshot" if local else "public_source"})

    if version is None:
        versions = fetch(RIOT_VERSIONS_URL)
        if not isinstance(versions, list) or not versions:
            raise ValueError("invalid_riot_versions")
        version = versions[0]
        source("riot_versions", RIOT_VERSIONS_URL, versions, version)
    if not isinstance(version, str) or not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{1,2}", version):
        raise ValueError("invalid_riot_version")
    tencent = read_snapshot(tencent_snapshot) if tencent_snapshot else fetch(TENCENT_URL)
    source("tencent_hero_list", TENCENT_URL, tencent, tencent.get("version"), local=bool(tencent_snapshot))
    sources[-1]["publishedAt"] = tencent.get("fileTime")
    riot_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
    riot = fetch(riot_url)
    if riot.get("version") != version:
        raise ValueError("riot_version_mismatch")
    source("riot_champions_en_US", riot_url, riot, version)
    opgg = read_snapshot(opgg_snapshot) if opgg_snapshot and opgg_snapshot.exists() else None
    if opgg:
        source("opgg_mayhem_champions", "https://op.gg/lol/modes/aram-mayhem", opgg,
               opgg.get("data", opgg).get("patch"), local=True)
    augments = []
    if not champions_only:
        patch = ".".join(version.split(".")[:2])
        prefix = f"https://raw.communitydragon.org/{patch}/plugins/rcp-be-lol-game-data/global"
        datasets = {}
        for directory, locale in (("default", "en_US"), ("zh_cn", "zh_CN"),
                                  ("zh_my", "zh_MY"), ("zh_tw", "zh_TW")):
            url = f"{prefix}/{directory}/v1/cherry-augments.json"
            payload = fetch(url)
            datasets[locale] = payload
            source(f"communitydragon_{directory}_cherry-augments.json", url, payload, patch)
            sources[-1]["locale"] = locale
        mode_url = f"{prefix}/default/v1/augment-lists.json"
        mode_lists = fetch(mode_url)
        source("communitydragon_default_augment-lists.json", mode_url, mode_lists, patch)
        mainland = fetch(TENCENT_AUGMENTS_URL)
        source("tencent_mainland_kiwi_augments", TENCENT_AUGMENTS_URL, mainland)
        sources[-1].update(region="CN", crossCheckedClientPatch=patch)
        evidence_dir = opgg_snapshot.parent if opgg_snapshot else DEFAULT_CACHE_PATH.parents[1] / "opgg_cache"
        verified_ids, evidence = collect_opgg_augment_evidence(evidence_dir, patch)
        for key, payload in evidence:
            champion = key.removesuffix("-augments")
            source("opgg_mayhem_augment_evidence", f"https://op.gg/lol/modes/aram-mayhem/{champion}/augments",
                   payload, patch, local=True)
            sources[-1]["cacheKey"] = key
        augments = build_augment_records(
            datasets["en_US"], datasets["zh_CN"], mode_lists,
            opgg_augment_ids=verified_ids,
            locale_sources={locale: datasets[locale] for locale in ("zh_MY", "zh_TW")},
            mainland_source=mainland,
        )
    catalog = build_catalog(tencent, riot, sources=sources, built_at=built_at,
                            opgg=opgg, augments=augments)
    write_catalog_atomic(output, catalog)
    return catalog["metadata"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--version", help="Pin a Data Dragon version, such as 16.17.1.")
    parser.add_argument("--tencent-snapshot", type=Path)
    parser.add_argument("--opgg-snapshot", type=Path, default=DEFAULT_CACHE_PATH.parents[1] / "opgg_cache/global-champion-tiers.json")
    parser.add_argument("--champions-only", action="store_true", help="Omit localized Mayhem augments.")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    try:
        metadata = refresh_catalog(**vars(args))
    except Exception as exc:
        print(json.dumps({"status": "failed", "errorType": type(exc).__name__,
                          "reason": "refresh_failed_previous_cache_preserved"}))
        return 1
    print(json.dumps({"status": "ok", "metadata": metadata}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
