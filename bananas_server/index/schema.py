from marshmallow import (
    fields,
    Schema,
    validate,
)
from marshmallow_enum import EnumField

from ..helpers.content_type import ContentType


class ContentEntry(Schema):
    class Meta:
        ordered = True

    content_id = fields.Integer(data_key="content-id")
    unique_id = fields.Raw(data_key="unique-id", validate=validate.Length(min=4, max=4))
    content_type = EnumField(ContentType, data_key="content-type", by_value=True)
    filesize = fields.Integer()
    # Most of these limits are limitations in the OpenTTD client.
    name = fields.String(validate=validate.Length(max=31))
    version = fields.String(validate=validate.Length(max=15))
    url = fields.String(validate=validate.Length(max=95))
    description = fields.String(validate=validate.Length(max=511))
    tags = fields.List(fields.String(validate=validate.Length(max=31)))
    md5sum = fields.Raw(validate=validate.Length(min=16, max=16))
    upload_date = fields.Integer(data_key="upload-date")
    min_version = fields.List(fields.Integer(), data_key="min-version", missing=None)
    max_version = fields.List(fields.Integer(), data_key="max-version", missing=None)
    raw_dependencies = fields.List(
        fields.Tuple(
            (
                EnumField(ContentType, by_value=True),
                fields.Raw(validate=validate.Length(min=4, max=4)),
                fields.Raw(validate=validate.Length(min=16, max=16)),
            )
        ),
        data_key="raw-dependencies",
    )
