import click
import logging
import os
import yaml

from collections import defaultdict
from openttd_helpers import click_helper

from .schema import ContentEntry as ContentEntryTest
from ..helpers.content_type import get_content_type_from_name
from ..helpers.content_type import get_folder_name_from_content_type
from ..openttd.protocol.enums import ContentType

log = logging.getLogger(__name__)

_folder = None


class ContentEntry:
    def __init__(
        self,
        content_id,
        content_type,
        filesize,
        name,
        version,
        url,
        description,
        unique_id,
        upload_date,
        md5sum,
        dependencies,
        min_version,
        max_version,
        tags,
    ):
        super().__init__()

        self.content_id = content_id
        self.content_type = content_type
        self.filesize = filesize
        self.name = name
        self.version = version
        self.url = url
        self.description = description
        self.unique_id = unique_id
        self.upload_date = upload_date
        self.md5sum = md5sum
        self.raw_dependencies = dependencies
        self.dependencies = None
        self.min_version = min_version
        self.max_version = max_version
        self.tags = tags

    def calculate_dependencies(self, by_unique_id_and_md5sum):
        dependencies = []

        for dependency in self.raw_dependencies:
            (content_type, unique_id, md5sum) = dependency
            dep_content_entry = by_unique_id_and_md5sum[content_type].get(unique_id, {}).get(md5sum)
            if dep_content_entry is None:
                log.error("Invalid dependency: %r", dependency)
                continue

            dependencies.append(dep_content_entry.content_id)

        self.dependencies = dependencies

    def __repr__(self):
        return (
            f"ContentEntry(content_id={self.content_id!r}, "
            f"content_type={self.content_type!r}, "
            f"filesize={self.filesize!r}, "
            f"name={self.name!r}, "
            f"version={self.version!r}, "
            f"url={self.url!r}, "
            f"description={self.description!r}, "
            f"unique_id={self.unique_id!r}, "
            f"upload_date={self.upload_date!r}, "
            f"md5sum={self.md5sum!r}, "
            f"dependencies={self.dependencies!r}, "
            f"min_version={self.min_version!r}, "
            f"max_version={self.max_version!r}, "
            f"tags={self.tags!r})"
        )


class Index:
    def __init__(self):
        self._folder = _folder

    def _read_content_entry_version(self, content_type, unique_id, data, md5sum_mapping):
        unique_id = bytes.fromhex(unique_id)

        md5sum_partial = bytes.fromhex(data["md5sum-partial"])
        md5sum = md5sum_mapping[content_type][unique_id][md5sum_partial]

        upload_date = int(data["upload-date"].timestamp())

        dependencies = []
        for dependency in data.get("dependencies", []):
            dep_content_type = get_content_type_from_name(dependency["content-type"])
            dep_unique_id = bytes.fromhex(dependency["unique-id"])

            dep_md5sum_partial = bytes.fromhex(dependency["md5sum-partial"])
            dep_md5sum = md5sum_mapping[dep_content_type][dep_unique_id][dep_md5sum_partial]

            dependencies.append((dep_content_type, dep_unique_id, dep_md5sum))

        min_version = None
        max_version = None
        for com in data.get("compatibility", {}):
            if com["name"] != "official":
                continue

            for conditions in com["conditions"]:
                if conditions.startswith(">="):
                    min_version = [int(p) for p in conditions[2:].split(".")]
                elif conditions.startswith("<"):
                    max_version = [int(p) for p in conditions[1:].split(".")]
                else:
                    raise Exception("Invalid compatibility flag", com)

        # Validate the object to make sure all fields are within set limits.
        ContentEntryTest().load(
            {
                "content-type": content_type,
                "content-id": 0,
                "filesize": data["filesize"],
                "name": data["name"],
                "version": data["version"],
                "url": data.get("url", ""),
                "description": data.get("description", ""),
                "unique-id": unique_id,
                "upload-date": upload_date,
                "md5sum": md5sum,
                "min-version": min_version,
                "max-version": max_version,
                "tags": data.get("tags", []),
                "raw-dependencies": dependencies,
            }
        )

        # Calculate if this entry wouldn't exceed the OpenTTD packet size if
        # we would transmit this over the wire.
        size = 1 + 4 + 4  # content-type, content-id, filesize
        size += len(data["name"]) + 2
        size += len(data["version"]) + 2
        size += len(data.get("url", "")) + 2
        size += len(data.get("description", "")) + 2
        size += len(unique_id) + 2
        size += len(md5sum) + 2
        size += len(dependencies) * 4
        size += 1
        for tag in data.get("tags", []):
            size += len(tag) + 2
        size += 4  # upload_date

        if size > 1400:
            raise Exception("Entry would exceed OpenTTD packet size.")

        content_entry = ContentEntry(
            content_type=content_type,
            content_id=0,
            filesize=data["filesize"],
            name=data["name"],
            version=data["version"],
            url=data.get("url", ""),
            description=data.get("description", ""),
            unique_id=unique_id,
            upload_date=data["upload-date"],
            md5sum=md5sum,
            dependencies=dependencies,
            min_version=min_version,
            max_version=max_version,
            tags=data.get("tags", []),
        )

        # Calculate the content-id we want to give him, but don't assign it
        # just yet. When everything is read, we will check if this id is
        # unique over the whole set.
        # We take 24bits from the right side of the md5sum; the left side
        # is already given to the user as an md5sum-partial, and not a
        # secret. We only take 24bits to allow room for a counter.
        content_entry.pre_content_id = int.from_bytes(md5sum[-3:], "little")

        return content_entry

    def _read_content_entry(self, content_type, folder_name, unique_id, md5sum_mapping):
        folder_name = f"{folder_name}/{unique_id}"

        with open(f"{folder_name}/global.yaml") as f:
            global_data = yaml.safe_load(f.read())

        # If this entry is blacklisted, we won't be finding anything useful
        if global_data.get("blacklisted"):
            return [], []

        content_entries = []
        archived_content_entries = []
        for version in os.listdir(f"{folder_name}/versions"):
            with open(f"{folder_name}/versions/{version}") as f:
                version_data = yaml.safe_load(f.read())

                # Extend the version data with global data with fields not set
                for key, value in global_data.items():
                    if key not in version_data:
                        version_data[key] = value

                try:
                    content_entry = self._read_content_entry_version(
                        content_type, unique_id, version_data, md5sum_mapping
                    )
                except Exception:
                    log.exception(f"Failed to load entry {folder_name}/versions/{version}. Skipping.")
                    continue

                if version_data["availability"] == "new-games":
                    content_entries.append(content_entry)
                else:
                    archived_content_entries.append(content_entry)

        return content_entries, archived_content_entries

    def reload(self, md5sum_mapping):
        by_content_id = {}
        by_content_type = defaultdict(list)
        by_unique_id = defaultdict(dict)
        by_unique_id_and_md5sum = defaultdict(lambda: defaultdict(dict))

        content_ids = defaultdict(list)

        for content_type in ContentType:
            if content_type == ContentType.CONTENT_TYPE_END:
                continue

            counter_entries = 0
            counter_archived = 0

            content_type_folder_name = get_folder_name_from_content_type(content_type)
            folder_name = f"{self._folder}/{content_type_folder_name}"

            if not os.path.isdir(folder_name):
                continue

            for unique_id in os.listdir(folder_name):
                content_entries, archived_content_entries = self._read_content_entry(
                    content_type, folder_name, unique_id, md5sum_mapping
                )

                for content_entry in content_entries:
                    counter_entries += 1
                    by_unique_id_and_md5sum[content_type][content_entry.unique_id][content_entry.md5sum] = content_entry

                    content_ids[content_entry.pre_content_id].append(content_entry)
                    del content_entry.pre_content_id

                    by_content_type[content_type].append(content_entry)
                    by_unique_id[content_type][content_entry.unique_id] = content_entry

                for content_entry in archived_content_entries:
                    counter_archived += 1
                    by_unique_id_and_md5sum[content_type][content_entry.unique_id][content_entry.md5sum] = content_entry

                    content_ids[content_entry.pre_content_id].append(content_entry)
                    del content_entry.pre_content_id

            log.info(
                "Loaded %d entries and %d archived for %s", counter_entries, counter_archived, content_type_folder_name
            )

        # There is a small chance the content_id, based on the md5sum, is not
        # unique. This is why we simply add a number to indicate it is the Nth
        # time we have seen this part of the md5sum, sorted by upload-date.
        # This means that content_ids are stable over multiple runs, and means
        # we can scale this server horizontally.
        for content_id, content_entries in content_ids.items():
            if len(content_entries) > 255:
                raise Exception(
                    "We have more than 255 hash collisions;"
                    "content-ids would be identical for more than one package. Aborting."
                )

            for i, content_entry in enumerate(sorted(content_entries, key=lambda x: x.upload_date)):
                content_entry.content_id = (i << 24) + content_id
                by_content_id[content_entry.content_id] = content_entry

        # Now everything is known, calculate the dependencies.
        for content_entry in by_content_id.values():
            content_entry.calculate_dependencies(by_unique_id_and_md5sum)

        # defaultdict() cannot be pickled, so convert to a normal dict.
        return (
            by_content_id,
            dict(by_content_type),
            dict(by_unique_id),
            {key: dict(value) for key, value in by_unique_id_and_md5sum.items()},
        )


@click_helper.extend
@click.option(
    "--index-local-folder",
    help="Folder to use for index storage. (index=local only)",
    type=click.Path(dir_okay=True, file_okay=False),
    default="BaNaNaS",
    show_default=True,
)
def click_index_local(index_local_folder):
    global _folder

    _folder = index_local_folder
