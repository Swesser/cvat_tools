# cvat_tools

Load an annotation task into a running CVAT, give its job to an annotator, and take the
annotations back in the schema the task came in. Needs nothing but the server address and the
administrator credentials.

## Created

2026-08-25

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

```bash
export CVAT_URL=https://<address>
export CVAT_ADMIN_LOGIN=admin
export CVAT_ADMIN_PASSWORD=<password>

.venv/bin/python scripts/cvat_ops.py import --source <task directory>
.venv/bin/python scripts/cvat_ops.py assign --project <task directory name> --annotator User1
.venv/bin/python scripts/cvat_ops.py export-figures --project <name> --output results/figures.json
```

`import` creates a project named after the directory, one task holding every image and one job,
and uploads the detections from `figures.json` as annotations. `assign` gives every job of the
project to that user at stage `annotation`. `export-figures` writes the annotations of the
project's jobs as one file in the source schema.

## The task directory

```
<task>/img/            the images
<task>/meta.json       the label definitions: a BBOX and a KGROUP entry per class
<task>/figures.json    per image name: bboxes and kgroups in absolute pixels
```

A class becomes two CVAT labels, because CVAT requires top-level label names to be unique within a
project: a `rectangle` under the source name and a `skeleton` named `<name>_skeleton`, whose
sublabels, colours and edges come from `keypoint_info` and `keypoint_connections`. A keypoint
missing from a group is uploaded as an element marked `outside`; `export-figures` drops it again.

## The trash tag

Besides the classes of `meta.json`, every project gets an image-level tag named `trash`, so an
annotator can mark a frame that should not be used. `export-figures` writes the mark as a
`"trash"` flag on the frame:

```json
{
    "img_name.jpg": {
        "trash": false,
        "bboxes": [],
        "kgroups": [],
        "height": 1080,
        "width": 1920
    }
}
```

Every frame of the job is written, tagged or not: the flag says what the annotator decided, and
the consumer of the file decides what to do with it. The tag is matched by name rather than by
id, so a project that was created before the tag existed, and numbers its labels differently,
still reads correctly.

The flag only ever travels outwards. A task arrives from a detector, which has no notion of a
frame being unusable, so the `figures.json` of a source task carries no `"trash"` key and
`import` has none to read: the mark is made by an annotator in CVAT and leaves through
`export-figures`.

The server certificate is not verified: the package carries no CA file.
# cvat_tools
