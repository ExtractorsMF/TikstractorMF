from tikstractormf import Media, Post, Profile, Tiktokscraper, __version__


def test_imports():
    assert __version__
    assert Tiktokscraper is not None
    assert Post and Media and Profile


def test_cli_version():
    from typer.testing import CliRunner

    from tikstractormf.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "tikstractormf" in result.output
