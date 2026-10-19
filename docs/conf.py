"""Sphinx configuration for the public pytest-telescope documentation."""

project = "pytest-telescope"
author = "pytest-telescope contributors"
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]
myst_heading_anchors = 3

html_theme = "furo"
html_title = project
html_theme_options = {
    "source_repository": "https://github.com/xu-hao/pytest-telescope/",
    "source_branch": "main",
    "source_directory": "docs/",
}
