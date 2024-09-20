# -*- coding: utf-8 -*-
#
# Copyright (C) 2022-2024 CERN.
#
# Invenio-Vocabularies is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see LICENSE file for more
# details.

"""Awards datastreams, transformers, writers and readers."""

import io

import requests
from invenio_access.permissions import system_identity
from invenio_i18n import lazy_gettext as _

from invenio_vocabularies.datastreams.errors import ReaderError

from ...datastreams.errors import TransformerError
from ...datastreams.readers import BaseReader
from ...datastreams.transformers import BaseTransformer
from ...datastreams.writers import ServiceWriter
from .config import awards_ec_ror_id, awards_openaire_funders_mapping


class AwardsServiceWriter(ServiceWriter):
    """Funders service writer."""

    def __init__(self, *args, **kwargs):
        """Constructor."""
        service_or_name = kwargs.pop("service_or_name", "awards")
        super().__init__(service_or_name=service_or_name, *args, **kwargs)

    def _entry_id(self, entry):
        """Get the id from an entry."""
        return entry["id"]


class OpenAIREProjectTransformer(BaseTransformer):
    """Transforms an OpenAIRE project record into an award record."""

    def apply(self, stream_entry, **kwargs):
        """Applies the transformation to the stream entry."""
        record = stream_entry.entry
        award = {}

        code = record["code"]

        # The `id` should follow the format `sourcePrefix::md5(localId)` where `sourcePrefix` is 12 characters long.
        # See: https://graph.openaire.eu/docs/data-model/pids-and-identifiers#identifiers-in-the-graph
        #
        # The format of `id` in the full OpenAIRE Graph Dataset (https://doi.org/10.5281/zenodo.3516917)
        # follows this format (e.g. 'abc_________::0123456789abcdef0123456789abcdef').
        # However, the format of `id` in the new collected projects dataset (https://doi.org/10.5281/zenodo.6419021)
        # does not follow this format, and has a `40|` prefix (e.g. '40|abc_________::0123456789abcdef0123456789abcdef').
        #
        # The number '40' corresponds to the entity types 'Project'.
        # See: https://ec.europa.eu/research/participants/documents/downloadPublic?documentIds=080166e5a3a1a213&appId=PPGMS
        # See: https://graph.openaire.eu/docs/5.0.0/data-model/entities/project#id
        openaire_funder_prefix = record["id"].split("::", 1)[0].split("|", 1)[-1]

        funder_id = awards_openaire_funders_mapping.get(openaire_funder_prefix)
        if funder_id is None:
            raise TransformerError(
                _(
                    "Unknown OpenAIRE funder prefix {openaire_funder_prefix}".format(
                        openaire_funder_prefix=openaire_funder_prefix
                    )
                )
            )

        award["id"] = f"{funder_id}::{code}"

        funding = next(iter(record.get("funding", [])), None)
        if funding:
            program = funding.get("fundingStream", {}).get("id", "")
            if program:
                award["program"] = program

        identifiers = []
        if funder_id == awards_ec_ror_id:
            identifiers.append(
                {
                    "identifier": f"https://cordis.europa.eu/projects/{code}",
                    "scheme": "url",
                }
            )
        elif record.get("websiteurl"):
            identifiers.append(
                {"identifier": record.get("websiteurl"), "scheme": "url"}
            )

        if identifiers:
            award["identifiers"] = identifiers

        award["number"] = code

        # `title` is a mandatory attribute of the `Project` object in the OpenAIRE Graph Data Model.
        # See: https://graph.openaire.eu/docs/data-model/entities/project#title
        # However, 15'000+ awards for the FCT funder (and 1 record the NIH funder) are missing a title attribute.
        if "title" not in record:
            raise TransformerError(
                _(
                    "Missing title attribute for award {award_id}".format(
                        award_id=award["id"]
                    )
                )
            )
        award["title"] = {"en": record["title"]}

        award["funder"] = {"id": funder_id}
        acronym = record.get("acronym")
        if acronym:
            award["acronym"] = acronym

        stream_entry.entry = award
        return stream_entry


class CORDISProjectHTTPReader(BaseReader):
    """CORDIS Project HTTP Reader returning an in-memory binary stream of the latest CORDIS Horizon Europe project zip file."""

    def _iter(self, fp, *args, **kwargs):
        raise NotImplementedError(
            "CORDISProjectHTTPReader downloads one file and therefore does not iterate through items"
        )

    def read(self, item=None, *args, **kwargs):
        """Reads the latest CORDIS Horizon Europe project zip file and yields an in-memory binary stream of it."""
        if item:
            raise NotImplementedError(
                "CORDISProjectHTTPReader does not support being chained after another reader"
            )

        if self._origin == "HE":
            file_url = "https://cordis.europa.eu/data/cordis-HORIZONprojects-xml.zip"
        elif self._origin == "H2020":
            file_url = "https://cordis.europa.eu/data/cordis-h2020projects-xml.zip"
        elif self._origin == "FP7":
            file_url = "https://cordis.europa.eu/data/cordis-fp7projects-xml.zip"
        else:
            raise ReaderError(
                "The --origin option should be either 'HE' (for Horizon Europe) or 'H2020' (for Horizon 2020) or 'FP7'"
            )

        # Download the ZIP file and fully load the response bytes content in memory.
        # The bytes content are then wrapped by a BytesIO to be file-like object (as required by `zipfile.ZipFile`).
        # Using directly `file_resp.raw` is not possible since `zipfile.ZipFile` requires the file-like object to be seekable.
        file_resp = requests.get(file_url)
        file_resp.raise_for_status()
        yield io.BytesIO(file_resp.content)


class CORDISProjectTransformer(BaseTransformer):
    """Transforms a CORDIS project record into an award record."""

    def apply(self, stream_entry, **kwargs):
        """Applies the transformation to the stream entry."""
        record = stream_entry.entry
        award = {}

        # Here `id` is the project ID, which will be used to attach the update to the existing project.
        award["id"] = f"00k4n6c32::{record['id']}"

        categories = record["relations"]["categories"]["category"]
        if isinstance(categories, dict):
            categories = [categories]

        award["subjects"] = [
            {"id": f"euroscivoc:{category['code'].split('/')[-1]}"}
            for category in categories
            if category.get("@classification") == "euroSciVoc"
        ]

        organizations = record["relations"]["associations"]["organization"]
        # Projects with a single organization are not wrapped in a list,
        # so we do this here to be able to iterate over it.
        organizations = (
            organizations if isinstance(organizations, list) else [organizations]
        )
        award["organizations"] = []
        for organization in organizations:
            # Some organizations in FP7 projects do not have a "legalname" key,
            # for instance the 14th participant in "SAGE" https://cordis.europa.eu/project/id/999902.
            # In this case, fully skip the organization entry.
            if "legalname" not in organization:
                continue

            organization_data = {
                # TODO: Here the legal name is uppercase.
                "organization": organization["legalname"],
            }

            # Some organizations in FP7 projects do not have an "id" key (the PIC identifier),
            # for instance "AIlGreenVehicles" in "MOTORBRAIN" https://cordis.europa.eu/project/id/270693.
            # In this case, still store the name but skip the identifier part.
            if "id" in organization:
                organization_data.update(
                    {
                        "scheme": "pic",
                        "id": organization["id"],
                    }
                )

            award["organizations"].append(organization_data)

        stream_entry.entry = award
        return stream_entry


class CORDISAwardsServiceWriter(ServiceWriter):
    """CORDIS Awards service writer."""

    def __init__(self, *args, **kwargs):
        """Constructor."""
        service_or_name = kwargs.pop("service_or_name", "awards")
        # Here we only update and we do not insert, since CORDIS data is used to augment existing awards
        # (with subjects and organizations information) and is not used to create new awards.
        super().__init__(service_or_name=service_or_name, insert=False, *args, **kwargs)

    def _entry_id(self, entry):
        """Get the id from an entry."""
        return entry["id"]


VOCABULARIES_DATASTREAM_READERS = {
    "cordis-project-http": CORDISProjectHTTPReader,
}

VOCABULARIES_DATASTREAM_TRANSFORMERS = {
    "openaire-award": OpenAIREProjectTransformer,
    "cordis-award": CORDISProjectTransformer,
}
"""ORCiD Data Streams transformers."""

VOCABULARIES_DATASTREAM_WRITERS = {
    "awards-service": AwardsServiceWriter,
    "cordis-awards-service": CORDISAwardsServiceWriter,
}
"""ORCiD Data Streams transformers."""

DATASTREAM_CONFIG_CORDIS = {
    "readers": [
        {"type": "cordis-project-http"},
        {
            "type": "zip",
            "args": {
                "regex": "\\.xml$",
                "mode": "r",
            },
        },
        {
            "type": "xml",
            "args": {
                "root_element": "project",
            },
        },
    ],
    "transformers": [
        {"type": "cordis-award"},
    ],
    "writers": [
        {
            "type": "cordis-awards-service",
            "args": {
                "identity": system_identity,
            },
        }
    ],
}
"""Data Stream configuration.

An origin is required for the reader.
"""

DATASTREAM_CONFIG = {
    "readers": [
        {
            "type": "tar",
            "args": {
                "regex": "\\.json.gz$",
                "mode": "r",
            },
        },
        {"type": "gzip"},
        {"type": "jsonl"},
    ],
    "transformers": [
        {"type": "openaire-award"},
    ],
    "writers": [
        {
            "type": "awards-service",
            "args": {
                "identity": system_identity,
            },
        }
    ],
}
"""Data Stream configuration.

An origin is required for the reader.
"""
