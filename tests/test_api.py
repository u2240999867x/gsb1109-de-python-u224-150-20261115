from jsonref import load, loads, dump, dumps, replace_refs


class TestApi(object):
    def test_loads(self):
        json = """{"a": 1, "b": {"$ref": "#/a"}}"""
        assert loads(json) == {"a": 1, "b": 1}

    def test_loads_kwargs(self):
        json = """{"a": 5.5, "b": {"$ref": "#/a"}}"""
        loaded = loads(json, parse_float=lambda x: int(float(x)))
        assert loaded["a"] == loaded["b"] == 5

    def test_load(self, tmpdir):
        json = """{"a": 1, "b": {"$ref": "#/a"}}"""
        tmpdir.join("in.json").write(json)
        assert load(tmpdir.join("in.json")) == {"a": 1, "b": 1}

    def test_dumps(self):
        json = """[1, 2, {"$ref": "#/0"}, 3]"""
        loaded = loads(json)
        # The string version should load the reference
        assert str(loaded) == "[1, 2, 1, 3]"
        # Our dump function should write the original reference
        assert dumps(loaded) == json

    def test_dump(self, tmpdir):
        json = """[1, 2, {"$ref": "#/0"}, 3]"""
        loaded = loads(json)
        # The string version should load the reference
        assert str(loaded) == "[1, 2, 1, 3]"
        dump(loaded, tmpdir.join("out.json"))
        # Our dump function should write the original reference
        assert tmpdir.join("out.json").read() == json

    def test_dumps_cycle(self):
        json = """{"x": {"link": {"$ref": "#/y"}}, "y": {"link": {"$ref": "#/x"}}}"""
        loaded = loads(json, proxies=False)
        # The cycle should be dumped as a reference, not recursed forever
        assert dumps(loaded) == (
            '{"x": {"link": {"link": {"$ref": "#/x"}}}, '
            '"y": {"link": {"link": {"$ref": "#/y"}}}}'
        )

    def test_dumps_cycle_roundtrip(self):
        json = """{"x": {"link": {"$ref": "#/y"}}, "y": {"link": {"$ref": "#/x"}}}"""
        loaded = loads(json, proxies=False)
        # The dumped references should load back into an equivalent structure
        reloaded = loads(dumps(loaded), proxies=False)
        assert reloaded["x"]["link"]["link"] is reloaded["x"]
        assert reloaded["y"]["link"]["link"] is reloaded["y"]

    def test_dumps_root_cycle(self):
        loaded = replace_refs({"a": 1, "b": {"$ref": "#"}}, proxies=False)
        assert dumps(loaded) == '{"a": 1, "b": {"$ref": "#"}}'

    def test_dumps_acyclic_unchanged(self):
        json = """{"a": [1, 2], "b": {"$ref": "#/a"}}"""
        assert dumps(loads(json)) == json
