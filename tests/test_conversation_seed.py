"""Packaged and host-selected conversation-seed tests."""

from json import dumps as json_dumps

from pytest import raises as pytest_raises

from engram.conversation_seed import (
    conversation_seed_pairs_from_text,
    load_bundled_conversation_pairs,
    load_conversation_pairs,
    require_conversation_catch_all,
)


def test_bundled_conversation_seed_is_packaged_and_has_a_catch_all() -> None:
    pairs = load_bundled_conversation_pairs()

    assert len(pairs) == 328
    require_conversation_catch_all(pairs)


def test_host_selected_conversation_seed_loads_from_an_explicit_path(tmp_path) -> None:
    path = tmp_path / "conversation.json"
    expected = [{"pattern": "*", "response": "Ready."}]
    path.write_text(json_dumps({"pairs": expected}), encoding="utf-8")

    assert load_conversation_pairs(str(path)) == expected


def test_conversation_seed_rejects_unknown_root_fields_and_missing_catch_all() -> None:
    with pytest_raises(ValueError, match="only pairs"):
        conversation_seed_pairs_from_text('{"pairs": [], "extra": true}')
    with pytest_raises(ValueError, match="catch-all"):
        require_conversation_catch_all([{"pattern": "HELLO", "response": "Hello."}])
