# How it works

This page follows one accident from a video to a unit arriving on the map. It says what each part does, what it sends to the next part, and how to look at it yourself. For the design behind it, see [architecture-flows.md](architecture-flows.md).

## The picture in one line

```
catalog video -> detector node -> RabbitMQ -> core -> agencies and SMS gateway
                                               |
                                               +-> web console (map, CCTV, controller)
```

Each arrow is a separate network call between separate containers. Nothing is shared in memory.

## Step by step

### 1. A video belongs to a camera

`dataset/videos.json` lists every video, the camera it belongs to and whether it is a good or a bad scenario. A camera is a point on a road with a name and a heading, listed in `src/atms_contracts/data/network.json`.

Look at it with `curl -H 'Authorization: Bearer ops-dev' localhost:<port>/v1/cctv/cameras/CHN003/videos`.

### 2. The detector node checks the video

You press the button on the CCTV page, or call `POST /v1/analyze` on the node. The node

1. takes between 8 and 24 frames spread over the video,
2. runs the accident model on each frame and records a score from 0 to 1,
3. counts a frame as a hit when its score is 0.5 or more,
4. needs at least two hit frames before it believes there is an accident.

If it believes it, the second model counts the vehicles near the accident box on the best frame. Severity is a simple rule on top of that. Five vehicles or a very large box is critical, two or more is major, otherwise minor. The affected lanes come from where the box sits across the frame. These two rules are guesses, they are not something the models predict.

Confidence is the average score of the hit frames.

The page shows one bar per frame so you can see why the AI decided what it did. Look at the node with `docker compose logs -f detector`.

### 3. The alarm goes to the broker

For a confirmed accident the node cuts a 12 second clip that starts four seconds before the worst frame. It then publishes two events over MQTT, exactly like a camera box would.

* `accident.detected` carries the camera site, its position and heading, confidence, severity, vehicle count and lanes.
* `video.ready` carries the clip reference and its checksum.

The topic is `atms/{state}/{city}/{road}/{camera}/event/...`, so a Chennai camera publishes under `atms/TN/CHENNAI/NH32/CHN003/`. See [../contracts/topics.md](../contracts/topics.md).

Before sending, the node writes the event to a small local database. If the broker is down it keeps retrying, so a detection is not lost.

Watch the broker at `http://localhost:15672` with user `atms` and password `atms-dev`. Start with `-f compose.ports.yaml` so that port is published.

### 4. The core turns events into an incident

The core reads from the broker, checks each event against its schema, and passes it to the correlator.

* A detection within 300 metres and 10 minutes of an open incident, heading the same way, is added to that incident. Two cameras seeing the same crash make one incident.
* Anything else opens a new incident.
* A detection below 60 percent is logged and no incident opens.

The clip is attached to the incident as evidence.

### 5. Verification

| Confidence | Severity | What happens |
|---|---|---|
| below 60 percent | any | logged only |
| 60 to 90 percent | any | waits for an operator |
| 90 percent or more | minor | waits for an operator |
| 90 percent or more | major or critical | verified automatically |

An operator verifies or rejects on the controller page, or with the button on the CCTV page. The controller shows the clip, the confidence against these lines, and what the AI saw.

### 6. Dispatch

Verifying an incident starts the dispatch step. It needs these agencies.

| Severity | Agencies asked |
|---|---|
| minor | police, tow |
| major | police, ambulance, highway patrol, tow |
| critical | police, ambulance, highway patrol, fire, tow |

For each one it ranks that agency's stations by distance to the crash and asks the nearest. The request carries the station, so the answer is about that station. The agency replies with a unit and an arrival time, or declines, or does not answer. A decline or no answer moves on to the next station. If nobody answers, the supervisor gets an SMS to phone the agency.

In this project the agency systems are simulated. The simulator works out the arrival time from the real distance plus up to two minutes for a crew to get moving. With `--reliable`, which compose uses, it never declines.

### 7. Notices

Notices go out when the incident is verified, rejected, resolved or closed.

| Who | How | When |
|---|---|---|
| Duty officer of the area | SMS | major or worse |
| The two nearest stations of each dispatched agency, plus the stations actually asked | signed webhook | every notice kind |
| Control room supervisor | SMS | when an agency cannot be reached |

A crash in Chennai notifies only Chennai stations and the Chennai duty officer. The test `test_a_crash_in_chennai_is_dispatched_to_and_notified_through_chennai_only` checks this.

Each notice is saved before it is sent. SMS goes to a first provider and falls back to a second. Webhooks are signed with a shared secret so an agency can tell they are real. A delivery receipt comes back and updates the notice. Failed notices are retried with growing delays and can be retried by hand from the controller.

### 8. The road

Traffic management shows a warning on the message boards upstream of the crash and takes it down when the incident is resolved. Message boards and road speeds exist for Pune only, because that is where the demo has sensor data. The other areas get a plain line drawn along their cameras.

### 9. The map

The browser asks the core for one payload, `GET /v1/map`, and refreshes it when the live stream says something changed. The payload holds

* the areas and their cameras, with a cone showing where each looks,
* every station, with a ring when it has been notified,
* open incidents, their units and the notices sent,
* for each unit, when it set off and the arrival time it gave.

The page draws each unit on the real road. It asks a public routing service (OSRM) for the road path from the station to the crash, and moves the dot along that path by the share of the arrival time that has passed. If the routing service cannot be reached it draws a straight line.

## What is real and what is pretend

| Part | Real or pretend | Notes |
|---|---|---|
| Accident and vehicle models | Real | Pretrained Hugging Face models, run on CPU |
| Video | Recorded | Not a live stream. A camera box would read a stream |
| Frame sets from Kaggle | Real, labelled | Stitched into short clips, one frame a second |
| Your recordings | Real, not labelled | The AI sorted them |
| MQTT, RabbitMQ, event schema, outbox | Real | |
| Incident, dispatch and notice rules | Real | |
| Agencies | Simulated | Real adapters are not built |
| SMS gateway | Simulated | Real provider adapters are not built |
| Unit positions | Estimated | From arrival time, not GPS |
| Camera and station positions | Approximate | Made up for the demo |
| Road speeds | Synthetic | Pune only |

## Watching each hop

| Hop | How to see it |
|---|---|
| Detector node | `docker compose logs -f detector` |
| Broker | `http://localhost:15672` (start with `-f compose.ports.yaml`) |
| Core | `docker compose logs -f core` and `http://localhost:8000/console/` |
| What the agencies were told | `docker compose exec core python -c "import urllib.request as u; print(u.urlopen('http://agencies:9100/v1/agencies/notices').read().decode())"` |
| Incident and units | `curl -H 'Authorization: Bearer ops-dev' localhost:<port>/v1/map` |
| Notices of one incident | `curl -H 'Authorization: Bearer ops-dev' localhost:<port>/v1/incidents/<id>/notifications` |

## When something looks wrong

| You see | Likely reason |
|---|---|
| CCTV page says the detector node is offline | The container is still starting or the models are still downloading. Check `docker compose logs detector`. |
| The AI says no accident for a bad video | The model is not accurate. The label may also be a guess, see [dataset.md](dataset.md). |
| Incident stays `detected` | The AI was below 90 percent or the crash was minor. Verify it as an operator. |
| SMS notice stays pending or fails | `ATMS_SMS_URL` is not set, or the SMS gateway is down. |
| Dots do not move | The unit has not accepted yet, or no arrival time was given. |
| Map shows no streets | The OpenStreetMap tiles could not be loaded. Check your connection. |
