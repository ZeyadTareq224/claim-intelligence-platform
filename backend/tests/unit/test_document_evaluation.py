from app.document_evaluation import distance, fields, normalized


def test_error_distance_for_characters_and_words():
    assert distance("kitten", "sitting") == 3
    assert distance([], ["one", "two"]) == 2
    assert distance(["one", "two"], ["one", "three"]) == 1


def test_normalization_and_exact_field_extraction():
    value = normalized("  policy number P-1001\nSUM   INSURED EGP 12000 ")
    assert value == "POLICY NUMBER P-1001 SUM INSURED EGP 12000"
    assert fields(value) == {"policy_number": "P-1001", "amount": "EGP 12000"}
    assert fields("POLICY UNKNOWN") == {"policy_number": None, "amount": None}


def test_fixture_generator_is_repeatable_with_current_library(tmp_path, monkeypatch):
    import hashlib
    from fixtures.pdf import generate

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(generate, "ROOT", first)
    generate.main()
    monkeypatch.setattr(generate, "ROOT", second)
    generate.main()
    for path in first.glob("*.pdf"):
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((second / path.name).read_bytes()).digest()
