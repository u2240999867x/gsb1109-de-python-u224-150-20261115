import pytest
from jsonref import replace_refs, JsonRefError


class TestJsonRefErrors(object):
    def test_basic_error_properties(self):
        json = [{"$ref": "#/x"}]
        result = replace_refs(json)
        with pytest.raises(JsonRefError) as excinfo:
            result[0].__subject__
        e = excinfo.value
        assert e.reference == json[0]
        assert e.uri == "#/x"
        assert e.base_uri == ""
        assert e.path == (0,)
        assert type(e.cause) == TypeError

    def test_nested_refs(self):
        json = {
            "a": {"$ref": "#/b"},
            "b": {"$ref": "#/c"},
            "c": {"$ref": "#/foo"},
        }
        result = replace_refs(json)
        with pytest.raises(JsonRefError) as excinfo:
            print(result["a"])
        e = excinfo.value
        assert e.path == ("c",)

    def test_error_reports_pointer_and_failing_step(self):
        json = {"arr": [10, 20], "ref": {"$ref": "#/arr/5"}}
        result = replace_refs(json)
        with pytest.raises(JsonRefError) as excinfo:
            result["ref"].__subject__
        e = excinfo.value
        assert e.pointer == "/arr/5"
        assert e.pointer_tokens == ("arr", "5")
        assert e.steps == ("arr",)
        assert e.step_index == 1
        assert e.step == "5"
        assert isinstance(e.cause, IndexError)

    def test_error_reports_parse_failure(self):
        json = {"ref": {"$ref": "#/bad~2escape"}}
        result = replace_refs(json)
        with pytest.raises(JsonRefError) as excinfo:
            result["ref"].__subject__
        e = excinfo.value
        assert e.pointer == "/bad~2escape"
        assert e.step_index == 0
        assert e.step == "bad~2escape"

    def test_non_canonical_array_index_is_an_error(self):
        json = {"arr": [10, 20], "ref": {"$ref": "#/arr/01"}}
        result = replace_refs(json)
        with pytest.raises(JsonRefError) as excinfo:
            result["ref"].__subject__
        e = excinfo.value
        assert e.step_index == 1
        assert e.step == "01"
        assert isinstance(e.cause, TypeError)
