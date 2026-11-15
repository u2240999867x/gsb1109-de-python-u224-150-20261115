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

    def test_circular_refs(self):
        json = {
            "a": {"$ref": "#/b"},
            "b": {"$ref": "#/a"},
        }
        result = replace_refs(json)
        # Loading is lazy, no error yet
        assert result["a"].__reference__ == json["a"]
        # Accessing the reference raises a JsonRefError, not a RecursionError
        with pytest.raises(JsonRefError) as excinfo:
            result["a"].__subject__
        assert "Circular reference" in excinfo.value.message
