import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses
import xlsxwriter

from value_mapper import env
from value_mapper.client import SITES, ArchipelagoClient, Instance
from value_mapper.mapping import COUNT_COLUMNS, KEYS, source_columns, target_columns

BASE = SITES[Instance.local]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """No real credentials leak in: no ARCHIPELAGO_* variables, no .env files read."""
    for name in list(os.environ):
        if name.startswith("ARCHIPELAGO_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(env, "_loaded", True)


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("ARCHIPELAGO_LOCAL_USER", "u")
    monkeypatch.setenv("ARCHIPELAGO_LOCAL_PASSWORD", "p")


def node(uuid, nid, metadata, title="t", published=True):
    return {
        "id": uuid,
        "attributes": {
            "title": title,
            "status": published,
            "drupal_internal__nid": nid,
            "field_descriptive_metadata": {"value": json.dumps(metadata)},
        },
    }


class FakeSite:
    """
    Answers real HTTP calls (through `responses`) the way an Archipelago site
    with tamu_metadata_api does: /api/metadata-values pages, single JSON:API
    nodes, and PATCHes, which are recorded and applied.

    The endpoint answers from `listing`, a snapshot taken at construction, so
    a test can change `nodes` to stand for an edit made after the find.
    `reports_published=False` stands for a site still running the module from
    before rows carried "published".
    """

    def __init__(
        self, mock, nodes, patch_status=None, endpoint_status=200, endpoint_error="nope", reports_published=True
    ):
        self.reports_published = reports_published
        self.nodes = {n["id"]: n for n in nodes}
        self.listing = copy.deepcopy(nodes)
        self.patches = []
        self.endpoint_calls = []
        self.patch_status = patch_status or {}
        self.endpoint_status = endpoint_status
        self.endpoint_error = endpoint_error
        mock.add_callback(responses.GET, f"{BASE}/api/metadata-values", self.metadata_values)
        mock.add_callback(responses.GET, re.compile(rf"{re.escape(BASE)}/jsonapi/node/\w+/[\w-]+"), self.get_node)
        mock.add_callback(responses.PATCH, re.compile(rf"{re.escape(BASE)}/jsonapi/node/\w+/[\w-]+"), self.patch)

    def metadata_values(self, request):
        params = {key: values[0] for key, values in parse_qs(urlparse(request.url).query).items()}
        self.endpoint_calls.append(params)
        if self.endpoint_status != 200:
            return self.endpoint_status, {}, json.dumps({"error": self.endpoint_error})
        keys = params["keys"].split(",")
        after, limit = int(params["after"]), int(params["limit"])
        rows = []
        for n in sorted(self.listing, key=lambda n: n["attributes"]["drupal_internal__nid"]):
            attributes = n["attributes"]
            metadata = json.loads(attributes["field_descriptive_metadata"]["value"])
            held = {key: metadata[key] for key in keys if key in metadata}
            if attributes["drupal_internal__nid"] > after and held:
                row = {
                    "nid": attributes["drupal_internal__nid"],
                    "uuid": n["id"],
                    "bundle": "digital_object",
                    "title": attributes["title"],
                    "metadata": held,
                }
                if self.reports_published:
                    row["published"] = attributes["status"]
                rows.append(row)
        rows = rows[:limit]
        body = {"rows": rows, "next": rows[-1]["nid"] if len(rows) == limit else None}
        return 200, {}, json.dumps(body)

    def get_node(self, request):
        return 200, {}, json.dumps({"data": self.nodes[request.url.rsplit("/", 1)[1]]})

    def patch(self, request):
        uuid = request.url.rsplit("/", 1)[1]
        assert request.headers["Content-Type"] == "application/vnd.api+json"
        assert request.headers["Authorization"].startswith("Basic ")
        body = json.loads(request.body)
        metadata = json.loads(body["data"]["attributes"]["field_descriptive_metadata"]["value"])
        self.patches.append((uuid, metadata))
        status = self.patch_status.get(uuid, 200)
        if status >= 400:
            return status, {}, json.dumps({"errors": [{"detail": "nope"}]})
        attributes = self.nodes[uuid]["attributes"]
        self.nodes[uuid] = node(
            uuid, attributes["drupal_internal__nid"], metadata, attributes["title"], attributes["status"]
        )
        return 200, {}, json.dumps({"data": self.nodes[uuid]})


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def make_site(http, credentials):
    def build(nodes, **options):
        return FakeSite(http, nodes, **options)

    return build


@pytest.fixture
def client(credentials):
    return ArchipelagoClient(Instance.local)


def make_workbook(path: Path, rows_by_key: dict) -> Path:
    """A filled-in workbook: rows are tuples in each sheet's own column order."""
    workbook = xlsxwriter.Workbook(str(path), {"strings_to_numbers": False, "strings_to_formulas": False})
    for key, rows in rows_by_key.items():
        spec = KEYS[key]
        sheet = workbook.add_worksheet(key)
        sheet.write_row(0, 0, source_columns(spec) + target_columns(spec) + COUNT_COLUMNS)
        for number, row in enumerate(rows, start=1):
            for column, value in enumerate(row):
                if value is not None:
                    sheet.write(number, column, value)
    workbook.close()
    return path
