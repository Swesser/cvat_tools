#!/usr/bin/env python3
"""Load a task into CVAT, assign its job, and take the annotations back.

Needs only CVAT_URL, CVAT_ADMIN_LOGIN and CVAT_ADMIN_PASSWORD; a project is addressed by
its name, so nothing is kept between commands.
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import urllib3
from cvat_sdk import Client, Config
from cvat_sdk.api_client import models
from cvat_sdk.core.proxies.tasks import ResourceType

from figures import TRASH_LABEL, figures_from_shapes, read_task


def build_client(args: argparse.Namespace) -> Client:
    # The server certificate is self-signed and the package carries no CA file for it.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    client = Client(url=args.url, config=Config(verify_ssl=False), check_server_version=False)
    client.login((args.login, args.password))
    return client


def paginate(list_method, **kwargs) -> list:
    items = []
    page = 1
    while True:
        data, _ = list_method(page=page, page_size=100, **kwargs)
        items.extend(data.results)
        if not data.next:
            return items
        page += 1


def find_project(client: Client, name: str):
    matches = [
        project
        for project in paginate(client.api_client.projects_api.list, name=name)
        if project.name == name
    ]
    if not matches:
        sys.exit(f"no project named {name!r}")
    if len(matches) > 1:
        sys.exit(f"{len(matches)} projects are named {name!r}: {[p.id for p in matches]}")
    return matches[0]


def project_jobs(client: Client, project_id: int) -> list:
    return paginate(client.api_client.jobs_api.list, project_id=project_id)


def user_id(client: Client, username: str) -> int:
    for user in paginate(client.api_client.users_api.list):
        if user.username == username:
            return user.id
    sys.exit(f"user {username!r} does not exist on the server")


def label_ids(task) -> tuple[dict, dict]:
    """Map label name -> id and (label name, sublabel name) -> id for one task."""
    labels = {}
    sublabels = {}
    for label in task.get_labels():
        labels[label.name] = label.id
        for sublabel in getattr(label, "sublabels", None) or []:
            sublabels[(label.name, sublabel.name)] = sublabel.id
    return labels, sublabels


def label_names(client: Client, job_id: int) -> tuple[dict, dict]:
    """Map label id -> name, and sublabel id -> (name, position in the skeleton definition)."""
    names = {}
    sublabels = {}
    for label in paginate(client.api_client.labels_api.list, job_id=job_id):
        names[label.id] = label.name
        for order, sublabel in enumerate(getattr(label, "sublabels", None) or []):
            sublabels[sublabel.id] = (sublabel.name, order)
    return names, sublabels


def shape_requests(annotations: dict, frames: dict, labels: dict, sublabels: dict) -> list:
    shapes = []
    for image_name, figure in annotations.items():
        frame = frames.get(image_name)
        if frame is None:
            sys.exit(f"{image_name} was not found among the task frames")

        for shape in figure["shapes"]:
            label_id = labels.get(shape["label"])
            if label_id is None:
                sys.exit(f"label {shape['label']!r} was not created on the task")

            if shape["type"] == "rectangle":
                shapes.append(
                    models.LabeledShapeRequest(
                        type=models.ShapeType("rectangle"),
                        label_id=label_id,
                        frame=frame,
                        points=[float(value) for value in shape["points"]],
                        occluded=False,
                        outside=False,
                        z_order=0,
                        rotation=0.0,
                        attributes=[],
                    )
                )
            elif shape["type"] == "skeleton":
                elements = []
                for element in shape["elements"]:
                    sublabel_id = sublabels.get((shape["label"], element["label"]))
                    if sublabel_id is None:
                        sys.exit(
                            f"sublabel {element['label']!r} of {shape['label']!r} was not created"
                        )
                    elements.append(
                        models.SubLabeledShapeRequest(
                            type=models.ShapeType("points"),
                            label_id=sublabel_id,
                            frame=frame,
                            points=[float(value) for value in element["points"]],
                            occluded=False,
                            outside=bool(element["outside"]),
                            z_order=0,
                            rotation=0.0,
                            attributes=[],
                        )
                    )
                shapes.append(
                    models.LabeledShapeRequest(
                        type=models.ShapeType("skeleton"),
                        label_id=label_id,
                        frame=frame,
                        points=[],
                        occluded=False,
                        outside=False,
                        z_order=0,
                        rotation=0.0,
                        attributes=[],
                        elements=elements,
                    )
                )
            else:
                sys.exit(f"unsupported shape type {shape['type']!r} for {image_name}")
    return shapes


def job_figures(client: Client, job, counts: Counter) -> dict:
    meta = client.api_client.jobs_api.retrieve_data_meta(job.id)[0]
    annotations = client.api_client.jobs_api.retrieve_annotations(job.id)[0]
    names, sublabels = label_names(client, job.id)

    # A task built with a frame filter or with deleted frames numbers its frames sparsely,
    # so take the numbers the server reports rather than counting from start_frame.
    included = list(meta.included_frames or [])
    numbers = included or list(range(meta.start_frame, meta.start_frame + len(meta.frames)))

    frames = {
        number: {"name": Path(frame.name).name, "width": frame.width, "height": frame.height}
        for number, frame in zip(numbers, meta.frames)
    }

    # Matched by name rather than by id: a project created before the tag existed carries it
    # under an id of its own, and every project numbers its labels separately.
    trash = {tag.frame for tag in annotations.tags if names.get(tag.label_id) == TRASH_LABEL}

    counts["tracks"] += len(annotations.tracks)
    counts["tags"] += len(annotations.tags) - len(trash)
    counts["trash frames"] += len(trash)
    return figures_from_shapes(frames, annotations.shapes, names, sublabels, counts, trash)


def cmd_import(client: Client, args: argparse.Namespace) -> None:
    name = args.source.resolve().name
    if paginate(client.api_client.projects_api.list, name=name):
        sys.exit(f"a project named {name!r} already exists on the server")

    labels, annotations, images, counts = read_task(args.source)
    print(
        f"{name}: {len(images)} images, {counts['boxes']} boxes, {counts['skeletons']} skeletons "
        f"({counts['incomplete']} incomplete, {counts['dropped']} empty groups dropped)"
    )

    project = client.projects.create({"name": name, "labels": labels})
    print(f"project {project.id} {project.name!r} with {len(labels)} labels")

    task = client.tasks.create_from_data(
        spec={"name": name, "project_id": project.id, "segment_size": len(images)},
        resources=images,
        resource_type=ResourceType.LOCAL,
        data_params={"image_quality": 90},
    )
    print(f"task {task.id}: {len(images)} images uploaded")

    frames = {Path(frame.name).name: index for index, frame in enumerate(task.get_frames_info())}
    labels_by_name, sublabels_by_name = label_ids(task)
    shapes = shape_requests(annotations, frames, labels_by_name, sublabels_by_name)

    task.set_annotations(models.LabeledDataRequest(shapes=shapes))
    print(f"task {task.id}: {len(shapes)} shapes uploaded into {len(task.get_jobs())} job(s)")


def cmd_assign(client: Client, args: argparse.Namespace) -> None:
    project = find_project(client, args.project)
    annotator_id = user_id(client, args.annotator)

    for job in project_jobs(client, project.id):
        client.api_client.jobs_api.partial_update(
            job.id,
            patched_job_write_request=models.PatchedJobWriteRequest(
                assignee=annotator_id, stage=models.JobStage("annotation")
            ),
        )
        print(f"job {job.id}: assignee={args.annotator} stage=annotation")


def cmd_export_figures(client: Client, args: argparse.Namespace) -> None:
    project = find_project(client, args.project)

    figures = {}
    counts = Counter()
    for job in sorted(project_jobs(client, project.id), key=lambda item: item.id):
        figures.update(job_figures(client, job, counts))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(figures, indent=4))

    extras = ", ".join(
        f"{value} {key}" for key, value in sorted(counts.items())
        if value and key not in ("bboxes", "kgroups")
    )
    print(
        f"{project.name}: {len(figures)} images, {counts['bboxes']} bboxes, "
        f"{counts['kgroups']} kgroups -> {args.output}" + (f" ({extras})" if extras else "")
    )


COMMANDS = {
    "import": cmd_import,
    "assign": cmd_assign,
    "export-figures": cmd_export_figures,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("CVAT_URL"))
    parser.add_argument("--login", default=os.environ.get("CVAT_ADMIN_LOGIN"))
    parser.add_argument("--password", default=os.environ.get("CVAT_ADMIN_PASSWORD"))

    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser("import", help="create a project, a task and a job from a task directory")
    importer.add_argument("--source", type=Path, required=True)

    assign = subparsers.add_parser("assign", help="give every job of a project to an annotator")
    assign.add_argument("--project", required=True)
    assign.add_argument("--annotator", required=True)

    export = subparsers.add_parser("export-figures", help="write the annotations as figures.json")
    export.add_argument("--project", required=True)
    export.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if not args.url or not args.login or not args.password:
        parser.error("CVAT_URL, CVAT_ADMIN_LOGIN and CVAT_ADMIN_PASSWORD must be set")
    return args


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    with build_client(args) as client:
        COMMANDS[args.command](client, args)


if __name__ == "__main__":
    main()
