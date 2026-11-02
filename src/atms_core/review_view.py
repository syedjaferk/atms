"""What a controller needs to judge one incident: what the detector saw, why it is or is not trusted, and what can be done."""
from __future__ import annotations

from atms_contracts.models import AssignmentState, IncidentState

from .incident_service import SEVERITY_ORDER, Policy
from .state import ASSIGNMENT_TRANSITIONS, INCIDENT_TRANSITIONS
from .store import Incident, Store

OPERATOR_STATUSES = (AssignmentState.en_route, AssignmentState.on_scene, AssignmentState.done)


def verdict(inc: Incident, policy: Policy) -> dict:
    """Plain facts about how far the detector is trusted, so the page can explain itself and the numbers stay on the server."""
    would_auto = (inc.confidence >= policy.auto_verify_confidence
                  and SEVERITY_ORDER.index(inc.severity) >= SEVERITY_ORDER.index(policy.auto_verify_min_severity))
    verified_by = next((t.summary.removeprefix("verified by ") for t in inc.timeline if t.kind == "verified"), None)
    rejected = next((t for t in inc.timeline if t.kind == "rejected"), None)
    if rejected is not None:
        outcome = "rejected"
    elif verified_by is not None:
        outcome = "verified_automatically" if verified_by == "auto" else "verified_by_operator"
    else:
        outcome = "waiting_for_operator"
    return {
        "outcome": outcome, "verified_by": verified_by, "rejected_reason": rejected.summary if rejected else None,
        "rejected_by": rejected.actor if rejected else None,
        "confidence": inc.confidence, "severity": inc.severity.value, "lanes_closed": len(inc.lanes),
        "cameras": sorted(inc.cameras), "clips": len(inc.evidence),
        "review_confidence": policy.review_confidence, "auto_verify_confidence": policy.auto_verify_confidence,
        "auto_verify_min_severity": policy.auto_verify_min_severity.value, "would_auto_verify": would_auto,
    }


def allowed(inc: Incident) -> dict[str, bool]:
    nxt = INCIDENT_TRANSITIONS[inc.state]
    return {"verify": IncidentState.verified in nxt, "reject": IncidentState.rejected in nxt,
            "resolve": IncidentState.resolved in nxt, "close": IncidentState.closed in nxt}


def units(store: Store, inc: Incident) -> list[dict]:
    out = []
    for a in sorted(store.for_incident(inc.id), key=lambda a: a.requested_at):
        out.append({"id": str(a.id), "agency": a.agency.value, "unit_ref": a.unit_ref, "state": a.state.value, "eta_s": a.eta_s,
                    "can": [s.value for s in OPERATOR_STATUSES if s in ASSIGNMENT_TRANSITIONS[a.state]]})
    return out
