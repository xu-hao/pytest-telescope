from pathlib import Path


def test_project_file():
    assert Path("data/project-note.txt").read_text().strip()
