from conftest import make_workbook, node
from typer.testing import CliRunner

from value_mapper.cli import app

runner = CliRunner()


def test_help_lists_both_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "get" in result.output and "update" in result.output


def test_get_prints_summary(make_site, tmp_path):
    make_site([node("a", 1, {"subject": ["Texas"]})])
    result = runner.invoke(app, ["get", "-o", str(tmp_path / "m.xlsx"), "-k", "subject"])
    assert result.exit_code == 0, result.output
    assert "Read 1 ADO(s)" in result.output
    assert (tmp_path / "m.xlsx").is_file()


def test_unknown_key_is_rejected_by_the_cli():
    result = runner.invoke(app, ["get", "-k", "label"])
    assert result.exit_code != 0


def test_missing_credentials_exit_1(tmp_path):
    result = runner.invoke(app, ["get", "-i", "prod", "-o", str(tmp_path / "m.xlsx")])
    assert result.exit_code == 1
    assert "ARCHIPELAGO_PROD_USER" in result.output


def test_update_dry_run(make_site, tmp_path):
    site = make_site([node("a", 1, {"subject": ["Texas"]})])
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = runner.invoke(app, ["update", "-x", str(workbook), "--dry-run", "-o", str(tmp_path / "r.csv")])
    assert result.exit_code == 0, result.output
    assert "Would update 1 ADO(s)" in result.output
    assert site.patches == []


def test_update_with_failures_exits_1(make_site, tmp_path):
    make_site([node("a", 1, {"subject": ["Texas"]})], patch_status={"a": 500})
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = runner.invoke(app, ["update", "-x", str(workbook), "-o", str(tmp_path / "r.csv")])
    assert result.exit_code == 1
    assert "1 change(s) failed" in result.output


def test_update_bad_workbook_is_a_clean_error(tmp_path):
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", None, 1, 1)]})
    result = runner.invoke(app, ["update", "-x", str(workbook)])
    assert result.exit_code == 1
    assert "nothing to do" in result.output
