from pathlib import Path

import tomllib

import flywizz


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    if flywizz.__version__.startswith("0.0.0+"):
        return  # not installed; nothing to compare against
    assert flywizz.__version__ == declared


def test_public_surface_is_mirrored():
    sync = {n for n in dir(flywizz.WizzAir) if not n.startswith("_")}
    a = {n for n in dir(flywizz.AsyncWizzAir) if not n.startswith("_")}
    sync -= {"close"}
    a -= {"aclose"}
    assert sync == a
