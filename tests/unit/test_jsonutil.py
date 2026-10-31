import pytest

from core.jsonutil import JSONExtractError, extract_json


def test_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_json_in_a_code_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_surrounded_by_prose():
    assert extract_json('Sure! {"a": {"b": 2}} Hope that helps.') == {"a": {"b": 2}}


def test_surrounding_whitespace_is_ignored():
    assert extract_json('  \n {"a": 1} \n ') == {"a": 1}


@pytest.mark.parametrize("text", ["", "no json here", "{not: valid}", "} {", "{"])
def test_garbage_raises(text):
    with pytest.raises(JSONExtractError):
        extract_json(text)