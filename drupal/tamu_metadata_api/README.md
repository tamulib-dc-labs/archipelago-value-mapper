# tamu_metadata_api

The Drupal side of [archipelago-value-mapper](../../README.md): a read-only endpoint that lists the name, subject and genre values of every Archipelago Digital Object, so `value-mapper` can read a whole repository in a few requests with an ordinary Drupal account.

## The endpoint

```
GET /api/metadata-values?keys=subject,creator&after=0&limit=5000
```

| Parameter | Meaning |
|---|---|
| `keys` | Comma-separated metadata keys. Required. Allowed: `creator`, `contributor`, `agent`, `subject`, `geographic_subject`, `temporal_subject`, `genre_form`, and the `_linked_data` form of each. |
| `after` | Return Digital Objects with a node id greater than this. Start at `0`; pass `next` from the previous page. |
| `limit` | Rows per page, 1–5000. Default 5000. |

```json
{
  "rows": [
    {"nid": 12345, "uuid": "3f2a…", "bundle": "digital_object", "title": "Map of College Station",
     "metadata": {"subject": ["Texas"], "creator": ["Smith, John"]}}
  ],
  "next": 12345
}
```

`next` is `null` on the last page. Only Digital Objects and Collections holding at least one of the keys are listed, unpublished ones included, and `metadata` holds only the keys each one has. A bad parameter returns `400` with an `error` message.

Each page is one read-only query on the Strawberry Field table, run through Drupal's own database connection. Nothing is written, and no SQL is taken from the request: keys come from a fixed list and every value is a query placeholder.

## Access

The route needs the **Read Archipelago metadata values** permission and accepts a Drupal username and password (HTTP basic auth, so `basic_auth` must be enabled) or a logged-in browser session.

Installing it on a site is covered in the repository README, under "Installing the Drupal module".

## Keys

`MetadataValuesController::KEYS` must match `KEYS` in `value_mapper/mapping.py`. `tests/test_keys_in_sync.py` fails if they drift.
