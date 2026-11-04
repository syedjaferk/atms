# ATMS

ATMS watches highway CCTV cameras. When the AI sees an accident on a camera, the system opens an incident, sends police, ambulance, highway patrol, fire and tow units from the nearest stations, tells those stations and the duty officer, and shows all of it live on a map.

Every part runs as its own container, so you can start the whole thing with one command and watch it work.

## See it in two minutes

You need Docker with Compose, about 6 GB of free disk, and the `videos/` folder with your recordings next to this file.

```bash
scripts/start.sh
```

It checks that Docker runs, picks a free port for the console so a clash cannot stop it, starts everything, and prints the address. If something fails to start it shows the status and the last log lines of every part, so you see why. `docker compose up --build` does the same on port 8000.

The first run builds the images (the detector image holds PyTorch, so it is large) and downloads the AI models, which took about a minute and a half to start on a fresh machine once the images were built. Allow ten to twenty minutes in total on a slow connection. Later runs start in under a minute. When the console is up, open it.

```
http://localhost:8000/console/        token  ops-dev      (the script prints the real port)
```

1. Open the **CCTV** page. Pick a city at the top of the map, then pick a camera.
2. Each camera has its own videos. **Good** ones show normal traffic. **Bad** ones show an accident. Pick a bad one.
3. Press **Check with AI and raise the alarm**. You watch the AI look at the video frame by frame.
4. If it confirms an accident, the page then follows the platform. An incident opens, stations are asked to send units, and notices go out. Press **See it on the map** and watch the units drive to the crash.

If the AI is below 90 percent sure, a person has to verify first. That is on purpose. The page shows a **Verify as operator** button for it.

## What happens, in plain words

```
 video  ->  detector node  ->  broker  ->  core platform  ->  agencies and SMS
 (a camera)  (AI models)       (MQTT)      (incident, dispatch,   (stations get told,
                                            notices, map)          units start driving)
```

1. A camera has a recorded video. The **detector node** takes frames from it and runs two AI models. One finds accident regions, the other counts the vehicles around them.
2. An accident only counts when it shows in several frames. This removes most one frame mistakes.
3. The node cuts a short clip around the worst moment and publishes an `accident.detected` event and a `video.ready` event to the **broker** (RabbitMQ over MQTT). The event carries the camera, so it carries the place.
4. The **core** reads the event. If another camera reported the same crash within 300 metres and 10 minutes, the reports merge into one incident.
5. The incident starts as `detected`. If the AI is at least 90 percent sure and the crash is major or worse, it is verified by itself. Otherwise it waits for an operator on the console. Below 60 percent it is only logged.
6. When an incident is verified, the **dispatch** step picks the nearest station of each agency that the severity needs, and asks that agency's system to send a unit. A minor crash needs police and tow. A major one adds ambulance and highway patrol. A critical one adds fire.
7. Each agency answers with a unit and an arrival time. The time comes from the real distance to the crash.
8. The **notification** step tells people. The duty officer of that area gets an SMS. The nearest stations of each dispatched agency get a signed webhook. Every notice is stored first, sent next, and kept until delivery is confirmed.
9. **Traffic management** puts a warning on the message boards upstream of the crash, and takes it down again when the road is clear.
10. The console shows all of it live. The map shows cameras, stations, the incident and each unit moving along the real road. The controller page is where an operator reviews the clip, verifies or rejects, and follows the units to the end.

[docs/how-it-works.md](docs/how-it-works.md) explains each hop and says which parts are real and which are simulated.

## The parts

| Part | Container | Port | What it does | Code |
|---|---|---|---|---|
| Broker | `rabbitmq` | inside only | Receives camera events over MQTT | `rabbitmq/` |
| Core | `core` | 8000 | Incident service, dispatch, notices, forecast, API and the web console | `src/atms_core/` |
| Detector node | `detector` | inside only | Runs the AI models on a video and publishes the alarm | `src/atms_edge/` |
| Agencies | `agencies` | inside only | Pretend police, ambulance, fire, highway patrol and tow systems | `src/atms_sim/agencies.py` |
| SMS gateway | `sms` | inside only | Pretend SMS provider that sends delivery receipts | `src/atms_sim/sms.py` |
| Sensors | `sensors` | none | Runs once and sends a few hours of road readings for the forecast | `src/atms_sim/sensors.py` |
| Kafka and Postgres | `kafka`, `postgres` | 9092, 5433 | Optional, see below | `compose.durable.yaml` |

Only the core publishes a port on your machine, so the other parts cannot clash with anything you already run. To reach them from the command line, add `-f compose.ports.yaml` (broker 1883 and 15672, agencies 9100, SMS 9200, detector 8300).

The browser only talks to the core. The core talks to the detector node, and the detector node talks to the broker, exactly like a camera box in the field would.

## Where it watches

Four areas, 21 cameras and 44 stations, all in `src/atms_contracts/data/network.json`.

| Area | Road | Cameras |
|---|---|---|
| Pune | NH48 at Katraj | 7 |
| Chennai | GST Road, Pallavaram to Perungalathur | 5 |
| Bengaluru | Hosur Road, Silk Board to Electronic City | 5 |
| Delhi NCR (Gurugram) | NH48 Delhi Jaipur highway | 4 |

Positions are approximate and made for the demo. They are not an official list of cameras or stations. Adding a city is a data change, see [docs/dataset.md](docs/dataset.md).

## The videos

`dataset/videos.json` says which videos belong to which camera and whether each is a good scenario or a bad one. It has two sources.

* **Labelled clips** are built from the Kaggle CCTV accident dataset. People labelled those frames as accident or normal, so they are ground truth.
* **Your own recordings** in `videos/` are not labelled by anyone. The AI sorted them and the catalog says so. Edit `dataset/labels.csv` to correct any of them.

Rebuild the catalog after adding videos.

```bash
.venv/bin/python scripts/build_dataset.py
```

## Run it

### With Docker, the normal way

```bash
scripts/start.sh                     # everything for the demo, picks a free port
docker compose logs -f core detector # watch two of them
scripts/start.sh stop                # stop, keeps the downloaded models
```

If the AI models are already on your machine, point the detector at them and skip the download.

```bash
HF_CACHE=$HOME/.cache/huggingface docker compose up --build
```

Other folders for your videos go in `VIDEOS_DIR`, and every published port can be changed, see `.env.example`.

Only the console port is published, and `scripts/start.sh` picks a free one. If you use `docker compose up` yourself and Docker says a port is already allocated, set another, for example `CORE_PORT=8001 docker compose up`.

### With Docker, with Kafka and Postgres

Services then talk over Kafka and incidents survive a restart.

```bash
docker compose -f compose.yaml -f compose.durable.yaml --profile durable up --build
```

### Without Docker

You need Python 3.12 and ffmpeg.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d rabbitmq
.venv/bin/python -m atms_sim.cli agencies --port 9100 --reliable &
.venv/bin/python -m atms_sim.cli sms --port 9200 &
ATMS_SMS_URL=http://localhost:9200 ATMS_DETECTOR_URL=http://localhost:8300 \
  ATMS_FORECAST_MODEL_DIR=models .venv/bin/python -m atms_core.main &
.venv/bin/python -m atms_edge.service &
.venv/bin/python -m atms_sim.cli sensors --minutes 180
```

## If something does not start

| You see | Reason and fix |
|---|---|
| `port is already allocated` | Another program uses the console port. Use `scripts/start.sh`, which picks a free one, or set `CORE_PORT`. |
| The CCTV page says a video file is missing | The `videos/` folder is not on this machine. Your own recordings are not in git. Put them in `videos/` and run `scripts/build_dataset.py`, or ignore them, the labelled clips in `dataset/clips/` still work. |
| Map has no speed colours outside Pune | Expected. Road sensors are simulated for Pune only. |
| No forecast, `no forecast model` in the core log | The model folder `models/` is empty. Train one with `python -m atms_forecast.train --registry models --days 70`. A trained one is in git. |
| Detector stays at `loading` for minutes | It is downloading the AI models. Watch `docker compose logs -f detector`. |

## The three pages

| Page | Use it to |
|---|---|
| **Map** `/console/` | See every camera, station, open incident and unit. Switch between areas. The box at the top left lists alerts sent to officials. |
| **CCTV** `/console/cctv.html` | Pick a camera, watch its videos, run the AI on one and follow what the platform does next. |
| **Controller** `/console/controller.html` | Review an incident with the clip, the AI's reasoning and a comparison of ways to handle the traffic. Verify, reject, mark units and close. |

## Command line

```bash
# check videos with the AI, no alarm
.venv/bin/python -m atms_edge.cli check videos/v4.mp4 --frames 12

# check a folder and report accuracy against Accident and Non Accident folder names
.venv/bin/python -m atms_edge.cli check videos/archive/data/val --limit 12

# check and raise the alarm from a chosen camera, then narrate what the platform does
PYTHONUNBUFFERED=1 .venv/bin/python -m atms_edge.cli check videos/v4.mp4 --cameras CHN003 --publish --watch 30

# ask the running detector node to do the same thing, the way the CCTV page does
curl -X POST localhost:8300/v1/analyze -H 'content-type: application/json' -d '{"video_id": "v4"}'    # start with -f compose.ports.yaml
```

## Settings

| Variable | Used by | Meaning | Default |
|---|---|---|---|
| `ATMS_RABBIT_HOST` | core | Broker host the bridge reads from | `localhost` |
| `ATMS_AGENCIES_URL` | core | Agency systems | `http://localhost:9100` |
| `ATMS_SMS_URL` | core | SMS gateway. Unset means notices are only recorded | unset |
| `ATMS_PUBLIC_URL` | core | Where the SMS gateway sends receipts | `http://localhost:8000` |
| `ATMS_DETECTOR_URL` | core | Detector node for the Analyze button | unset |
| `ATMS_DATASET_DIR` | core, detector | Folder with `videos.json` and the stitched clips | `dataset` |
| `ATMS_VIDEO_DIR` | core, detector | Your own recordings | `videos` |
| `ATMS_MEDIA_DIR` | core, detector | Where evidence clips are written and read | `simulator-out` |
| `ATMS_FORECAST_MODEL_DIR` | core | Forecast model registry. Unset turns the forecast off | unset |
| `ATMS_KAFKA_BOOTSTRAP` | core | Use Kafka between services | unset |
| `ATMS_DATABASE_URL` | core | Keep state in Postgres | unset |
| `ATMS_TOKENS` | core | Tokens as JSON. Unset uses development tokens | development tokens |
| `ATMS_SIGNING_SECRET` | core | Signs video links | `dev-secret` |
| `ATMS_BROKER_HOST` | detector | Broker the alarm is published to | `localhost` |
| `HF_HOME` | detector | Where the AI models are cached | `~/.cache/huggingface` |

The development tokens (`ops-dev`, `sensor-dev` and one per agency) and secrets are for local use only. Set your own before putting this anywhere shared.

## Tests

```bash
.venv/bin/pytest                                   # about 210 tests, no Docker needed
docker compose -f compose.yaml -f compose.ports.yaml --profile durable up -d --wait rabbitmq kafka postgres
ATMS_BROKER_HOST=localhost ATMS_KAFKA=localhost:9092 ATMS_DB_URL=postgresql://atms:atms-dev@localhost:5433/atms .venv/bin/pytest -m integration
```

Some tests skip on purpose. They need Kafka, Postgres or the real AI models. Run the durable stack and `ATMS_RUN_HF=1` to include them.

## Folders

| Folder | What is in it |
|---|---|
| `src/atms_contracts/` | Event models, topic names, the network of areas and cameras, the video catalog |
| `src/atms_core/` | The core platform and the web console in `console/` |
| `src/atms_edge/` | The AI detector, the video checker and the detector node service |
| `src/atms_sim/` | Edge controller, agency and SMS simulators, responders, road sensors |
| `src/atms_forecast/` | Congestion forecast and the what-if traffic simulator |
| `dataset/` | The video catalog and the stitched labelled clips |
| `videos/` | Your recordings. Mounted read only, never changed |
| `docker/`, `compose*.yaml` | Images and the compose files |
| `contracts/` | JSON schemas and the OpenAPI file generated and checked by tests |
| `docs/` | Longer explanations |

## What is real and what is pretend

Real. The AI models and what they say, the MQTT broker, the event pipeline, the incident rules, the dispatch and notice logic, the routes drawn on real roads, the map.

Pretend. The agencies and the SMS gateway are simulators. The cameras are recorded videos, not live streams. The unit positions on the map are estimates from the arrival time, not GPS. Positions of cameras and stations are approximate. Road speeds are only drawn for Pune, and they come from synthetic readings.

The accident model is a pretrained one, and it is not accurate. On the labelled clips it misses many accidents and sometimes calls normal traffic an accident. The CCTV page shows this honestly by comparing each verdict with the label. Do not treat it as a safety system.

## More

* [docs/how-it-works.md](docs/how-it-works.md) each step in detail, what is simulated, how to watch each hop
* [docs/dataset.md](docs/dataset.md) how the catalog is built, labels, adding videos, cameras and cities
* [docs/architecture-flows.md](docs/architecture-flows.md) the original design, state machines and contracts
* [docs/architecture.excalidraw](docs/architecture.excalidraw) the architecture picture, open it at excalidraw.com
