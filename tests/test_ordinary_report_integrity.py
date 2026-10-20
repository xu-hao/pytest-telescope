"""Selection errors and transport incidents preserve independent inventory facts."""

from test_ordinary_failures import run_report, suites, write


def test_unmatched_selector_preserves_completed_membership_and_routes(tmp_path):
    suites(tmp_path)
    write(tmp_path, "tests/b/test_bad.py", "def test_actual(): pass\n")
    result = run_report(
        tmp_path,
        arguments=("tests/a/test_first.py::test_first", "tests/b/test_bad.py::test_absent"),
    )
    assert result["kind"] == "run" and result["status"] == "declaration_error"
    assert result["inventory_complete"] is True
    assert result["routing_complete"] is True
    assert result["grouping_complete"] is True
    assert result["inventory_digest"] is not None
    assert len(result["units"]) == 3
    assert result["counts"]["executed"] == 1
    assert result["counts"]["not-selected"] == 2
    assert result["collection"]["outcome"] == "executed"
    assert all(row["route"] == "ordinary" for row in result["units"])


def test_collection_aggregate_preserves_transport_incident(tmp_path):
    suites(tmp_path)
    write(
        tmp_path,
        "tests/b/test_bad.py",
        'import os\ndef test_bad(): os.write(1, b"TELESCOPE_RESULT {}\\n")\n',
    )
    result = run_report(tmp_path, arguments=("-s",))
    assert result["status"] == "infrastructure_error"
    assert result["collection"]["outcome"] == "infrastructure_error"
    assert result["collection"]["suites"][-1]["outcome"] == "not-started"
