import json


class JSONExtractError(ValueError):
    pass


def extract_json(text: str) -> object:
    """Parse JSON from LLM output, tolerating ```fences``` and surrounding prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise JSONExtractError("output was not valid JSON")