"""
Unit tests for execution/straddle_state.py: the allowed moves of the state machine.
"""

import pytest

from execution import straddle_state as s
from execution.straddle_state import StraddleStateMachine


def machine_in(state):
    machine = StraddleStateMachine()
    machine.state = state
    return machine


def test_starts_in_selecting():
    assert StraddleStateMachine().state == s.SELECTING


def test_happy_path_level_2():
    machine = StraddleStateMachine()
    for new_state in (s.WAITING_FOR_ENTRY, s.ENTRY_SUBMITTED, s.ENTERED, s.EXITING, s.CLOSED):
        assert machine.move_to(new_state, "test") is True
    assert machine.state == s.CLOSED
    assert len(machine.history) == 5
    assert machine.is_final()


def test_recovery_paths():
    keep = machine_in(s.ENTRY_SUBMITTED)
    assert keep.move_to(s.RECOVERING, "incomplete")
    assert keep.move_to(s.ENTERED, "matched straddle remains")

    flat = machine_in(s.ENTRY_SUBMITTED)
    assert flat.move_to(s.RECOVERING, "incomplete")
    assert flat.move_to(s.CLOSED, "nothing matched")


@pytest.mark.parametrize(
    "start, forbidden",
    [
        (s.ENTERED, s.ENTERED),           # no second entry
        (s.EXITING, s.EXITING),           # no duplicate exit
        (s.EXITING, s.ENTERED),           # an exit cannot be undone
        (s.WAITING_FOR_ENTRY, s.ENTERED),  # must submit first
        (s.CLOSED, s.ENTRY_SUBMITTED),    # final
        (s.ERROR, s.EXITING),             # final: no automatic action after ERROR
        (s.DRY_RUN_COMPLETE, s.ENTRY_SUBMITTED),
        (s.EXISTING_POSITIONS_FOUND, s.WAITING_FOR_ENTRY),
    ],
)
def test_forbidden_moves_are_refused_and_recorded(start, forbidden):
    machine = machine_in(start)
    assert machine.move_to(forbidden, "should be refused") is False
    assert machine.state == start
    assert machine.refused == [(start, forbidden, "should be refused")]


@pytest.mark.parametrize("start", [s.SELECTING, s.WAITING_FOR_ENTRY, s.ENTRY_SUBMITTED, s.RECOVERING, s.ENTERED, s.EXITING])
def test_error_reachable_from_every_active_state(start):
    assert machine_in(start).move_to(s.ERROR, "test")


def test_final_states_have_no_exit():
    for state in s.FINAL_STATES:
        assert s.ALLOWED_TRANSITIONS[state] == ()


def test_states_with_positions():
    assert machine_in(s.ENTERED).may_hold_positions()
    assert machine_in(s.RECOVERING).may_hold_positions()
    assert not machine_in(s.WAITING_FOR_ENTRY).may_hold_positions()
    assert not machine_in(s.CLOSED).may_hold_positions()


def test_every_state_has_a_transition_entry():
    for state in (s.SELECTING, s.SELECTION_FAILED, s.EXISTING_POSITIONS_FOUND, s.WAITING_FOR_ENTRY,
                  s.DRY_RUN_COMPLETE, s.ENTRY_SUBMITTED, s.RECOVERING, s.ENTERED, s.EXITING, s.CLOSED, s.ERROR):
        assert state in s.ALLOWED_TRANSITIONS
