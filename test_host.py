import pytest
from datetime import date
from common import (
    fuzzy_person_id,
    ShoppingItem,
    Commitment,
    AppState,
    PERSON_LABELS
)
from shopping_list import fuzzy_dedup
from check_commitments import parse_deadline
from host import handle_local_tool

def test_fuzzy_person_id():
    assert fuzzy_person_id("divyanshu garg") == "person_1"
    assert fuzzy_person_id("dvinix") == "person_2"
    assert fuzzy_person_id("divyanshu") == "person_1"  # substring match

def test_fuzzy_dedup():
    items = [
        ShoppingItem(name="apples", added_by="person_1"),
        ShoppingItem(name="milk", added_by="person_1"),
        ShoppingItem(name="bread", added_by="person_1", bought=True)
    ]
    # Exact match
    assert fuzzy_dedup(items, "apples").name == "apples"
    # Fuzzy match
    assert fuzzy_dedup(items, "aples").name == "apples"
    # Bought items shouldn't match
    assert fuzzy_dedup(items, "bread") is None
    # No match
    assert fuzzy_dedup(items, "oranges") is None

def test_parse_deadline():
    base_date = date(2026, 7, 10)  # Friday
    
    assert parse_deadline("today", base_date) == date(2026, 7, 10)
    assert parse_deadline("tomorrow", base_date) == date(2026, 7, 11)
    # Next monday should be July 13
    assert parse_deadline("monday", base_date) == date(2026, 7, 13)
    # "by monday" should also be July 13
    assert parse_deadline("by monday", base_date) == date(2026, 7, 13)

def test_handle_local_tool():
    app_state = AppState(
        commitments=[],
        shopping_items=[],
        active_person="person_1",
        active_label="Divyanshu Garg"
    )

    # Test record_commitment
    result = handle_local_tool(
        "record_commitment",
        {"task": "Buy groceries", "person": "dvinix", "deadline_guess": "tomorrow"},
        app_state
    )
    assert "Committed: Buy groceries" in result
    assert len(app_state.commitments) == 1
    assert app_state.commitments[0].person == "person_2"  # dvinix mapped to person_2
    
    # Test add_shopping_item
    result = handle_local_tool(
        "add_shopping_item",
        {"name": "Milk", "quantity": "2 liters"},
        app_state
    )
    assert "Added 'Milk'" in result
    assert len(app_state.shopping_items) == 1
    assert app_state.shopping_items[0].name == "Milk"
