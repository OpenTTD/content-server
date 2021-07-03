import struct

from .protocol.enums import (
    ContentType,
    PacketTCPContentType,
)
from .protocol.write import (
    SEND_MTU,
    write_init,
    write_string,
    write_uint8,
    write_uint32,
    write_presend,
)


class OpenTTDProtocolSend:
    async def send_PACKET_CONTENT_SERVER_INFO(
        self,
        content_type,
        content_id,
        filesize,
        name,
        version,
        url,
        description,
        unique_id,
        md5sum,
        dependencies,
        tags,
        upload_date,
    ):
        data = write_init(PacketTCPContentType.PACKET_CONTENT_SERVER_INFO)

        data = write_uint8(data, content_type.value)
        data = write_uint32(data, content_id)

        data = write_uint32(data, filesize)
        data = write_string(data, name)
        data = write_string(data, version)
        data = write_string(data, url)
        data = write_string(data, description)

        if content_type == ContentType.CONTENT_TYPE_NEWGRF:
            # OpenTTD client sends NewGRFs byte-swapped for some reason.
            # So we swap it back here, as nobody needs to know the
            # protocol is making a boo-boo.
            data = write_uint32(data, struct.unpack(">I", unique_id)[0])
        elif content_type in (ContentType.CONTENT_TYPE_SCENARIO, ContentType.CONTENT_TYPE_HEIGHTMAP):
            # We store Scenarios / Heightmaps byte-swapped (to what OpenTTD expects).
            # This is because otherwise folders are named 01000000, 02000000, which
            # makes sorting a bit odd, and in general just difficult to read.
            data = write_uint32(data, struct.unpack(">I", unique_id)[0])
        else:
            data = write_uint32(data, struct.unpack("<I", unique_id)[0])

        for i in range(16):
            data = write_uint8(data, md5sum[i])

        data = write_uint8(data, len(dependencies))
        for dependency in dependencies:
            data = write_uint32(data, dependency)

        data = write_uint8(data, len(tags))
        for tag in tags:
            data = write_string(data, tag)

        data = write_uint32(data, int(upload_date.timestamp()))

        data = write_presend(data)
        await self.send_packet(data)

    async def send_PACKET_CONTENT_SERVER_CONTENT(self, content_type, content_id, filesize, filename, stream):
        # First, send a packet to tell the client it will be receiving a file
        data = write_init(PacketTCPContentType.PACKET_CONTENT_SERVER_CONTENT)

        data = write_uint8(data, content_type.value)
        data = write_uint32(data, content_id)

        data = write_uint32(data, filesize)
        data = write_string(data, filename)

        data = write_presend(data)
        await self.send_packet(data)

        # Next, send the content of the file over
        while not stream.eof():
            data = write_init(PacketTCPContentType.PACKET_CONTENT_SERVER_CONTENT)
            data += stream.read(SEND_MTU - 3)
            data = write_presend(data)
            await self.send_packet(data)

        data = write_init(PacketTCPContentType.PACKET_CONTENT_SERVER_CONTENT)
        data = write_presend(data)
        await self.send_packet(data)
