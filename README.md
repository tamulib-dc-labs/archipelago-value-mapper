# archipelago-value-mapper

Clean up name, subject and genre values across every Archipelago Digital Object (ADO) through a spreadsheet:

1. **`value-mapper get`** writes a workbook with one sheet per metadata key and one row per distinct value, with empty *target* columns.
2. You fill in targets for the values that need fixing.
3. **`value-mapper update`** rewrites every ADO holding a source value to its target.

It works with an ordinary Drupal account. Reads go through a small read-only endpoint, the Drupal module in [`drupal/tamu_metadata_api`](drupal/tamu_metadata_api/README.md); writes go through JSON:API, so Drupal saves a revision, reindexes search and clears caches as it would for an edit in the UI.

## Installing

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/tamulib-dc-labs/archipelago-value-mapper.git
cd archipelago-value-mapper
uv sync
uv run value-mapper --help
```

Or install the command for your user, so `value-mapper` works from any folder:

```
uv tool install .
```

## Logging in

`--instance`/`-i` picks the site: `prod` (`https://digitalcollections.library.tamu.edu`) or `local` (`http://localhost:8001`, the default). Each needs a Drupal account:

```powershell
$env:ARCHIPELAGO_PROD_USER = "your-username"; $env:ARCHIPELAGO_PROD_PASSWORD = "your-password"
```

```bash
export ARCHIPELAGO_PROD_USER="your-username" ARCHIPELAGO_PROD_PASSWORD="your-password"
```

`ARCHIPELAGO_LOCAL_USER` / `ARCHIPELAGO_LOCAL_PASSWORD` for `local`. Or copy `.env.example` to `.env.local` in the folder you run from and fill it in; it is gitignored, and anything set in the shell wins over it.

The account's role needs **Read Archipelago metadata values** to read, and edit access to Digital Objects to update.

## `value-mapper get`

```
value-mapper get -i prod -o mapping.xlsx
value-mapper get -i prod -o subjects.xlsx -k subject -k subject_linked_data
```

Reads every `digital_object` and `digital_object_collection` node, unpublished ones included, and writes the workbook. The keys are `creator`, `contributor`, `agent`, `subject`, `geographic_subject`, `temporal_subject`, `genre_form`, and the `_linked_data` form of each; `--key`/`-k` (repeatable) limits it.

| Key shape | Columns |
|---|---|
| plain strings (`subject`, ...) | `source`, `target` |
| `{value, uri}` (`subject_linked_data`, ...) | `source_value`, `source_uri`, `target_value`, `target_uri` |
| `agent` | `source_value`, `source_role`, `target_value`, `target_role` |
| `agent_linked_data` | value, uri and role, source and target |

Every sheet also has `ado_count`, the number of ADOs holding the value, and `example_nid`, one node that has it. Rows are sorted case-insensitively, so spelling variants sit next to each other. A value is matched exactly after trimming whitespace and is case-sensitive, so `Texas` and `texas` are separate rows. Values are always stored as text: one that looks like a formula, a number or a link stays exactly as it is.

## `value-mapper update`

```
value-mapper update -x mapping.xlsx -i prod --dry-run
value-mapper update -x mapping.xlsx -i prod
```

- **A row with every target cell blank is ignored.**
- **Only the filled target cells change.** Fill just `target_uri` to fix a URI and keep the label.
- For `agent` and `agent_linked_data` the role is part of the match, so a name that appears with two roles is two rows.
- If two sources map to the same target on one ADO, the duplicate is dropped.
- A key stored as a single value rather than a list is written back the same way. Other keys on an entry are kept.
- The workbook is checked before anything is read from Archipelago. The run stops on an unknown sheet, a missing column, a target with no source, or the same source given two different targets.

It runs in two steps:

1. **Find** (the metadata endpoint, a few requests): read only the mapped keys for every ADO and keep the ones where a filled row matches. Matching uses the same code as the write, so the find and the write always agree. `--dry-run` stops here.
2. **Write** (JSON:API, one ADO at a time): for each ADO found, read it fresh, apply the mapping, and send back only `field_descriptive_metadata`. An ADO already fixed since the find is skipped. Expect roughly 1–3 seconds per ADO; the time depends on how many ADOs change, not on how many there are.

A report goes to `archipelago_mapping_report.csv` (`-o` to change it) with one line per changed key per ADO: old value, new value, and status (`dry run`, `ok`, or the error). The command exits with status 1 if any change failed.

Take a database backup before a large update on prod. Every write makes a revision, but a backup is the quick way back from a bad bulk mapping.

## Installing the Drupal module

Once per site, by someone with shell access to the `esmero-php` container, as the user that owns the site files (`www-data`).

1. Check the prerequisites. `read_only` must be `false`, and `basic_auth` and `jsonapi` must be listed:

   ```
   cd /var/www/html
   vendor/bin/drush config:get jsonapi.settings read_only
   vendor/bin/drush pm:list --status=enabled --format=list | grep -E '^(basic_auth|jsonapi)$'
   ```

2. Clone this repository **outside the web root**, checking out only `drupal/`, and link the module into place:

   ```
   cd /var/www/html
   git clone --filter=blob:none --sparse https://github.com/tamulib-dc-labs/archipelago-value-mapper.git
   git -C archipelago-value-mapper sparse-checkout set drupal
   mkdir -p web/modules/custom
   ln -s ../../../archipelago-value-mapper/drupal/tamu_metadata_api web/modules/custom/tamu_metadata_api
   ```

3. Enable it, grant the permission to the roles that edit Digital Objects, and rebuild caches:

   ```
   vendor/bin/drush en tamu_metadata_api -y
   for role in metadata_librarian metadata_api metadata_pro; do vendor/bin/drush role:perm:add $role 'read archipelago metadata values'; done
   vendor/bin/drush cr
   ```

4. Check it from your own machine: `curl -u <username> "https://<site>/api/metadata-values?keys=subject&limit=2"` should return JSON with `rows`. A `404` means the module is not enabled or the web server blocks `/api/`; a `403` means the account's role lacks the permission.

To update the module later: `git -C /var/www/html/archipelago-value-mapper pull`, then `vendor/bin/drush cr`.

If the site's configuration is ever re-imported from a sync folder that does not include this module, the import switches it off. Export the configuration after enabling it, or re-enable it after an import.

## Development

```
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```
