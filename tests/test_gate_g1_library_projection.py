"""Gate G1 Tests — Native Library Projection, NFO UniqueIDs, User-File Protection, and Plugin Routing."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import default as plugin_entry
from streambridge_core.adapters.fixture import FixtureAdapter
from streambridge_core.db import StreamBridgeStore
from streambridge_core.models import MediaType, Offer, Title
from streambridge_core.projection import NFO_OWNERSHIP_MARKER, ProjectionEngine
from streambridge_core.reconciler import ReconciliationEngine


def test_g1_movie_and_episode_strm_and_nfo_projection(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root, write_nfo=True)
    reconciler = ReconciliationEngine(store, projection)

    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)

    result = reconciler.reconcile_provider(adapter, region="US")
    assert result.added_count == 6
    assert result.requires_kodi_scan is True

    # Check Movie .strm and .nfo
    movie_strm = (
        export_root
        / "Streaming"
        / "Netflix"
        / "Movies"
        / "Glass Onion A Knives Out Mystery (2022)"
        / "Glass Onion A Knives Out Mystery (2022).strm"
    )
    movie_nfo = movie_strm.with_suffix(".nfo")
    assert movie_strm.exists()
    assert movie_nfo.exists()
    assert movie_strm.read_text(encoding="utf-8").strip() == "plugin://plugin.video.streambridge/play/sb_nf_movie_glassonion"

    nfo_text = movie_nfo.read_text(encoding="utf-8")
    assert NFO_OWNERSHIP_MARKER in nfo_text
    root = ET.fromstring(nfo_text)
    uids = {el.attrib["type"]: el.text for el in root.findall("uniqueid")}
    assert uids["tmdb"] == "661374"
    assert uids["imdb"] == "tt11564570"
    assert uids["netflix"] == "81458416"
    assert uids["streambridge"] == "sb_nf_movie_glassonion"

    # Check TV Episode .strm and .nfo
    ep_strm = (
        export_root
        / "Streaming"
        / "Netflix"
        / "TV"
        / "Stranger Things"
        / "Season 01"
        / "Stranger Things S01E01.strm"
    )
    assert ep_strm.exists()
    assert ep_strm.read_text(encoding="utf-8").strip() == "plugin://plugin.video.streambridge/play/sb_nf_ep_st_s01e01"
    store.close()


def test_g1_preserves_user_edited_nfo_and_rejects_user_strm_overwrite(tmp_path: Path) -> None:
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root, write_nfo=True)

    title = Title(
        title_id="sb_custom_1",
        media_type=MediaType.MOVIE,
        title="Custom Film",
        year=2025,
        tmdb_id="12345",
    )
    offer = Offer(
        title_id="sb_custom_1",
        provider="netflix",
        provider_content_id="999999",
        region="US",
        launch_url="https://www.netflix.com/watch/999999",
    )

    proj = projection.project_title(title, offer)
    assert proj is not None
    nfo_path = export_root / Path(proj.nfo_relative_path or "")
    assert nfo_path.exists()

    # Simulate user editing the NFO (removing the StreamBridge ownership marker)
    user_nfo_content = '<?xml version="1.0" encoding="UTF-8"?>\n<movie><title>My Custom Edit</title></movie>\n'
    nfo_path.write_text(user_nfo_content, encoding="utf-8")

    # Re-projecting must NOT overwrite the user's NFO
    proj2 = projection.project_title(title, offer)
    assert proj2 is not None
    assert proj2.nfo_relative_path is None
    assert nfo_path.read_text(encoding="utf-8") == user_nfo_content

    # Removing the projection must remove the owned .strm but preserve the user-edited .nfo
    projection.remove_projection(proj)
    assert not (export_root / Path(proj.relative_path)).exists()
    assert nfo_path.exists()

    # If a user creates their own .strm file not pointing to StreamBridge, refuse to overwrite
    strm_path = export_root / Path(proj.relative_path)
    strm_path.parent.mkdir(parents=True, exist_ok=True)
    strm_path.write_text("smb://nas/movies/custom.mkv\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        projection.project_title(title, offer)


def test_g1_handles_missing_year_and_skips_unaired_episodes(tmp_path: Path) -> None:
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root, write_nfo=True, export_unaired=False)

    unaired_ep = Title(
        title_id="sb_unaired_1",
        media_type=MediaType.EPISODE,
        title="TBA Episode",
        show_title="Future Show",
        season=1,
        episode=5,
        year=None,
        premiered=None,
        is_aired=False,
    )
    offer = Offer(
        title_id="sb_unaired_1",
        provider="netflix",
        provider_content_id="111222",
        region="US",
        launch_url="https://www.netflix.com/watch/111222",
    )
    assert projection.project_title(unaired_ep, offer) is None

    no_year_movie = Title(
        title_id="sb_noyear_1",
        media_type=MediaType.MOVIE,
        title="Mystery Date Movie",
        year=None,
        is_aired=True,
    )
    proj = projection.project_title(no_year_movie, offer)
    assert proj is not None
    assert proj.relative_path == "Streaming/Netflix/Movies/Mystery Date Movie/Mystery Date Movie.strm"


def test_g1_plugin_routing_and_ecosystem_metadata(tmp_path: Path) -> None:
    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)

    launched_urls: list[tuple[str, str]] = []
    from streambridge_core.broker import PlaybackBroker

    broker = PlaybackBroker(
        store=store,
        browser_launcher=lambda binary, url: launched_urls.append((binary, url)),
    )
    app = plugin_entry.StreamBridgePluginApp(
        store=store,
        export_root=export_root,
        adapter=adapter,
        broker=broker,
    )
    plugin_entry.set_app(app)

    try:
        sync_res = plugin_entry.plugin.run(["plugin://plugin.video.streambridge/sync/netflix", "1", ""])
        assert sync_res.added_count == 6

        provider_items = plugin_entry.plugin.run(["plugin://plugin.video.streambridge/providers/netflix", "1", ""])
        assert len(provider_items) == 6
        glass_onion = next(i for i in provider_items if i["unique_ids"]["streambridge"] == "sb_nf_movie_glassonion")
        assert glass_onion["default_unique_id"] == "tmdb"
        assert glass_onion["unique_ids"]["tmdb"] == "661374"
        assert glass_onion["unique_ids"]["netflix"] == "81458416"

        # Route via path parameter (/play/<id>) and via query parameter (/play?id=<id>)
        res1 = plugin_entry.plugin.run(["plugin://plugin.video.streambridge/play/sb_nf_movie_glassonion", "1", ""])
        assert res1.title_id == "sb_nf_movie_glassonion"

        res2 = plugin_entry.plugin.run(["plugin://plugin.video.streambridge/play", "1", "?id=sb_nf_ep_st_s01e01"])
        assert res2.title_id == "sb_nf_ep_st_s01e01"
    finally:
        plugin_entry.set_app(None)
        store.close()


def test_g1_library_filters_exclude_dubbed_foreign_ratings_and_genres(tmp_path: Path) -> None:
    from streambridge_core.filters import LibraryFilterPolicy, evaluate_title_filter

    store = StreamBridgeStore(tmp_path / "sb.db")
    export_root = tmp_path / "library"
    projection = ProjectionEngine(export_root)
    reconciler = ReconciliationEngine(store, projection)

    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "netflix_sample_catalog.json"
    adapter = FixtureAdapter(store, fixture_path=fixture_path)

    # 1. Permissive default exports all 6 sample items (including German-language dubbed 'All Quiet on the Western Front')
    r_all = reconciler.reconcile_provider(adapter, filter_policy=LibraryFilterPolicy.permissive_default())
    assert r_all.added_count == 6
    assert r_all.filtered_out_count == 0
    assert len(list(export_root.rglob("*.strm"))) == 6

    # 2. Native English-only + no dubbed content -> filters out 'All Quiet on the Western Front' (original_language="de", is_dubbed=True)
    # and automatically removes its previously projected .strm file + flags requires_kodi_clean!
    english_native_policy = LibraryFilterPolicy(
        allowed_original_languages=("en",),
        allow_dubbed_content=False,
    )
    r_en = reconciler.reconcile_provider(adapter, filter_policy=english_native_policy)
    assert r_en.filtered_out_count == 1
    assert r_en.removed_count == 1
    assert r_en.requires_kodi_clean is True
    assert len(list(export_root.rglob("*.strm"))) == 5

    # 3. Direct unit checks for content rating, reality/standup/documentary genres, min rating, and min year
    reality_show = Title(
        title_id="sb_reality_1",
        media_type=MediaType.SHOW,
        title="Love Is Blind",
        original_language="en",
        content_rating="TV-MA",
        user_rating=6.1,
        genres=("Reality", "Romance"),
    )
    assert not evaluate_title_filter(
        reality_show, LibraryFilterPolicy(exclude_reality_and_talk=True)
    ).allowed

    kids_show = Title(
        title_id="sb_kids_1",
        media_type=MediaType.SHOW,
        title="CoComelon",
        original_language="en",
        content_rating="TV-Y",
        user_rating=4.2,
        genres=("Animation", "Family"),
    )
    assert not evaluate_title_filter(
        kids_show, LibraryFilterPolicy(exclude_kids_content=True)
    ).allowed
    assert not evaluate_title_filter(
        kids_show, LibraryFilterPolicy(min_user_rating=6.5)
    ).allowed
    assert not evaluate_title_filter(
        reality_show, LibraryFilterPolicy(exclude_reality_and_talk=False, max_content_rating="PG-13")
    ).allowed

    # 4. Verify radio-button Kodi settings mapping (from_kodi_settings)
    radio_policy = LibraryFilterPolicy.from_kodi_settings(
        {
            "filter_by_language": True,
            "allow_dubbed_content": False,
            "lang_en": True,
            "lang_ja": True,
            "lang_de": False,
            "filter_by_rating": True,
            "rating_tv_y": False,
            "rating_tv_y7": False,
            "rating_r": False,
            "genre_reality": False,
            "genre_standup": False,
            "min_user_rating": 7.0,
            "min_release_year": 1990,
        }
    )
    assert "en" in radio_policy.allowed_original_languages
    assert "ja" in radio_policy.allowed_original_languages
    assert "de" in radio_policy.blocked_original_languages
    assert "R" in radio_policy.blocked_content_ratings
    assert "TV-MA" in radio_policy.blocked_content_ratings
    assert not evaluate_title_filter(reality_show, radio_policy).allowed
    assert not evaluate_title_filter(kids_show, radio_policy).allowed
    store.close()
