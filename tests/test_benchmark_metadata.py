"""Source-binding checks for machine-readable benchmark evidence."""

from scripts.benchmark_metadata import SOURCE_FILES, SOURCE_GLOBS, governed_source_sha256


def source_tree(root) -> None:
    for root_name, patterns in SOURCE_GLOBS:
        directory = root / root_name
        directory.mkdir(parents=True, exist_ok=True)
        for pattern in patterns:
            suffix = pattern.removeprefix("*")
            (directory / f"governed{suffix}").write_text("initial\n", encoding="utf-8")
    for relative in SOURCE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial\n", encoding="utf-8")


def test_governed_digest_includes_evaluation_manifests_and_operational_configuration(tmp_path) -> None:
    source_tree(tmp_path)
    original = governed_source_sha256(tmp_path)

    (tmp_path / "eval" / "governed.json").write_text('{"changed": true}\n', encoding="utf-8")
    evaluation_changed = governed_source_sha256(tmp_path)
    (tmp_path / "config.example.yml").write_text("changed: true\n", encoding="utf-8")
    configuration_changed = governed_source_sha256(tmp_path)

    assert evaluation_changed != original
    assert configuration_changed != evaluation_changed


def test_governed_digest_includes_packaged_runtime_corpora(tmp_path) -> None:
    source_tree(tmp_path)
    corpus = tmp_path / "engram" / "data" / "rewrite.json"
    corpus.parent.mkdir(parents=True)
    corpus.write_text('{"version": 1}\n', encoding="utf-8")
    original = governed_source_sha256(tmp_path)

    corpus.write_text('{"version": 2}\n', encoding="utf-8")

    assert governed_source_sha256(tmp_path) != original
