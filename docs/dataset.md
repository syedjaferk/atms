# The video dataset

The CCTV page needs to know which videos belong to which camera and whether each shows a good or a bad scenario. That is the video catalog, `dataset/videos.json`. This page explains where it comes from and how to change it.

## What is in it

Each entry says

| Field | Meaning |
|---|---|
| `id` | Short name, for example `v4` or `acc-03` |
| `camera_id` | The camera this video belongs to |
| `scenario` | `good` for normal traffic, `bad` for an accident |
| `truth` | `accident`, `normal`, or `unlabelled` when nobody has checked it by eye |
| `label_source` | `ground truth` when people labelled it, `model` when the AI sorted it |
| `root`, `file` | Where the file is, `dataset` or `videos` folder, and its path inside |
| `model` | What the AI said when the catalog was built |

Every camera gets both good and bad videos, so you can compare what the AI does on each.

## Where the videos come from

**Labelled clips.** The Kaggle CCTV accident dataset ships as single frames in folders called `Accident` and `Non Accident`. The build script groups frames that belong to the same clip, picks the longest sequences, and stitches each into a short mp4 in `dataset/clips/`, one frame per second. People labelled those frames, so the clips are ground truth. They look like slide shows, not smooth video, because the source has frames seconds apart.

**Your recordings.** Every mp4 in `videos/` is checked by the AI (frame sampling, the same models the detector node uses) and the answer is saved in `dataset/model_labels.json`. The build script uses it to decide good or bad. It cannot know better than the model, so these entries say `unlabelled` and `model`.

Recordings that have not been checked yet are left out of the catalog until they are.

## Build it

```bash
# 1. check your recordings, smallest first. You can stop any time, the file is written after each video.
.venv/bin/python -m atms_edge.cli check videos --only videos --frames 8 --json dataset/model_labels.json

# 2. write the catalog and the stitched clips
.venv/bin/python scripts/build_dataset.py
```

Step 1 takes about a minute per video on a CPU, longer for long videos. The script prints how many good and bad videos each camera got.

## Correct a label

If you watch a recording and the AI sorted it wrongly, say so in `dataset/labels.csv`.

```csv
id,truth
v4,accident
v7,normal
```

Run the build script again. Those entries become ground truth and move to the right scenario.

## Add a video

Put the mp4 in `videos/`, check it with step 1 above, build again. To put it on a chosen camera instead of the automatic one, edit `camera_id` in `dataset/videos.json` after the build, or leave the build alone and add a line to a copy of the script. The build deals videos out to cameras in turn, so re-running it can move videos between cameras.

## Add a camera, a station or a city

Everything about places is in `src/atms_contracts/data/network.json`.

* To add a camera, add an object to `cameras` with an `id`, its `area`, a `name`, `lat`, `lon` and `heading` in degrees.
* To add a station, add an object to `facilities` with an `agency` of `police`, `ambulance`, `highway_patrol`, `fire` or `tow`.
* To add a city, add an object to `areas` with the `state`, `city`, `road` and a `road_name`, then add its cameras and stations.

Keep at least one station of each agency within 15 km of each camera. A test checks this. Run `.venv/bin/pytest tests/test_network.py` after any change, then rebuild the catalog so the new camera gets videos.

The map, the CCTV page, dispatch and notices all read this one file, so no code changes are needed.

## Limits

* Positions are approximate and made for the demo. They are not an official list.
* The two kinds of source look different. Stitched clips are slide shows, recordings are smooth video. A person can tell them apart, the AI may too.
* The AI sorted your recordings, so a bad one may be good and the other way round. The CCTV page tells you which label is a guess.
* The Kaggle dataset has its own licence. Check it before sharing the stitched clips.
