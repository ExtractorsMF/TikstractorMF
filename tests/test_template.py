from tikstractormf.template import render_directory


def test_render_directory_no_traversal():
    rendered = render_directory("{username}", username="../../etc/passwd")
    assert "/" not in rendered

    assert ".." not in rendered


def test_render_directory_sanitizes_slashes():
    rendered = render_directory("{username}", username="a/b")
    assert "/" not in rendered
