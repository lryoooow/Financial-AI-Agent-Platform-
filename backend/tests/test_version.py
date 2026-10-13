import tomllib
from pathlib import Path

from finresearch import __version__
from finresearch.main import app


def test_public_version_matches_project_metadata() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == pyproject["project"]["version"]
    assert app.version == __version__
