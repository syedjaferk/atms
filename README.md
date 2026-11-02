# ATMS

Accident detection and emergency response. Design: [docs/architecture-flows.md](docs/architecture-flows.md).

## Layout

| Path | What |
|---|---|
| `src/atms_contracts/` | Event models (source of truth), topic helpers, schema export |
| `contracts/events/` | JSON Schemas generated from the models, one per event |
| `contracts/openapi.yaml` | Incident API |
| `contracts/topics.md` | MQTT, AMQP and Kafka naming rules |
| `src/atms_core/` | The services: bridge, correlator, incident service, orchestrator, hub, traffic, realtime, API, Kafka bus, outbox |
| `src/atms_forecast/` | Congestion forecasting: synthetic traffic, features, models, registry, service |
| `src/atms_edge/` | Hugging Face accident detector (two stages) and frame sampling |
| `src/atms_sim/` | Edge controller, fake agencies, fake responders |
| `src/atms_core/migrations/` | Postgres schema, numbered SQL files |
| `compose.yaml`, `rabbitmq/` | Dev RabbitMQ, Kafka and Postgres with PostGIS |

## Everyday commands

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/python -m atms_contracts.export        # regenerate schemas after changing models
.venv/bin/pytest                                  # unit tests
docker compose up -d --wait
ATMS_BROKER_HOST=localhost .venv/bin/pytest -m integration
.venv/bin/python -m atms_sim.cli list
.venv/bin/python -m atms_sim.cli edge --scenario major_two_vehicle
.venv/bin/python -m atms_sim.cli agencies --port 9100
.venv/bin/python -m atms_sim.cli sms --port 9200     # fake SMS gateway with signed receipts
.venv/bin/python -m atms_core.main                # bridge, services and API on :8000 (dev token ops-dev)
ATMS_DATABASE_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/python -m atms_core.main   # same, state kept in Postgres
ATMS_KAFKA_BOOTSTRAP=localhost:9092 ATMS_DATABASE_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/python -m atms_core.main   # services talk over Kafka
ATMS_DB_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/pytest   # whole suite on Postgres
ATMS_KAFKA=localhost:9092 ATMS_DB_URL=postgresql://atms:atms-dev@localhost:5433/atms ATMS_BROKER_HOST=localhost .venv/bin/pytest   # everything on real infrastructure
```

Try it end to end

```bash
docker compose up -d --wait
.venv/bin/python -m atms_sim.cli agencies --port 9100 &
.venv/bin/python -m atms_core.main &
.venv/bin/python -m atms_sim.cli edge --scenario major_two_vehicle
curl -H "Authorization: Bearer ops-dev" localhost:8000/v1/incidents
```

## Detection with Hugging Face models

Stage 1 `hilmantm/detr-traffic-accident-detection` (Apache 2.0) finds accident regions. Stage 2
`PekingU/rtdetr_v2_r18vd` counts vehicles around the region. An accident must appear in at least
2 sampled frames. Severity and lanes are rules on top, tuned per camera.

```bash
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install -e ".[vision]"
.venv/bin/python -m atms_edge.cli analyze --camera GNT001 --video clip.mp4 --publish
ATMS_RUN_HF=1 .venv/bin/pytest tests/test_detector.py -k real   # downloads the models
```

Known limits seen on the model card demo images (CPU, 222 px frames): the model found both
accidents (0.63 and 0.96) and correctly ignored one traffic jam, but scored the other jam as an
accident at 0.70. Single frames are not enough, so 2 frame confirmation is on by default. The
thresholds must be calibrated on footage from the real cameras before auto-verify is trusted.

## Congestion forecasting

```bash
.venv/bin/python -m atms_forecast.train --registry models --days 70      # train, backtest, promote if it passes
ATMS_FORECAST_MODEL_DIR=models .venv/bin/python -m atms_core.main
.venv/bin/python -m atms_sim.cli sensors --minutes 180                    # replay road readings ending now
curl -H "Authorization: Bearer ops-dev" "localhost:8000/v1/forecast/NH48-S03?lanes_closed=2"
```

What-if simulation

```bash
curl -X POST -H "Authorization: Bearer ops-dev" -H "content-type: application/json" localhost:8000/v1/whatif -d '{"horizon_min": 240, "scenarios": [{"name": "close 2 lanes", "incidents": [{"segment": "NH48-S03", "at_min": 30, "duration_min": 90, "lanes_closed": 2}]}]}'
curl -X POST -H "Authorization: Bearer ops-dev" localhost:8000/v1/incidents/<incident id>/whatif
```

## Operator console

```bash
docker compose up -d rabbitmq
.venv/bin/python -m atms_sim.cli agencies --port 9100 &
ATMS_FORECAST_MODEL_DIR=models .venv/bin/python -m atms_core.main
.venv/bin/python -m atms_sim.cli sensors --incident                       # road readings with an incident in them
.venv/bin/python -m atms_sim.cli edge --scenario major_two_vehicle        # a detection from the camera
```

Open http://localhost:8000/console/ and sign in with an operator token (the development token is `ops-dev`).
The map shows the highway by speed, now or 15, 30 and 60 minutes ahead, the message boards and what they say,
facilities, open incidents and the route of every unit sent, over OpenStreetMap tiles (the Streets button turns them off). The side panel lists open incidents and lets an
operator verify, reject, resolve, close, add a note and compare ways to handle the incident on the traffic twin.

## Controller area

Open http://localhost:8000/console/controller.html after the console steps above, and send a clip a browser can play.

```bash
.venv/bin/python -m atms_sim.cli edge --scenario needs_operator --playable    # 74 percent, waits for a person
.venv/bin/python -m atms_sim.cli edge --scenario major_two_vehicle --playable  # 94 percent, the detector acts alone
```

The queue shows what needs review. Pick an incident to watch the clip, read why the detector is or is not trusted,
see the traffic suggestion, then verify, reject, resolve, close, update a unit's status, retry a failed notice or add a note.
Clips are read from `ATMS_MEDIA_DIR` (default `simulator-out`) through short lived signed links.


## Video checker

Runs the Hugging Face accident models over real CCTV footage and, with `--publish`, raises the alarm the way a camera would.
Put a video, or a folder of videos or numbered frames, in `videos/`. Folders named `Accident` and `Non Accident` are read as labels,
so each run also reports accuracy, precision and recall.

```bash
.venv/bin/python -m atms_edge.cli check videos/archive/data/val --limit 12
.venv/bin/python -m atms_edge.cli check videos/archive/data/val/Accident --limit 6 --cameras GNT003,GNT005,GNT007 --publish
```

Each confirmed accident is stitched into a playable clip, placed at one of the mock cameras (`GNT001` to `GNT007`, listed in
`atms_contracts/cameras.py`) and sent over MQTT. The core platform then creates the incident, picks the nearest facilities,
asks the agencies and sends the notices. The command prints what happened afterwards.

On the operator map every camera is drawn with a cone for where it looks, and the one that reported turns red. Units that have
accepted drive from their station to the crash along the real road (route from the public OSRM demo server, straight line if it
cannot be reached), and their coloured dot moves with the ETA they gave. Stations that were notified pulse, and the box at the top
left lists every alert sent to officials with its delivery state.

Limits worth knowing. The pretrained accident model misses many clips and sometimes calls normal traffic an accident, so the
checker prints the labelled result every time. Its confidences (about 0.65 to 0.9) mostly fall below the 0.90 auto-verify line,
so most detections wait for an operator to verify, which is what starts the dispatch and the notices. The dataset is still frames, not
video, so a clip is a few seconds apart frames. The unit position is an estimate from the ETA, not a GPS feed.
# atms
