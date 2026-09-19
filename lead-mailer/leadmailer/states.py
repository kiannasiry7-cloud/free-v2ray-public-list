"""Lead status flow. Every change goes through check_transition(); nothing is ever deleted."""

CANDIDATE = "candidate"
DRAFTED = "drafted"
APPROVED = "approved"
QUEUED = "queued"
SENT = "sent"
REJECTED_DUPLICATE = "rejected_duplicate"
QUARANTINED = "quarantined"

ALL = (CANDIDATE, DRAFTED, APPROVED, QUEUED, SENT, REJECTED_DUPLICATE, QUARANTINED)

# from -> allowed targets. Quarantine is reachable from every working state.
# The only way out of quarantine is an explicit `release` back to candidate.
ALLOWED = {
    CANDIDATE: {DRAFTED, QUARANTINED},
    DRAFTED: {APPROVED, QUARANTINED},
    APPROVED: {QUEUED, QUARANTINED},
    QUEUED: {SENT, QUARANTINED},
    SENT: set(),
    REJECTED_DUPLICATE: set(),
    QUARANTINED: {CANDIDATE},
}


class IllegalTransition(Exception):
    pass


def check_transition(current: str, target: str) -> None:
    if target not in ALLOWED.get(current, set()):
        raise IllegalTransition(f"{current} -> {target} is not allowed")
