from mtcontroller import ros_versions as rv


def test_parse_and_compare():
    assert rv.parse("7.20.1 (stable)") == (7, 20, 1)
    assert rv.parse("7.24") == (7, 24, 0)
    assert rv.parse("7.24.5 (stable)") > rv.parse("7.24")
    assert rv.channel("7.21.3 (long-term)") == "long-term"
    assert rv.parse("garbage") == (0,)


def test_known_issues_ranges():
    crit = lambda v: [i for i in rv.issues_for(v) if i.severity == "critical"]
    assert crit("7.24.1 (stable)") and crit("7.24.4")
    assert not crit("7.24.5 (stable)") and not crit("7.23.1")
    assert rv.issues_for("7.25 (stable)")  # neopravený únik paměti stále hlášen


def test_min_supported():
    assert rv.is_supported("7.20.1") and not rv.is_supported("7.9")
