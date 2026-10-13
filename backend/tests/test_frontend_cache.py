from finresearch.main import FRONTEND_INDEX_HEADERS


def test_frontend_index_disables_persistent_cache() -> None:
    cache_control = FRONTEND_INDEX_HEADERS["Cache-Control"]

    assert "no-store" in cache_control
    assert "no-cache" in cache_control
    assert FRONTEND_INDEX_HEADERS["Pragma"] == "no-cache"
    assert FRONTEND_INDEX_HEADERS["Expires"] == "0"
