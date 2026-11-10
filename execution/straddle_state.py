"""
The straddle's state machine: every state the strategy can be in, and which moves
between states are allowed.

Why a state machine
-------------------
The strategy reacts to many independent events (quotes, order events, position events,
timers). The state answers "what am I doing right now?", and decides which reactions are
allowed: for example the stop-loss check only runs in ENTERED, and a second exit can
never start because EXITING -> EXITING is not a valid move. Writing the allowed moves
down in ONE table makes the behaviour explicit, testable, and easy to explain.

States
------
  SELECTING                 discovering contracts, waiting for the reference price
  SELECTION_FAILED          no usable ATM pair (no pairs, timeout, too far): stays idle
  EXISTING_POSITIONS_FOUND  BTC option positions already open at start: no new entry
  WAITING_FOR_ENTRY         legs selected, watching quotes until the entry check passes
  DRY_RUN_COMPLETE          dry run: entry orders were logged, never sent
  ENTRY_SUBMITTED           both entry orders sent, waiting for their outcome
  RECOVERING                entry incomplete (or timed out): closing unmatched exposure
  ENTERED                   balanced short straddle open; stop-loss/take-profit active
  EXITING                   stop-loss/take-profit hit: buying both legs back
  CLOSED                    both positions confirmed flat (final)
  ERROR                     something the strategy does not understand or could not
                            repair: no more orders, manual action required (final)

Allowed moves
-------------
  SELECTING          -> WAITING_FOR_ENTRY, SELECTION_FAILED, EXISTING_POSITIONS_FOUND, ERROR
  WAITING_FOR_ENTRY  -> ENTRY_SUBMITTED, DRY_RUN_COMPLETE, ERROR
  ENTRY_SUBMITTED    -> ENTERED, RECOVERING, ERROR
  RECOVERING         -> ENTERED, CLOSED, ERROR
  ENTERED            -> EXITING, ERROR
  EXITING            -> CLOSED, ERROR
  (SELECTION_FAILED, EXISTING_POSITIONS_FOUND, DRY_RUN_COMPLETE, CLOSED, ERROR: final)

Plain Python; used by strategies/btc_straddle.py through set_state().
"""


SELECTING = "SELECTING"
SELECTION_FAILED = "SELECTION_FAILED"
EXISTING_POSITIONS_FOUND = "EXISTING_POSITIONS_FOUND"
WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
RECOVERING = "RECOVERING"
ENTERED = "ENTERED"
EXITING = "EXITING"
CLOSED = "CLOSED"
ERROR = "ERROR"

ALLOWED_TRANSITIONS = {
    SELECTING: (WAITING_FOR_ENTRY, SELECTION_FAILED, EXISTING_POSITIONS_FOUND, ERROR),
    WAITING_FOR_ENTRY: (ENTRY_SUBMITTED, DRY_RUN_COMPLETE, ERROR),
    ENTRY_SUBMITTED: (ENTERED, RECOVERING, ERROR),
    RECOVERING: (ENTERED, CLOSED, ERROR),
    ENTERED: (EXITING, ERROR),
    EXITING: (CLOSED, ERROR),
    SELECTION_FAILED: (),
    EXISTING_POSITIONS_FOUND: (),
    DRY_RUN_COMPLETE: (),
    CLOSED: (),
    ERROR: (),
}

# States in which the strategy may hold positions it is responsible for.
STATES_WITH_POSITIONS = (ENTRY_SUBMITTED, RECOVERING, ENTERED, EXITING)

# States from which nothing more will happen automatically.
FINAL_STATES = (SELECTION_FAILED, EXISTING_POSITIONS_FOUND, DRY_RUN_COMPLETE, CLOSED, ERROR)


class StraddleStateMachine:
    """
    Holds the current state and refuses moves that are not in ALLOWED_TRANSITIONS.

    Attributes
    ----------
    state (str): the current state.
    history (list[tuple[str, str, str]]): every accepted move as (from, to, reason).
    refused (list[tuple[str, str, str]]): every refused move as (from, to, reason).
    """

    def __init__(self, initial_state=SELECTING):
        self.state = initial_state
        self.history = []
        self.refused = []

    def can_move_to(self, new_state):
        """True if the move from the current state to new_state is allowed."""
        return new_state in ALLOWED_TRANSITIONS[self.state]

    def move_to(self, new_state, reason):
        """
        Move to new_state if allowed.

        Parameters: new_state (str); reason (str, kept in the history for the log).
        Returns: True if the move happened, False if it was refused (the state is then
        unchanged and the refusal is recorded). Refusing instead of raising keeps the
        strategy running; the caller logs the refusal as an error.
        """
        if not self.can_move_to(new_state):
            self.refused.append((self.state, new_state, reason))
            return False
        self.history.append((self.state, new_state, reason))
        self.state = new_state
        return True

    def is_final(self):
        return self.state in FINAL_STATES

    def may_hold_positions(self):
        return self.state in STATES_WITH_POSITIONS
