"""Deterministic, offline tests for source joins, ambiguity and atomic refresh."""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.refresh_name_catalog import (
    build_augment_records, collect_opgg_augment_evidence, fetch_json,
    refresh_catalog, _SourceRedirects,
)
from src.lol_mcp import name_catalog as module
from src.lol_mcp.name_catalog import NameCatalog, build_catalog, write_catalog_atomic


@pytest.fixture
def sources():
    tencent = {"version": "16.17", "fileTime": "2026-08-26", "hero": [
        {"heroId": "62", "name": "齐天大圣", "title": "孙悟空", "alias": "MonkeyKing",
         "keywords": "猴子,孙悟空,齐天大圣,MonkeyKing,houzi,hz,shared"},
        {"heroId": "20", "name": "雪原双子", "title": "努努和威朗普", "alias": "Nunu",
         "keywords": "努努,努努和威朗普,Nunu,shared"},
    ]}
    riot = {"version": "16.17.1", "data": {
        "MonkeyKing": {"id": "MonkeyKing", "key": "62", "name": "Wukong"},
        "Nunu": {"id": "Nunu", "key": "20", "name": "Nunu & Willump"},
    }}
    return tencent, riot


@pytest.fixture
def data(sources):
    return build_catalog(*sources, sources=[{"name": "test", "version": "16.17"}],
                         built_at="2026-09-05T00:00:00+00:00")


@pytest.mark.parametrize("query", [62, "62", "MonkeyKing", "MONKEY KING", "Wukong", "猴子", "孙悟空", "齐天大圣", "houzi"])
def test_numeric_join_resolves_engine_display_and_source_aliases(data, query):
    result = NameCatalog(data=data).resolve_champion(query)
    assert result["status"] == "ok"
    assert result["champion"]["championId"] == 62
    assert result["champion"]["nameEn"] == "Wukong"
    assert result["champion"]["internalName"] == "MonkeyKing"
    assert result["champion"]["opggSlug"] == "monkeyking"
    assert result["champion"]["nameZh"] == "孙悟空"


def test_name_normalization_is_exact_and_ambiguous_aliases_never_guess(data):
    catalog = NameCatalog(data=data)
    assert catalog.resolve_champion("nunu & willump")["champion"]["championId"] == 20
    assert catalog.resolve_champion("ＮＵＮＵ　＆　ＷＩＬＬＵＭＰ")["champion"]["championId"] == 20
    ambiguous = catalog.resolve_champion("shared")
    assert ambiguous["status"] == "ambiguous"
    assert {row["championId"] for row in ambiguous["candidates"]} == {20, 62}
    for query in ("wukon", "猴", "https://example.com/monkeyking", "x" * 129, None, True, "Wukong\x00"):
        assert catalog.resolve_champion(query) == {"status": "not_found"}


def test_roster_deduplicates_known_names_without_mutating_catalog(data):
    catalog = NameCatalog(data=data)
    records = catalog.champion_records_for(["MonkeyKing", "猴子", "Nunu", "missing"])
    assert [row["championId"] for row in records] == [62, 20]
    records[0]["nameEn"] = "changed"
    assert catalog.resolve_champion(62)["champion"]["nameEn"] == "Wukong"
    with pytest.raises(ValueError, match="roster_query_limit"):
        catalog.champion_records_for(["Nunu"] * 21)


def test_duplicate_and_unjoined_source_ids_fail_refresh(sources):
    tencent, riot = copy.deepcopy(sources)
    tencent["hero"].append(tencent["hero"][0])
    with pytest.raises(ValueError, match="duplicate_tencent"):
        build_catalog(tencent, riot, sources=[], built_at="test")
    tencent, riot = copy.deepcopy(sources)
    riot["data"].pop("MonkeyKing")
    with pytest.raises(ValueError, match="missing_in_riot"):
        build_catalog(tencent, riot, sources=[], built_at="test")


def test_missing_or_corrupt_cache_is_no_network_not_found(tmp_path, monkeypatch, data):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("network on hot path"))
    target = tmp_path / "catalog.json"
    assert NameCatalog(target).resolve_champion("Wukong") == {"status": "not_found"}
    target.write_text("not-json", encoding="utf-8")
    assert NameCatalog(target).metadata["status"] == "unavailable"
    target.write_text("{}", encoding="utf-8")
    assert NameCatalog(target).metadata["status"] == "unavailable"
    write_catalog_atomic(target, data)
    assert NameCatalog(target).resolve_champion("Wukong")["status"] == "ok"


def test_hash_tampering_and_failed_atomic_replace_preserve_last_good(tmp_path, monkeypatch, data):
    target = tmp_path / "catalog.json"
    write_catalog_atomic(target, data)
    original = target.read_bytes()
    tampered = copy.deepcopy(data)
    tampered["champions"][0]["nameEn"] = "edited without hash"
    with pytest.raises(ValueError, match="hash_mismatch"):
        write_catalog_atomic(target, tampered)
    assert target.read_bytes() == original
    monkeypatch.setattr(module.os, "replace", MagicMock(side_effect=OSError("disk error")))
    with pytest.raises(OSError):
        write_catalog_atomic(target, data)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".catalog-*.tmp"))


def test_singleton_reloads_changed_local_snapshot_and_retains_good_on_corruption(tmp_path, monkeypatch, data):
    target = tmp_path / "catalog.json"
    monkeypatch.setattr(module, "DEFAULT_CACHE_PATH", target)
    monkeypatch.setattr(module, "_singleton", None)
    monkeypatch.setattr(module, "_singleton_stamp", None)
    empty = module.get_name_catalog()
    write_catalog_atomic(target, data)
    loaded = module.get_name_catalog()
    assert loaded is not empty
    assert loaded.resolve_champion("猴子")["status"] == "ok"
    assert module.get_name_catalog() is loaded
    target.write_text("corrupted", encoding="utf-8")
    assert module.get_name_catalog() is loaded


def augment_sources():
    en = [
        {"id": 2095, "augmentNameId": "ARAM_HighRoller", "nameTRA": "High Roller"},
        {"id": 95, "augmentNameId": "HighRoller", "nameTRA": "High Roller"},
        {"id": 1400, "augmentNameId": "TrainOfTheDead", "nameTRA": "Final City Transit"},
    ]
    zh = [{**row, "nameTRA": name} for row, name in zip(en, ["掷骰狂人", "竞技场掷骰狂人", "最终都市列车"])]
    modes = [{"modeName": "KIWI", "augmentList": ["Maps/ModeSpecificData/Augments/ARAM_HighRoller"]},
             {"modeName": "CHERRY", "augmentList": ["Maps/ModeSpecificData/Augments/HighRoller"]}]
    return en, zh, modes


def test_augment_modes_and_ids_are_scoped_without_aliasing_opgg_icon_keys():
    rows = build_augment_records(*augment_sources(), opgg_augment_ids={1400})
    catalog = NameCatalog(data={"augments": rows})
    assert catalog.resolve_augment("掷骰狂人")["augment"]["augmentId"] == 2095
    assert catalog.resolve_augment("ARAM_HighRoller")["augment"]["namespace"] == "aram_mayhem"
    assert catalog.resolve_augment(95) == {"status": "not_found"}
    assert catalog.resolve_augment(1400)["augment"]["nameEn"] == "Final City Transit"
    assert catalog.resolve_augment("Nightstalking") == {"status": "not_found"}
    assert catalog.resolve_augment(1400)["augment"]["modeEvidence"] == ["opgg_mayhem_cache"]


def test_augment_locale_id_mismatch_is_rejected():
    en, zh, modes = augment_sources()
    zh[0]["id"] = 95
    with pytest.raises(ValueError, match="locale_identifier_mismatch"):
        build_augment_records(en, zh, modes)


def test_opgg_mode_evidence_requires_matching_wrapper_and_patch(tmp_path):
    payload = {"version": 1, "key": "ahri-augments", "data": {"patch": "16.17", "augments": [{"id": 1400}]}}
    (tmp_path / "ahri-augments.json").write_text(json.dumps(payload), encoding="utf-8")
    stale = {**payload, "key": "nunu-augments", "data": {"patch": "16.16", "augments": [{"id": 95}]}}
    (tmp_path / "nunu-augments.json").write_text(json.dumps(stale), encoding="utf-8")
    ids, evidence = collect_opgg_augment_evidence(tmp_path, "16.17")
    assert ids == {1400}
    assert [key for key, _ in evidence] == ["ahri-augments"]


def test_refresh_pins_locales_and_preserves_cache_on_upstream_failure(tmp_path, sources, data):
    output = tmp_path / "catalog.json"
    write_catalog_atomic(output, data)
    tencent, riot = sources
    augment_en, augment_zh, modes = augment_sources()
    requested = []

    def fetch(url):
        requested.append(url)
        if url == module.RIOT_VERSIONS_URL:
            return ["16.17.1"]
        if url == module.TENCENT_URL:
            return tencent
        if url == module.TENCENT_AUGMENTS_URL:
            return [{"augmentID": row["id"], "name_en": row["augmentNameId"],
                     "name_cn": translated["nameTRA"], "isPBE": 0}
                    for row, translated in zip(augment_en, augment_zh)]
        if "ddragon" in url:
            return riot
        if "augment-lists" in url:
            return modes
        return augment_zh if any(f"/{locale}/" in url for locale in ("zh_cn", "zh_my", "zh_tw")) else augment_en

    metadata = refresh_catalog(output=output, opgg_snapshot=tmp_path / "absent/opgg.json", fetcher=fetch)
    assert metadata["championCount"] == 2 and metadata["augmentCount"] == 1
    assert all("/16.17/" in url for url in requested if "communitydragon" in url)
    assert {row["locale"] for row in metadata["sources"] if "locale" in row} == {"en_US", "zh_CN", "zh_MY", "zh_TW"}
    assert all(len(source["sha256"]) == 64 for source in metadata["sources"])
    good = output.read_bytes()
    def mismatched_locale(url):
        payload = copy.deepcopy(fetch(url))
        if "/zh_tw/" in url:
            payload[0]["id"] = 999
        return payload
    with pytest.raises(ValueError, match="locale_identifier_mismatch"):
        refresh_catalog(output=output, opgg_snapshot=tmp_path / "absent/opgg.json", fetcher=mismatched_locale)
    assert output.read_bytes() == good
    with pytest.raises(TimeoutError):
        refresh_catalog(output=output, fetcher=MagicMock(side_effect=TimeoutError()))
    assert output.read_bytes() == good


def test_download_boundaries_reject_arbitrary_hosts_redirects_and_oversize(monkeypatch):
    with pytest.raises(ValueError, match="unapproved_source_url"):
        fetch_json("http://127.0.0.1/private")
    with pytest.raises(ValueError, match="unapproved_source_redirect"):
        _SourceRedirects().redirect_request(None, None, 302, "", {}, "https://untrusted.example/file")
    response = MagicMock()
    response.geturl.return_value = module.TENCENT_URL
    response.headers = {"Content-Length": "11"}
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)
    with pytest.raises(ValueError, match="source_too_large"):
        fetch_json(module.TENCENT_URL, max_bytes=10)
    response.read.assert_not_called()


def regional_augment_sources():
    en, cn, modes = augment_sources()
    my, tw = copy.deepcopy(cn), copy.deepcopy(cn)
    my[0]["nameTRA"] = "豪掷千金"
    my[0]["simpleNameTRA"] = "马服测试简称"
    tw[0]["nameTRA"] = "豪氣賭客"
    mainland = [{"augmentID": row["id"], "name_en": row["augmentNameId"],
                 "name_cn": row["nameTRA"], "isPBE": 0, "mode": ""} for row in cn]
    return en, cn, modes, {"zh_MY": my, "zh_TW": tw}, mainland


def test_all_client_locales_are_distinct_exact_aliases_with_tencent_verification():
    en, cn, modes, locales, mainland = regional_augment_sources()
    rows = build_augment_records(en, cn, modes, locale_sources=locales, mainland_source=mainland)
    catalog = NameCatalog(data={"augments": rows})
    record = catalog.resolve_augment(2095)["augment"]
    assert record["namesByLocale"] == {
        "en_US": "High Roller", "zh_CN": "掷骰狂人", "zh_MY": "豪掷千金", "zh_TW": "豪氣賭客",
    }
    assert record["nameZh"] == record["namesByLocale"]["zh_CN"]
    assert record["namesByRegion"] == {"CN": "掷骰狂人"}
    assert record["regionVerification"] == {"CN": "identity_and_name_verified"}
    for name in (*record["namesByLocale"].values(), "马服测试简称"):
        assert catalog.resolve_augment(name)["augment"]["augmentId"] == 2095
    assert catalog.resolve_augment("豪气赌客") == {"status": "not_found"}


@pytest.mark.parametrize("change", ["missing", "id", "key"])
def test_selected_augment_missing_or_misaligned_locale_fails(change):
    en, cn, modes, locales, _ = regional_augment_sources()
    if change == "missing":
        locales["zh_MY"].pop(0)
    elif change == "id":
        locales["zh_MY"][0]["id"] = 999
    else:
        locales["zh_MY"][0]["augmentNameId"] = "WrongApiKey"
    with pytest.raises(ValueError, match="augment_locale"):
        build_augment_records(en, cn, modes, locale_sources=locales)


def test_cross_locale_name_collision_returns_distinct_ids_as_candidates():
    en, cn, modes, locales, _ = regional_augment_sources()
    locales["zh_MY"][2]["nameTRA"] = "掷骰狂人"
    rows = build_augment_records(en, cn, modes, locale_sources=locales, opgg_augment_ids={1400})
    result = NameCatalog(data={"augments": rows}).resolve_augment("掷骰狂人")
    assert result["status"] == "ambiguous"
    assert {row["augmentId"] for row in result["candidates"]} == {2095, 1400}


@pytest.mark.parametrize("change", ["key", "pbe"])
def test_tencent_verification_never_falls_back_to_client_translation(change):
    en, cn, modes, locales, mainland = regional_augment_sources()
    if change == "key":
        mainland[0]["name_en"] = "DifferentApiKey"
    else:
        mainland[0]["isPBE"] = 1
    record = build_augment_records(en, cn, modes, locale_sources=locales, mainland_source=mainland)[0]
    assert record["namesByRegion"] == {}
    assert record["namesByLocale"]["zh_CN"] == "掷骰狂人"
    assert record["regionVerification"]["CN"] in {"identifier_key_mismatch", "not_in_tencent_catalog"}


def test_source_punctuation_only_name_and_regional_variant_remain_resolvable():
    en = [{"id": 1424, "augmentNameId": "MissingPing", "nameTRA": "???"}]
    cn = copy.deepcopy(en)
    modes = [{"modeName": "KIWI", "augmentList": ["Maps/ModeSpecificData/Augments/MissingPing"]}]
    mainland = [{"augmentID": 1424, "name_en": "MissingPing", "name_cn": "？？？", "isPBE": 0}]
    catalog = NameCatalog(data={"augments": build_augment_records(en, cn, modes, mainland_source=mainland)})
    for name in ("???", "？？？"):
        result = catalog.resolve_augment(name)
        assert result["status"] == "ok"
        assert result["augment"]["augmentId"] == 1424
    assert result["augment"]["namesByRegion"]["CN"] == "？？？"
    assert result["augment"]["regionVerification"]["CN"] == "identity_verified_name_variant"
