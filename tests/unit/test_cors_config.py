from core.config import Settings


def origins(value: str) -> list[str]:
    return Settings(_env_file=None, cors_origins=value).cors_origin_list


def test_the_dashboards_local_addresses_are_allowed_by_default():
    assert Settings(_env_file=None).cors_origin_list == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_origins_are_split_trimmed_and_stripped_of_trailing_slashes():
    assert origins(" https://a.example/ , https://b.example ") == ["https://a.example", "https://b.example"]


def test_blank_entries_are_ignored():
    assert origins("https://a.example,, ,") == ["https://a.example"]


def test_an_empty_setting_allows_no_origin_at_all():
    assert origins("") == []
