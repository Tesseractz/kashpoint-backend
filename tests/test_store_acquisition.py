from app.api.deps import _acquisition_from_metadata


def test_keeps_only_the_three_utm_keys():
    raw = {"utm_source": "facebook", "utm_medium": "groups", "utm_campaign": "spaza", "utm_term": "x", "ref": "A"}
    assert _acquisition_from_metadata(raw) == {"utm_source": "facebook", "utm_medium": "groups", "utm_campaign": "spaza"}


def test_trims_and_caps_length():
    out = _acquisition_from_metadata({"utm_source": "  " + "a" * 300 + "  "})
    assert out == {"utm_source": "a" * 100}


def test_ignores_blank_and_non_string_values():
    assert _acquisition_from_metadata({"utm_source": "   ", "utm_medium": 5}) is None


def test_missing_or_non_dict_gives_none():
    assert _acquisition_from_metadata(None) is None
    assert _acquisition_from_metadata("facebook") is None
    assert _acquisition_from_metadata({}) is None
