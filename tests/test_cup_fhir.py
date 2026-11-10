import gzip
import json
from pathlib import Path

import pandas as pd

from cup_fhir import (
    LOINC,
    SNOMED,
    has_category,
    has_code,
    identifier_value,
    latest_per_patient,
    load_bundles,
    patient_names_and_ages,
    to_dataframes,
    validate_bundles,
    with_patient,
)

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


def test_load_sample_folder():
    result = load_bundles(SAMPLE)
    assert len(result.bundles) == 2
    assert result.errors == []
    assert [Path(p).name for p in result.files_without_bundles] == ["not-a-bundle.json"]


def test_ndjson_gz_with_several_bundles(tmp_path):
    lines = "\n".join(json.dumps({"resourceType": "Bundle", "id": f"b{i}", "type": "collection"}) for i in range(3))
    (tmp_path / "export.ndjson.gz").write_bytes(gzip.compress(lines.encode()))
    (tmp_path / "broken.json").write_text("{not json")
    result = load_bundles(tmp_path)
    assert len(result.bundles) == 3
    assert len(result.errors) == 1


def test_validate_sample_bundles():
    valid, invalid = validate_bundles(load_bundles(SAMPLE).bundles)
    assert len(valid) == 2 and not invalid


def test_dataframes_and_queries():
    dfs = to_dataframes(load_bundles(SAMPLE).bundles)
    assert set(dfs) == {"Condition", "MedicationRequest", "Observation", "Patient"}

    patients = patient_names_and_ages(dfs["Patient"], today=pd.Timestamp("2026-06-14"))
    alice = patients.set_index("id").loc["alice"]
    assert alice["given"] == "Alice May" and alice["age"] == 71
    assert identifier_value(patients["identifier"], "http://example.org/fhir/mrn").sort_values().tolist() == ["MRN001", "MRN002", "MRN003"]

    obs = dfs["Observation"]
    hba1c = obs[has_code(obs["code.coding"], "4548-4", LOINC)]
    assert len(hba1c) == 4
    assert has_category(obs["category"], "vital-signs").sum() == 1

    # Both urn:uuid and Patient/<id> references resolve
    joined = with_patient(hba1c, patients)
    assert joined["patient_id"].notna().all()
    assert len(joined) == len(hba1c)

    joined["effective"] = pd.to_datetime(joined["effectiveDateTime"], format="ISO8601", utc=True)
    latest = latest_per_patient(joined, "effective").set_index("patient_id")
    assert latest.loc["alice", "valueQuantity.value"] == 8.4

    cond = dfs["Condition"]
    active_t2dm = cond[has_code(cond["clinicalStatus.coding"], "active") & has_code(cond["code.coding"], "44054006", SNOMED)]
    assert with_patient(active_t2dm, patients)["patient_id"].tolist() == ["alice"]
