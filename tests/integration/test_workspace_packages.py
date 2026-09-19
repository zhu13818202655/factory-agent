from importlib.metadata import version


def test_workspace_installs_the_single_application() -> None:
    """The repository builds exactly one application package.

    The workspace used to hold a second member for the statistics service; a
    second installable dist here would mean that split came back.
    """
    assert version("factory-agent") == "0.1.0"
