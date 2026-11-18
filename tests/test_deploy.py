"""Wdrożenie wydań: scripts/deploy/envfile.py i spójność plików, które serwer pobiera."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("envfile", ROOT / "scripts" / "deploy" / "envfile.py")
envfile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(envfile)


def test_write_renders_variables_and_skips_deploy_settings(tmp_path, monkeypatch):
    variables = {
        "FEEDSODT_RETRY_MINUTES": "",
        "FEEDSODT_RUN_AT": "00:30",
        "DEPLOY_APP_USER": "feedsodt",
        "FEEDSODT_SHAPE_WORKERS": 'a"$b\\',
    }
    monkeypatch.setenv("VARS_JSON", json.dumps(variables))
    out = tmp_path / "production.env"
    envfile.write(out)

    text = out.read_text()
    assert "DEPLOY_APP_USER" not in text
    assert out.stat().st_mode & 0o777 == 0o600
    # Compose czyta z powrotem dokładnie te wartości, pustą też.
    assert envfile.parse(text) == {
        "FEEDSODT_RETRY_MINUTES": "",
        "FEEDSODT_RUN_AT": "00:30",
        "FEEDSODT_SHAPE_WORKERS": 'a"$b\\',
    }


def test_write_refuses_a_line_break(tmp_path, monkeypatch):
    monkeypatch.setenv("VARS_JSON", json.dumps({"FEEDSODT_RUN_AT": "00:30\nCOMPOSE_FILE=x"}))
    with pytest.raises(SystemExit):
        envfile.write(tmp_path / "production.env")


def _merge(tmp_path, release, host):
    (tmp_path / "release.env").write_text(release)
    (tmp_path / "host.env").write_text(host)
    out = tmp_path / "production.env"
    envfile.merge(tmp_path / "release.env", tmp_path / "host.env", out)
    return envfile.load(out)


def test_merge_lets_the_host_file_win(tmp_path):
    merged = _merge(
        tmp_path,
        'FEEDSODT_RUN_AT="01:00"\n',
        "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml\nFEEDSODT_RUN_AT=02:00\n",
    )
    assert merged == {
        "FEEDSODT_RUN_AT": "02:00",
        "COMPOSE_FILE": "docker-compose.yml:docker-compose.prod.yml",
    }


@pytest.mark.parametrize("key", ["COMPOSE_FILE", "FEEDSODT_PORT", "FEEDSODT_VERSION", "RAIL_MEM_LIMIT"])
def test_merge_refuses_host_settings_from_a_release(tmp_path, key):
    with pytest.raises(SystemExit):
        _merge(tmp_path, f"{key}=x\n", "COMPOSE_FILE=docker-compose.yml\n")


def test_merge_needs_compose_file_in_the_host_file(tmp_path):
    with pytest.raises(SystemExit):
        _merge(tmp_path, "", "FEEDSODT_PORT=8090\n")


def test_release_variables_reach_the_fetcher():
    """Zmienna dozwolona w wydaniu, której compose nie podaje fetcherowi, nic by nie zmieniła."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in envfile.RELEASE_VARIABLES:
        assert re.search(rf"^\s+{name}: \$\{{{name}[:-]", compose, re.M), name


def test_deploy_files_exist():
    script = (ROOT / "scripts" / "deploy" / "release.sh").read_text()
    files = re.search(r'^DEPLOY_FILES="([^"]*)"', script, re.M).group(1).split()
    assert files
    for path in files:
        assert (ROOT / path).exists(), path
        # shaper/cache/ trzyma grafy routingu na serwerze; release.sh kasuje każdą
        # ścieżkę z listy przed przeniesieniem nowej.
        assert path != "shaper" and not path.startswith(("gtfs", "osm", "shaper/cache")), path
