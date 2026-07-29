from app.api.file import _get_file_extension, _sanitize_filename


def test_filename_sanitization_blocks_path_traversal() -> None:
    assert _sanitize_filename("../cpu report.md") == ".._cpu_report.md"
    assert _sanitize_filename('cpu:"report".MD') == "cpu__report_.MD"


def test_file_extension_is_case_insensitive() -> None:
    assert _get_file_extension("runbook.MD") == "md"
    assert _get_file_extension("README") == ""
