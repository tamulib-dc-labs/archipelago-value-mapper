import pytest
from conftest import node

from value_mapper.client import (
    Ado,
    ArchipelagoClient,
    ArchipelagoError,
    Instance,
    MissingCredentials,
)


def test_pages_until_next_is_null(make_site, client):
    site = make_site([node(f"u{i}", i, {"subject": [f"s{i}"]}) for i in range(1, 6)])
    ados = list(client.iter_metadata(["subject", "creator"], page_size=2))
    assert [ado.nid for ado in ados] == [1, 2, 3, 4, 5]
    assert ados[0] == Ado("digital_object", "u1", 1, "t", {"subject": ["s1"]}, True)
    assert [call["after"] for call in site.endpoint_calls] == ["0", "2", "4"]
    assert site.endpoint_calls[0]["keys"] == "subject,creator"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, "tamu_metadata_api module is not installed"),
        (400, "refused the request: nope"),
        (403, "Read Archipelago metadata values"),
        (401, "ARCHIPELAGO_LOCAL_USER and ARCHIPELAGO_LOCAL_PASSWORD"),
    ],
)
def test_endpoint_errors_say_what_to_fix(make_site, client, status, message):
    make_site([], endpoint_status=status)
    with pytest.raises(ArchipelagoError, match=message):
        list(client.iter_metadata(["subject"]))


def test_missing_credentials_named():
    client = ArchipelagoClient(Instance.prod)
    with pytest.raises(MissingCredentials, match="ARCHIPELAGO_PROD_USER and ARCHIPELAGO_PROD_PASSWORD"):
        client.require_auth()


def test_instance_picks_site_and_account(monkeypatch):
    monkeypatch.setenv("ARCHIPELAGO_PROD_USER", "pu")
    monkeypatch.setenv("ARCHIPELAGO_PROD_PASSWORD", "pp")
    monkeypatch.setenv("ARCHIPELAGO_LOCAL_USER", "lu")
    monkeypatch.setenv("ARCHIPELAGO_LOCAL_PASSWORD", "lp")
    prod, local = ArchipelagoClient(Instance.prod), ArchipelagoClient("local")
    assert prod.base == "https://digitalcollections.library.tamu.edu"
    assert prod.session.auth == ("pu", "pp")
    assert local.base == "http://localhost:8001"
    assert local.session.auth == ("lu", "lp")


def test_parse_node_decodes_the_json_string():
    ado = ArchipelagoClient.parse_node("digital_object", node("a", 7, {"subject": ["Texas"]}, title="T"))
    assert ado == Ado("digital_object", "a", 7, "T", {"subject": ["Texas"]}, True)


def test_published_flag_is_read(make_site, client):
    make_site([node("a", 1, {"subject": ["x"]}, published=False)])
    assert [ado.published for ado in client.iter_metadata(["subject"])] == [False]
    raw = node("a", 1, {"subject": ["x"]}, published=False)
    assert ArchipelagoClient.parse_node("digital_object", raw).published is False


def test_old_module_leaves_published_unknown(make_site, client):
    make_site([node("a", 1, {"subject": ["x"]})], reports_published=False)
    assert [ado.published for ado in client.iter_metadata(["subject"])] == [None]


def test_parse_node_refuses_non_object():
    broken = node("a", 7, {})
    broken["attributes"]["field_descriptive_metadata"]["value"] = "[1, 2]"
    with pytest.raises(ValueError, match="not a JSON object"):
        ArchipelagoClient.parse_node("digital_object", broken)
