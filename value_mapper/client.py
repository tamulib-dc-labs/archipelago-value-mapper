"""
Reading and writing Archipelago Digital Objects (ADOs) with one Drupal account.

Two kinds of request:

* bulk reads go to ``/api/metadata-values``, served by the tamu_metadata_api
  module in ``drupal/``: one database query per page of up to 5000 ADOs,
  returning only the keys asked for. JSON:API would take a request per 50
  ADOs, each loading and serializing every node in full.
* single-ADO reads and all writes go through JSON:API.

Archipelago keeps an ADO's descriptive metadata in one Strawberry Field,
``field_descriptive_metadata``, whose ``value`` is the whole metadata document
serialized as a JSON *string*. Reads ``json.loads`` that string and writes send
it back with ``json.dumps``.

Writes go through Drupal (``PATCH /jsonapi/node/{bundle}/{uuid}``) rather than
the database, so the node revision, Strawberry Field processing and the Solr
reindex happen exactly as for an edit made in the UI. Only
``field_descriptive_metadata`` is sent; JSON:API leaves every attribute it is
not given alone.

The site needs a few things this client cannot fix:

* the tamu_metadata_api module enabled, with its "Read Archipelago metadata
  values" permission on the account's role
* JSON:API set to accept writes (``drush config:get jsonapi.settings
  read_only`` is ``false``); otherwise every PATCH is a 405
* the ``basic_auth`` module enabled
* for updates, an account allowed to edit Digital Objects
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import requests

from .env import setting

JSONAPI_TYPE = "application/vnd.api+json"

METADATA_VALUES_PATH = "/api/metadata-values"

# The endpoint's maximum.
METADATA_PAGE_SIZE = 5000


class Instance(StrEnum):
    local = "local"
    prod = "prod"


SITES = {
    Instance.local: "http://localhost:8001",
    Instance.prod: "https://digitalcollections.library.tamu.edu",
}


class ArchipelagoError(RuntimeError):
    """A request to Archipelago failed in a way the operator has to fix."""


class MissingCredentials(ArchipelagoError):
    """No Drupal account configured for the chosen instance."""


@dataclass
class Ado:
    bundle: str
    uuid: str
    nid: int | None
    title: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    # The node's published flag; None when the site did not say.
    published: bool | None = None


def credential_variables_for(instance: Instance) -> tuple[str, str]:
    """Names of the environment variables holding the account for an instance."""
    prefix = f"ARCHIPELAGO_{Instance(instance).name.upper()}"
    return f"{prefix}_USER", f"{prefix}_PASSWORD"


class ArchipelagoClient:
    """
    `instance` picks the site. The account comes from ARCHIPELAGO_PROD_USER /
    ARCHIPELAGO_PROD_PASSWORD (or the _LOCAL_ pair), read from the shell first
    and from a .env.local / .env in the working directory otherwise.
    """

    def __init__(self, instance: Instance = Instance.local, timeout: int = 60, session: requests.Session | None = None):
        self.instance = Instance(instance)
        self.base = SITES[self.instance]
        self.credential_variables = credential_variables_for(self.instance)
        self.user, self.password = (setting(name) for name in self.credential_variables)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": JSONAPI_TYPE})
        if self.user and self.password:
            self.session.auth = (self.user, self.password)

    def require_auth(self) -> None:
        """Fail with the real reason before any request goes out."""
        if not (self.user and self.password):
            user_variable, password_variable = self.credential_variables
            raise MissingCredentials(
                f"{user_variable} and {password_variable} must both be set.\n"
                f'    PowerShell:  $env:{user_variable} = "your-username"; '
                f'$env:{password_variable} = "your-password"\n'
                f'    bash:        export {user_variable}="your-username" '
                f'{password_variable}="your-password"\n'
                f"or put them in .env.local (see .env.example)."
            )

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        if response.status_code in (401, 403):
            raise ArchipelagoError(
                f"{response.status_code} reading {url}; check "
                f"{' and '.join(self.credential_variables)}, that basic_auth is enabled, and that "
                f"the account's role has 'Read Archipelago metadata values'"
            )
        if response.status_code == 404 and url.endswith(METADATA_VALUES_PATH):
            raise ArchipelagoError(
                f"{self.base} has no {METADATA_VALUES_PATH}; the tamu_metadata_api module "
                f"is not installed or not enabled there"
            )
        if response.status_code == 400:
            try:
                detail = response.json().get("error")
            except ValueError:
                detail = None
            raise ArchipelagoError(f"{url} refused the request: {detail or response.text[:200]}")
        response.raise_for_status()
        return response.json()

    def iter_metadata(self, keys: Iterable[str], page_size: int = METADATA_PAGE_SIZE) -> Iterator[Ado]:
        """
        Every ADO holding any of `keys`, from the tamu_metadata_api endpoint.

        `metadata` holds only the requested keys the ADO has, and `published`
        is None if the site runs a version of the module from before it
        reported it. Pages follow `next` until the endpoint returns null.
        """
        url = f"{self.base}{METADATA_VALUES_PATH}"
        keys = ",".join(keys)
        after = 0
        while after is not None:
            document = self._get(
                url,
                params={"keys": keys, "after": after, "limit": page_size},
                headers={"Accept": "application/json"},
            )
            for row in document.get("rows") or []:
                yield Ado(
                    row["bundle"],
                    row["uuid"],
                    row["nid"],
                    row.get("title"),
                    row.get("metadata") or {},
                    row.get("published"),
                )
            after = document.get("next")

    @staticmethod
    def parse_node(bundle: str, node: dict) -> Ado:
        """
        One JSON:API node resource as an Ado with its full metadata.

        The field normally serializes as {"value": "<json string>"}; a bare
        dict is accepted too in case a normalizer has already decoded it.
        Raises ValueError when the stored JSON is not an object.
        """
        attributes = node.get("attributes") or {}
        stored = attributes.get("field_descriptive_metadata") or {}
        raw = stored.get("value") if isinstance(stored, dict) and "value" in stored else stored
        if isinstance(raw, str):
            metadata = json.loads(raw) if raw.strip() else {}
        elif isinstance(raw, dict):
            metadata = raw
        else:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("field_descriptive_metadata is not a JSON object")
        return Ado(
            bundle,
            node.get("id"),
            attributes.get("drupal_internal__nid"),
            attributes.get("title"),
            metadata,
            attributes.get("status"),
        )

    def get_ado(self, bundle: str, uuid: str) -> Ado:
        """Read one ADO fresh, with its whole metadata document."""
        document = self._get(f"{self.base}/jsonapi/node/{bundle}/{uuid}")
        return self.parse_node(bundle, document.get("data") or {})

    def patch_metadata(self, bundle: str, uuid: str, metadata: dict) -> requests.Response:
        """Replace an ADO's descriptive metadata. Returns the response."""
        body = {
            "data": {
                "type": f"node--{bundle}",
                "id": uuid,
                "attributes": {
                    "field_descriptive_metadata": {"value": json.dumps(metadata, ensure_ascii=False)},
                },
            },
        }
        return self.session.patch(
            f"{self.base}/jsonapi/node/{bundle}/{uuid}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": JSONAPI_TYPE},
            timeout=self.timeout,
        )
