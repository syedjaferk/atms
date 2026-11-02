import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

from atms_contracts import export
from atms_contracts.models import (
    EVENT_TYPES, AccidentDetected, AccidentDetectedData, Location, Severity, Site,
    UnknownEventType, make_dedup_key, parse_event,
)
from atms_contracts.models import (
    Agency, AssignmentAcked, AssignmentAckedData, AssignmentDeclined, AssignmentDeclinedData,
    AssignmentRequested, AssignmentRequestedData, AssignmentState, AssignmentTimedOut,
    AssignmentTimedOutData, IncidentClosed, IncidentClosedData, IncidentCreated, IncidentCreatedData,
    IncidentRejected, IncidentRejectedData, IncidentResolved, IncidentResolvedData, IncidentState,
    IncidentUpdated, IncidentUpdatedData, AssignmentCancelled, AssignmentCancelledData,
    IncidentVerified, IncidentVerifiedData, CongestionForecast, CongestionForecastData, NotificationChannel, NotificationDelivered, NotificationDeliveredData,
    NotificationFailed, NotificationFailedData, NotificationSent, NotificationSentData, ResponderStatus, ResponderStatusChanged,
    ResponderStatusChangedData, TrafficPlanApplied, TrafficPlanAppliedData, TrafficPlanReverted,
    TrafficPlanRevertedData, VideoReady, VideoReadyData,
)

ROOT = Path(__file__).resolve().parents[1]
SITE = Site(state="MH", city="PUNE", road="NH48", camera_id="GNT001")
LOC = Location(lat=18.4575, lon=73.8508)
AID, IID = uuid4(), uuid4()


def samples():
    p = "test"
    return [
        AccidentDetected(producer=p, data=AccidentDetectedData(
            site=SITE, location=LOC, heading_deg=350, confidence=0.94, severity=Severity.major,
            vehicles_involved=2, affected_lanes=["LANE_1"], model_version="m1")),
        VideoReady(producer=p, data=VideoReadyData(camera_id="GNT001", video_ref="clips/a.mp4",
                                                   duration_s=60, size_bytes=10, sha256="a" * 64)),
        IncidentCreated(producer=p, incident_id=IID, data=IncidentCreatedData(
            state=IncidentState.detected, severity=Severity.major, confidence=0.9, location=LOC,
            road_segment="MH/PUNE/NH48", detection_event_ids=[uuid4()])),
        IncidentUpdated(producer=p, incident_id=IID, data=IncidentUpdatedData(
            reason="second camera", confidence=0.95, severity=Severity.major,
            affected_lanes=["LANE_1"], detection_event_ids=[uuid4()])),
        AssignmentCancelled(producer=p, incident_id=IID, data=AssignmentCancelledData(
            assignment_id=AID, agency=Agency.police, reason="rejected")),
        IncidentVerified(producer=p, incident_id=IID, data=IncidentVerifiedData(verified_by="auto")),
        IncidentRejected(producer=p, incident_id=IID,
                         data=IncidentRejectedData(rejected_by="operator", reason="debris", operator_id="op1")),
        AssignmentRequested(producer=p, incident_id=IID, data=AssignmentRequestedData(
            assignment_id=AID, agency=Agency.ambulance, idempotency_key="k1", attempt=1)),
        AssignmentAcked(producer=p, incident_id=IID, data=AssignmentAckedData(
            assignment_id=AID, agency=Agency.ambulance, unit_ref="AMB-001", eta_s=300)),
        AssignmentDeclined(producer=p, incident_id=IID, data=AssignmentDeclinedData(
            assignment_id=AID, agency=Agency.tow, reason="none free")),
        AssignmentTimedOut(producer=p, incident_id=IID, data=AssignmentTimedOutData(
            assignment_id=AID, agency=Agency.police, attempt=1)),
        ResponderStatusChanged(producer=p, incident_id=IID, data=ResponderStatusChangedData(
            assignment_id=AID, agency=Agency.police, previous=AssignmentState.acked,
            status=ResponderStatus.en_route, eta_s=240, incident_state=IncidentState.dispatched)),
        TrafficPlanApplied(producer=p, incident_id=IID, data=TrafficPlanAppliedData(
            plan_id=uuid4(), lanes_closed=["LANE_1"], vms_messages={"VMS-12": "ACCIDENT AHEAD"})),
        TrafficPlanReverted(producer=p, incident_id=IID, data=TrafficPlanRevertedData(plan_id=uuid4())),
        IncidentResolved(producer=p, incident_id=IID, data=IncidentResolvedData(resolved_by="op1")),
        IncidentClosed(producer=p, incident_id=IID, data=IncidentClosedData(report_ref="reports/1.pdf")),
        CongestionForecast(producer=p, data=CongestionForecastData(
            segment_id="NH48-S03", horizon_min=30, level="heavy", predicted_speed_kmh=48.0, low_kmh=40.0, high_kmh=57.0,
            free_flow_kmh=100.0, model_version="v1", main_factor="evening peak")),
        NotificationSent(producer=p, incident_id=IID, data=NotificationSentData(
            notification_id=uuid4(), contact_id="duty-nh48", channel=NotificationChannel.sms, provider="primary", attempt=1)),
        NotificationDelivered(producer=p, incident_id=IID, data=NotificationDeliveredData(
            notification_id=uuid4(), contact_id="duty-nh48", channel=NotificationChannel.sms, provider="primary")),
        NotificationFailed(producer=p, incident_id=IID, data=NotificationFailedData(
            notification_id=uuid4(), contact_id="duty-nh48", recipient="Duty officer NH48",
            channel=NotificationChannel.sms, reason="provider down", attempts=4)),
    ]


def test_samples_cover_every_event_type():
    assert {s.type for s in samples()} == set(EVENT_TYPES)


@pytest.mark.parametrize("event", samples(), ids=lambda e: e.type)
def test_event_round_trips_and_matches_json_schema(event):
    raw = event.model_dump_json()
    assert parse_event(raw) == event
    schema = json.loads((ROOT / "contracts" / "events" / f"{event.type}.schema.json").read_text())
    jsonschema.validate(json.loads(raw), schema, format_checker=jsonschema.FormatChecker())


def test_schema_files_are_not_stale():
    assert export.main(["--check"]) == 0


def test_unknown_type_is_rejected():
    with pytest.raises(UnknownEventType):
        parse_event({"type": "nope"})


def test_extra_fields_are_rejected():
    obj = json.loads(samples()[0].model_dump_json())
    obj["data"]["surprise"] = 1
    with pytest.raises(Exception):
        parse_event(obj)


def test_naive_timestamp_is_rejected():
    obj = json.loads(samples()[0].model_dump_json())
    obj["occurred_at"] = "2026-11-01T10:00:00"
    with pytest.raises(Exception):
        parse_event(obj)


def test_confidence_out_of_range_is_rejected():
    obj = json.loads(samples()[0].model_dump_json())
    obj["data"]["confidence"] = 1.4
    with pytest.raises(Exception):
        parse_event(obj)


def test_dedup_key_buckets_by_ten_seconds():
    t = datetime(2026, 11, 1, 10, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 1, 10, 0, 9, tzinfo=timezone.utc)
    t3 = datetime(2026, 11, 1, 10, 0, 11, tzinfo=timezone.utc)
    k = make_dedup_key("GNT001", "accident.detected", t)
    assert k == make_dedup_key("GNT001", "accident.detected", t2)
    assert k != make_dedup_key("GNT001", "accident.detected", t3)
    assert k != make_dedup_key("GNT002", "accident.detected", t)


def test_openapi_is_valid_and_has_every_documented_operation():
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    validate(spec)
    ops = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert {"listIncidents", "getIncident", "verifyIncident", "rejectIncident", "closeIncident",
            "updateAssignment", "resolveIncident", "listNotifications", "retryNotification", "receiveReceipt", "postReadings", "postWeather",
            "whatIf", "incidentWhatIf", "forecastOverview", "forecastModel", "forecastAccuracy", "forecastSegment", "addNote", "getEvidence", "getReview", "getMap", "stream"} <= ops


def test_openapi_enums_match_the_models():
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())["components"]["schemas"]
    assert spec["IncidentState"]["enum"] == [s.value for s in IncidentState]
    assert spec["AssignmentState"]["enum"] == [s.value for s in AssignmentState]
    assert spec["ResponderStatus"]["enum"] == [s.value for s in ResponderStatus]
    assert spec["Severity"]["enum"] == [s.value for s in Severity]
    assert spec["Agency"]["enum"] == [s.value for s in Agency]
