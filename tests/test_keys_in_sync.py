"""The Drupal endpoint and the Python tool must allow exactly the same keys, in the same order."""

import re
from pathlib import Path

from value_mapper.mapping import KEYS

CONTROLLER = (
    Path(__file__).parent.parent
    / "drupal"
    / "tamu_metadata_api"
    / "src"
    / "Controller"
    / "MetadataValuesController.php"
)


def test_php_keys_match_python_keys():
    source = CONTROLLER.read_text(encoding="utf-8")
    block = re.search(r"const KEYS = \[(.*?)\];", source, re.S)
    assert block, "KEYS constant not found in MetadataValuesController.php"
    assert re.findall(r"'(\w+)'", block.group(1)) == list(KEYS)
