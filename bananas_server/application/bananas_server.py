import asyncio
import logging

from collections import defaultdict
from concurrent import futures

from ..helpers.safe_filename import safe_filename
from ..openttd.protocol.enums import ContentType
from ..openttd.protocol.exceptions import SocketClosed
from ..storage.exceptions import StreamReadError

log = logging.getLogger(__name__)


class Application:
    def __init__(self, storage, index, bootstrap_unique_id):
        super().__init__()

        self.storage = storage
        self.index = index

        if bootstrap_unique_id:
            self._bootstrap_unique_id = bytes.fromhex(bootstrap_unique_id)
        else:
            self._bootstrap_unique_id = None

        self._by_content_id = None
        self._by_content_type = None
        self._by_unique_id = None
        self._by_unique_id_and_md5sum = None

        self._reload_busy = asyncio.Event()
        self._reload_busy.set()

        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.reload())

    async def _send_content_entry(self, source, content_entry):
        await source.protocol.send_PACKET_CONTENT_SERVER_INFO(
            content_type=content_entry.content_type,
            content_id=content_entry.content_id,
            filesize=content_entry.filesize,
            name=content_entry.name,
            version=content_entry.version,
            url=content_entry.url,
            description=content_entry.description,
            unique_id=content_entry.unique_id,
            md5sum=content_entry.md5sum,
            dependencies=content_entry.dependencies,
            tags=content_entry.tags,
            upload_date=content_entry.upload_date,
        )

    def get_by_content_id(self, content_id):
        return self._by_content_id.get(content_id)

    def get_by_unique_id(self, content_type, unique_id):
        return self._by_unique_id[content_type].get(unique_id)

    def get_by_unique_id_and_md5sum(self, content_type, unique_id, md5sum):
        return self._by_unique_id_and_md5sum[content_type].get(unique_id, {}).get(md5sum)

    async def receive_PACKET_CONTENT_CLIENT_INFO_LIST(self, source, content_type, openttd_version):
        version_major = (openttd_version >> 28) & 0xF
        version_minor = (openttd_version >> 24) & 0xF
        version_patch = (openttd_version >> 20) & 0xF
        version = [version_major, version_minor, version_patch]

        bootstrap_content_entry = None

        # Make sure the first entry we sent is the bootstrap base graphics,
        # as this is the one the OpenTTD client will use in the bootstrap.
        if content_type == ContentType.CONTENT_TYPE_BASE_GRAPHICS and self._bootstrap_unique_id:
            bootstrap_content_entry = self.get_by_unique_id(
                ContentType.CONTENT_TYPE_BASE_GRAPHICS, self._bootstrap_unique_id
            )

            if not bootstrap_content_entry:
                log.error(f"Bootstrap package with unique-id {self._bootstrap_unique_id} not found")
            else:
                await self._send_content_entry(source, bootstrap_content_entry)

        for content_entry in self._by_content_type.get(content_type, []):
            if content_entry == bootstrap_content_entry:
                continue

            if content_entry.min_version and version < content_entry.min_version:
                continue
            if content_entry.max_version and version >= content_entry.max_version:
                continue

            await self._send_content_entry(source, content_entry)

    async def receive_PACKET_CONTENT_CLIENT_INFO_EXTID(self, source, content_infos):
        for content_info in content_infos:
            content_entry = self.get_by_unique_id(content_info.content_type, content_info.unique_id)
            if content_entry:
                await self._send_content_entry(source, content_entry)

    async def receive_PACKET_CONTENT_CLIENT_INFO_EXTID_MD5(self, source, content_infos):
        for content_info in content_infos:
            content_entry = self.get_by_unique_id_and_md5sum(
                content_info.content_type, content_info.unique_id, content_info.md5sum
            )
            if content_entry:
                await self._send_content_entry(source, content_entry)

    async def receive_PACKET_CONTENT_CLIENT_INFO_ID(self, source, content_infos):
        for content_info in content_infos:
            content_entry = self.get_by_content_id(content_info.content_id)
            if content_entry:
                await self._send_content_entry(source, content_entry)

    async def receive_PACKET_CONTENT_CLIENT_CONTENT(self, source, content_infos):
        for content_info in content_infos:
            content_entry = self.get_by_content_id(content_info.content_id)
            if not content_entry:
                continue

            try:
                with self.storage.get_stream(content_entry) as stream:
                    await source.protocol.send_PACKET_CONTENT_SERVER_CONTENT(
                        content_type=content_entry.content_type,
                        content_id=content_entry.content_id,
                        filesize=content_entry.filesize,
                        filename=safe_filename(content_entry),
                        stream=stream,
                    )
            except StreamReadError:
                # Reading from the backend failed; we don't have many options
                # except to abort the connection and hope the user retries.
                raise SocketClosed
            except SocketClosed:
                # The user terminated it's connection; our caller knows how to
                # handle this signal.
                raise
            except Exception:
                log.exception("Error with storage, aborting for this client ...")
                raise SocketClosed

    async def reload(self):
        await self._reload_busy.wait()
        self._reload_busy.clear()

        try:
            reload_helper = ReloadHelper(self.storage, self.index)
            reload_helper.prepare()

            # Run the reload in a new process, so we don't block the rest of the
            # server while doing this job.
            loop = asyncio.get_event_loop()
            with futures.ProcessPoolExecutor(max_workers=1) as executor:
                task = loop.run_in_executor(executor, reload_helper.reload)
                (
                    self._by_content_id,
                    self._by_content_type,
                    self._by_unique_id,
                    self._by_unique_id_and_md5sum,
                ) = await task
        finally:
            self._reload_busy.set()


class ReloadHelper:
    def __init__(self, storage, index):
        self.storage = storage
        self.index = index

    def _get_md5sum_mapping(self):
        log.info("Building md5sum mapping")
        md5sum_mapping = defaultdict(lambda: defaultdict(dict))

        for content_type in ContentType:
            if content_type == ContentType.CONTENT_TYPE_END:
                continue

            for unique_id_str in self.storage.list_folder(content_type):
                unique_id = bytes.fromhex(unique_id_str)

                for filename in self.storage.list_folder(content_type, unique_id_str):
                    md5sum, _, _ = filename.partition(".")

                    md5sum_partial = bytes.fromhex(md5sum[0:8])
                    md5sum = bytes.fromhex(md5sum)

                    md5sum_mapping[content_type][unique_id][md5sum_partial] = md5sum

        # defaultdict() cannot be pickled, so convert to a normal dict.
        return {key: dict(value) for key, value in md5sum_mapping.items()}

    def prepare(self):
        self.storage.clear_cache()

    def reload(self):
        md5sum_mapping = self._get_md5sum_mapping()
        return self.index.reload(md5sum_mapping)
