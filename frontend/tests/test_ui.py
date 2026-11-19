"""Smoke-test the actual Streamlit CRUD flow against a disposable migrated database."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def ui(monkeypatch):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("set TEST_DATABASE_URL to a separate migrated PostgreSQL database")
    if test_url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", test_url)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "main.py"), default_timeout=20).run()
    assert not app.exception
    return app


def test_organization_crud_and_all_screens(ui):
    code = f"UI{uuid4().hex[:12]}"
    ui.text_input(key="create_Organizations_name").set_value("UI Test")
    ui.text_input(key="create_Organizations_code").set_value(code)
    ui.button[0].click().run()
    assert not ui.exception and ui.success

    # Select the record explicitly; other test data may be present.
    ui.selectbox(key="select_Organizations").select_index(next(
        i for i, label in enumerate(ui.selectbox(key="select_Organizations").options) if code in label
    )).run()
    edit = next(x for x in ui.text_input if x.key.startswith("edit_") and x.key.endswith("_name"))
    edit.set_value("Updated UI Test")
    next(x for x in ui.button if x.label == "Save changes").click().run()
    assert not ui.exception and ui.success

    next(x for x in ui.checkbox if x.key.startswith("confirm_")).check().run()
    next(x for x in ui.button if x.label == "Delete").click().run()
    assert not ui.exception and ui.success

    for name in ("Vehicles", "Policies", "Policy clauses", "Claims", "Claim documents", "Policy documents", "Claim analyses"):
        ui.radio[0].set_value(name).run()
        assert not ui.exception and not ui.error, name
