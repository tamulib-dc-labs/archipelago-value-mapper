import pytest
from conftest import make_workbook
from python_calamine import CalamineWorkbook

from value_mapper.client import Ado
from value_mapper.mapping import (
    KEYS,
    Change,
    MappingWorkbookError,
    apply_mapping,
    clean,
    collect_values,
    read_mapping,
    write_workbook,
)


class TestApplyMapping:
    def test_plain_value_replaced_and_list_kept(self):
        metadata = {"subject": ["Texas", "Maps"], "label": "x"}
        new, changes = apply_mapping(metadata, {"subject": {("Texas",): {"value": "Texas--Maps"}}})
        assert new["subject"] == ["Texas--Maps", "Maps"]
        assert new["label"] == "x"
        assert metadata["subject"] == ["Texas", "Maps"], "input must not be modified"
        assert changes == [Change("subject", "Texas | Maps", "Texas--Maps | Maps")]

    def test_single_string_stays_single(self):
        new, _ = apply_mapping({"genre_form": "Letters"}, {"genre_form": {("Letters",): {"value": "Correspondence"}}})
        assert new["genre_form"] == "Correspondence"

    def test_uri_only_target_keeps_label_and_extra_keys(self):
        metadata = {"subject_linked_data": [{"value": "Austin (Tex.)", "uri": "", "source": "lcnaf"}]}
        rules = {
            "subject_linked_data": {("Austin (Tex.)", ""): {"uri": "http://id.loc.gov/authorities/names/n79005632"}}
        }
        new, _ = apply_mapping(metadata, rules)
        assert new["subject_linked_data"] == [
            {"value": "Austin (Tex.)", "uri": "http://id.loc.gov/authorities/names/n79005632", "source": "lcnaf"},
        ]

    def test_role_is_part_of_the_match(self):
        metadata = {"agent": [{"value": "Smith, J.", "role": "author"}, {"value": "Smith, J.", "role": "editor"}]}
        new, _ = apply_mapping(metadata, {"agent": {("Smith, J.", "author"): {"value": "Smith, John"}}})
        assert new["agent"] == [{"value": "Smith, John", "role": "author"}, {"value": "Smith, J.", "role": "editor"}]

    def test_two_sources_to_one_target_dedupes(self):
        new, _ = apply_mapping(
            {"creator": ["Smith, J.", "Smith, John", "Jones"]}, {"creator": {("Smith, J.",): {"value": "Smith, John"}}}
        )
        assert new["creator"] == ["Smith, John", "Jones"]

    def test_match_trims_but_is_case_sensitive(self):
        rules = {"subject": {("Texas",): {"value": "TX"}}}
        assert apply_mapping({"subject": ["  Texas "]}, rules)[0]["subject"] == ["TX"]
        assert apply_mapping({"subject": ["texas"]}, rules)[1] == []

    def test_no_match_no_change(self):
        new, changes = apply_mapping({"subject": ["Maps"]}, {"subject": {("Texas",): {"value": "TX"}}})
        assert changes == []
        assert new == {"subject": ["Maps"]}


def test_collect_counts_ados_not_occurrences():
    ados = [
        Ado("digital_object", "a", 1, "t", {"subject": ["Texas", "Texas"], "agent": {"value": "X", "role": "r"}}),
        Ado("digital_object", "b", 2, "t", {"subject": "Texas", "creator": []}),
    ]
    counts, total = collect_values(ados)
    assert total == 2
    assert counts["subject"][("Texas",)] == [2, 1]
    assert counts["agent"][("X", "r")] == [1, 1]
    assert counts["creator"] == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("  a\x07b ", "ab"),
        (1920.0, "1920"),
        (2.5, "2.5"),
        (7, "7"),
    ],
)
def test_clean(value, expected):
    assert clean(value) == expected


class TestWorkbook:
    def test_layout_and_values_stay_text(self, tmp_path):
        path = tmp_path / "m.xlsx"
        counts = {key: {} for key in KEYS}
        counts["subject"] = {("=SUM(1)",): [1, 5], ("1920",): [2, 6], ("apple",): [3, 9]}
        counts["agent_linked_data"] = {("A", "http://x.test/a", "author"): [1, 2]}
        write_workbook(path, counts, KEYS, info={"source": "x", "ados with these keys": 3})

        workbook = CalamineWorkbook.from_path(str(path))
        try:
            assert workbook.sheet_names == [*KEYS, "_info"]
            rows = workbook.get_sheet_by_name("subject").to_python()
            assert rows[0] == ["source", "target", "ado_count", "example_nid"]
            # Sorted case-insensitively; the formula and the number stay text.
            assert [row[0] for row in rows[1:]] == ["1920", "=SUM(1)", "apple"]
            assert [row[2] for row in rows[1:]] == [2, 1, 3]
            linked = workbook.get_sheet_by_name("agent_linked_data").to_python()
            assert linked[0] == [
                "source_value",
                "source_uri",
                "source_role",
                "target_value",
                "target_uri",
                "target_role",
                "ado_count",
                "example_nid",
            ]
            assert linked[1][:3] == ["A", "http://x.test/a", "author"]
        finally:
            workbook.close()

    def test_round_trip_through_read_mapping(self, tmp_path):
        path = tmp_path / "m.xlsx"
        counts = {key: {} for key in KEYS}
        counts["subject"] = {("Texas",): [1, 1]}
        write_workbook(path, counts)
        with pytest.raises(MappingWorkbookError, match="nothing to do"):
            read_mapping(path)


class TestReadMapping:
    def test_keeps_only_filled_targets(self, tmp_path):
        path = make_workbook(
            tmp_path / "m.xlsx",
            {
                "subject": [("Texas", "Texas--Maps", 3, 1), ("Maps", None, 1, 2), ("Same", "Same", 1, 3)],
                "subject_linked_data": [("Austin", None, None, "http://u", 1, 4)],
            },
        )
        assert read_mapping(path) == {
            "subject": {("Texas",): {"value": "Texas--Maps"}},
            "subject_linked_data": {("Austin", ""): {"uri": "http://u"}},
        }

    def test_number_typed_as_target_reads_as_text(self, tmp_path):
        path = make_workbook(tmp_path / "m.xlsx", {"temporal_subject": [("Nineteen twenties", 1920, 1, 1)]})
        assert read_mapping(path) == {"temporal_subject": {("Nineteen twenties",): {"value": "1920"}}}

    def test_conflicting_targets_refused(self, tmp_path):
        path = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "A", 1, 1), ("Texas", "B", 1, 1)]})
        with pytest.raises(MappingWorkbookError, match="rows 2 and 3"):
            read_mapping(path)

    def test_target_without_source_refused(self, tmp_path):
        path = make_workbook(tmp_path / "m.xlsx", {"subject": [(None, "A", 1, 1)]})
        with pytest.raises(MappingWorkbookError, match="no source"):
            read_mapping(path)

    def test_unknown_sheet_refused(self, tmp_path):
        path = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
        import xlsxwriter

        workbook = xlsxwriter.Workbook(str(tmp_path / "bad.xlsx"))
        workbook.add_worksheet("subjects").write_row(0, 0, ["source", "target"])
        workbook.close()
        with pytest.raises(MappingWorkbookError, match="'subjects' is not a known key"):
            read_mapping(tmp_path / "bad.xlsx")
        assert read_mapping(path)  # the good one still reads

    def test_missing_column_refused(self, tmp_path):
        import xlsxwriter

        workbook = xlsxwriter.Workbook(str(tmp_path / "bad.xlsx"))
        workbook.add_worksheet("subject").write_row(0, 0, ["source", "ado_count"])
        workbook.close()
        with pytest.raises(MappingWorkbookError, match="missing column"):
            read_mapping(tmp_path / "bad.xlsx")

    def test_unreadable_file_refused(self, tmp_path):
        (tmp_path / "not.xlsx").write_text("nope")
        with pytest.raises(MappingWorkbookError, match="cannot open"):
            read_mapping(tmp_path / "not.xlsx")
