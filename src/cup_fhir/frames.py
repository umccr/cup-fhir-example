"""Turn FHIR Bundles into pandas DataFrames, plus helpers for querying them.

``pd.json_normalize`` flattens nested *objects* into dotted columns
(``subject.reference``, ``valueQuantity.value``) but leaves *arrays* as list
columns (``code.coding``, ``name``, ``identifier``). Most helpers here exist to
query those list columns without users having to write lambdas each time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import pandas as pd

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"


# --------------------------------------------------------------------------- building


def to_dataframes(bundles: dict[str, dict]) -> dict[str, pd.DataFrame]:
    """One DataFrame per resourceType, built from every entry in every Bundle.

    Two extra columns are added to each row:

    * ``_source`` - the Bundle key the resource came from (see ``load_bundles``)
    * ``_fullUrl`` - the entry's ``fullUrl``, needed to resolve ``urn:uuid:`` references
    """
    by_type: dict[str, list[dict]] = defaultdict(list)
    for source, bundle in bundles.items():
        for entry in bundle.get("entry", []) or []:
            resource = entry.get("resource")
            if resource and "resourceType" in resource:
                by_type[resource["resourceType"]].append(
                    {"_source": source, "_fullUrl": entry.get("fullUrl"), **resource}
                )
    return {t: pd.json_normalize(rows) for t, rows in sorted(by_type.items())}


def populated(df: pd.DataFrame) -> pd.Series:
    """Fraction of rows with a value in each column, most populated first.

    A quick way to see which FHIR elements this dataset actually uses.
    """
    return df.notna().mean().sort_values(ascending=False)


# --------------------------------------------------------------------------- CodeableConcepts


def codes(series: pd.Series, system: str | None = None) -> pd.Series:
    """List of codes in each row of a ``*.coding`` column, optionally filtered by system."""

    def extract(coding):
        if not isinstance(coding, list):
            return []
        return [c.get("code") for c in coding if system is None or c.get("system") == system]

    return series.apply(extract)


def has_code(series: pd.Series, code: str | Iterable[str], system: str | None = None) -> pd.Series:
    """Boolean mask: does the ``*.coding`` column contain ``code`` (or any of a set of codes)?

    >>> observations[has_code(observations["code.coding"], "4548-4", LOINC)]
    """
    wanted = {code} if isinstance(code, str) else set(code)
    return codes(series, system).apply(lambda cs: bool(wanted.intersection(cs)))


def has_category(series: pd.Series, code: str) -> pd.Series:
    """Boolean mask for a ``category`` column (a *list* of CodeableConcepts).

    >>> observations[has_category(observations["category"], "laboratory")]
    """

    def match(categories):
        if not isinstance(categories, list):
            return False
        return any(c.get("code") == code for cat in categories for c in cat.get("coding", []) or [])

    return series.apply(match)


def identifier_value(series: pd.Series, system: str) -> pd.Series:
    """Value of the identifier with the given ``system`` from an ``identifier`` column."""

    def pick(ids):
        if not isinstance(ids, list):
            return None
        return next((i.get("value") for i in ids if i.get("system") == system), None)

    return series.apply(pick)


# --------------------------------------------------------------------------- Patients


def patient_names_and_ages(patients: pd.DataFrame, today: pd.Timestamp | None = None) -> pd.DataFrame:
    """Return a copy of a Patient DataFrame with ``family``, ``given`` and ``age`` columns.

    Uses the first ``name`` entry. ``birthDate`` is converted to a datetime.
    """
    df = patients.copy()
    first_name = df["name"].str[0] if "name" in df else pd.Series(index=df.index, dtype=object)
    df["family"] = first_name.str.get("family")
    df["given"] = first_name.str.get("given").apply(lambda g: " ".join(g) if isinstance(g, list) else None)

    if "birthDate" in df:
        bd = pd.to_datetime(df["birthDate"], format="ISO8601", errors="coerce")
        df["birthDate"] = bd
        today = (today or pd.Timestamp.today()).normalize()
        before_birthday = (bd.dt.month > today.month) | ((bd.dt.month == today.month) & (bd.dt.day > today.day))
        df["age"] = (today.year - bd.dt.year - before_birthday.astype(int)).astype("Int64")
    return df


def _patient_lookup(patients: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Map every way a Patient can be referenced to that Patient's columns."""
    has_id = "id" in patients.columns
    keep = ["_source", *([c for c in ["id", *columns] if c in patients.columns and c != "_source"])]
    parts = []
    if has_id:
        by_id = patients[keep].assign(_ref="Patient/" + patients["id"].astype(str))
        parts.append(by_id)
    by_url = patients[keep].assign(_ref=patients["_fullUrl"])
    parts.append(by_url)
    lookup = pd.concat(parts).dropna(subset=["_ref"])
    lookup = lookup.drop_duplicates(["_ref", "_source"])
    return lookup.rename(columns={c: f"patient_{c}" for c in keep if c != "_source"})


def with_patient(
    df: pd.DataFrame,
    patients: pd.DataFrame,
    ref_col: str = "subject.reference",
    columns: Iterable[str] = ("id", "family", "given", "gender", "age", "birthDate"),
) -> pd.DataFrame:
    """Add the referenced Patient's details to each row, as ``patient_*`` columns.

    Handles both reference styles: ``Patient/123`` and ``urn:uuid:...`` (common in
    transaction bundles). Matches within the same Bundle, because ``urn:uuid``
    references are only unique inside one Bundle. Rows whose patient can't be
    found keep NaN in the ``patient_*`` columns.

    Pass ``patient_names_and_ages(patients)`` to get name and age columns.
    """
    lookup = _patient_lookup(patients, list(columns))
    merged = df.merge(lookup, how="left", left_on=[ref_col, "_source"], right_on=["_ref", "_source"])
    return merged.drop(columns="_ref")


def latest_per_patient(df: pd.DataFrame, date_col: str, patient_col: str = "patient_id") -> pd.DataFrame:
    """Keep the most recent row per patient (e.g. the latest HbA1c for each person)."""
    return df.sort_values(date_col).groupby(patient_col, dropna=False).tail(1).reset_index(drop=True)


# --------------------------------------------------------------------------- Medications


def extract_med_ingredients(
    med_administrations: pd.DataFrame,
    patients: pd.DataFrame,
) -> pd.DataFrame:
    """Return a tidy DataFrame of medication ingredients from MedicationAdministration resources.

    Each row in ``med_administrations`` may reference a contained ``Medication`` resource
    with either a single top-level code (simple drug) or a list of ingredients (combination
    regimen). Patient references are resolved to ``study_id`` via ``patients``.

    Returns columns: ``study_id``, ``regimen``, ``ingredient_code``, ``ingredient_display``.
    """

    def _ingredients(row: dict) -> list[dict]:
        contained = row.get("contained", [])
        if not isinstance(contained, list) or not contained:
            return []
        med = contained[0]
        top_coding = med.get("code", {}).get("coding", [])
        if top_coding:
            return [{"code": c["code"], "display": c.get("display", "")} for c in top_coding]
        result = []
        for ing in med.get("ingredient", []):
            for c in ing.get("itemCodeableConcept", {}).get("coding", []):
                result.append({"code": c["code"], "display": c.get("display", "")})
        return result

    rows = []
    for _, admin in med_administrations.iterrows():
        patient_ref = admin.get("subject.reference", "")
        matched = patients[patients["_fullUrl"] == patient_ref]
        study_id = matched["study_id"].iloc[0] if not matched.empty else patient_ref
        regimen = admin.get("contained", [{}])[0].get("code", {}).get("text", "") or ""
        for ing in _ingredients(admin.to_dict()):
            rows.append({
                "study_id": study_id,
                "regimen": regimen,
                "ingredient_code": ing["code"],
                "ingredient_display": ing["display"],
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- DocumentReferences / VCF


def _vcf_type_key(doc: dict) -> str | None:
    """Return a column name for a DocumentReference, or None if it isn't a VCF.

    Uses ``content[].format.display`` (EDAM ontology) to confirm the file is a VCF,
    then SO category codes to determine origin and variant class:

    * ``SO:0001777`` / ``somatic_variant``  → somatic
    * ``SO:0001778`` / ``germline_variant`` → germline
    * ``SO:0001537`` / ``structural_variant`` in ``type.coding`` → SV, else SNV
    """
    # Must have at least one content item whose format is VCF
    formats = [
        c.get("format", {}).get("display", "")
        for c in doc.get("content") or []
    ]
    if not any(f.lower() == "vcf" for f in formats):
        return None

    # Origin from category SO code / display
    cat_display = (
        (doc.get("category") or [{}])[0]
        .get("coding", [{}])[0]
        .get("display", "")
    )
    cat_code = (
        (doc.get("category") or [{}])[0]
        .get("coding", [{}])[0]
        .get("code", "")
    )
    if "germline" in cat_display or cat_code == "SO:0001778":
        origin = "germline"
    else:
        origin = "somatic"

    # Variant class from type.coding SO code / display
    type_codings = (doc.get("type") or {}).get("coding") or []
    type_displays = [c.get("display", "") for c in type_codings]
    type_codes = [c.get("code", "") for c in type_codings]
    if any("structural" in d for d in type_displays) or "SO:0001537" in type_codes:
        variant_class = "sv"
    else:
        variant_class = "snv"

    return f"{origin}_{variant_class}_vcf"


def _source_prefix(bundle_key: str) -> str:
    """Return the directory portion of a bundle key, with a trailing separator.

    Works for both local paths (``data/real/SN1.json#0`` → ``data/real/``) and
    S3 URIs (``s3://bucket/prefix/SN1.json#0`` → ``s3://bucket/prefix/``).
    The ``#<n>`` NDJSON suffix is stripped before the parent is computed.
    """
    # Strip the #<n> suffix added by load_bundles
    path = bundle_key.split("#")[0]
    if path.startswith("s3://"):
        # Keep the s3:// scheme intact; rsplit on / to get the parent
        parent = path.rsplit("/", 1)[0]
        return parent.rstrip("/") + "/"
    else:
        from pathlib import PurePosixPath
        parent = str(PurePosixPath(path).parent)
        return parent.rstrip("/") + "/"


def build_vcf_table(bundles: dict) -> pd.DataFrame:
    """Build one row per bundle with VCF URLs pivoted into typed columns.

    Resolves the chain:
    ``DocumentReference → Specimen (tumour library) → biopsy Specimen → bodySite``

    ``subject_id`` is the patient study ID taken from the Patient resource inside each
    bundle (falls back to the bundle key if no identifier is present).

    Returns columns: ``subject_id``, ``tumour_library_id``, ``sample_location``,
    and one column per VCF type present (``somatic_snv_vcf``, ``germline_snv_vcf``,
    ``somatic_sv_vcf``, ``germline_sv_vcf``).
    """

    def _library_id(resource: dict) -> str | None:
        return next(
            (i["value"] for i in resource.get("identifier", [])
             if "library" in i.get("system", "")),
            None,
        )

    rows = []
    for bundle_key, bundle in bundles.items():
        prefix = _source_prefix(bundle_key)
        by_url = {
            e["fullUrl"]: e["resource"]
            for e in bundle.get("entry", [])
            if "fullUrl" in e
        }

        # Resolve study ID from the Patient resource inside this bundle
        patient = next(
            (r for r in by_url.values() if r.get("resourceType") == "Patient"),
            None,
        )
        ids = (patient or {}).get("identifier", [])
        study_id = ids[0]["value"] if isinstance(ids, list) and ids else bundle_key

        biopsy_specimen = next(
            (r for r in by_url.values()
             if r.get("resourceType") == "Specimen"
             and not r.get("parent")
             and not _library_id(r)),
            None,
        )
        sample_location = (
            biopsy_specimen.get("collection", {}).get("bodySite", {}).get("text", "unknown")
            if biopsy_specimen else "unknown"
        )

        tumour_lib = next(
            (r for r in by_url.values()
             if r.get("resourceType") == "Specimen"
             and r.get("parent")
             and _library_id(r)),
            None,
        )
        tumour_library_id = _library_id(tumour_lib) if tumour_lib else None

        row: dict = {
            "subject_id": study_id,
            "tumour_library_id": tumour_library_id,
            "sample_location": sample_location,
        }
        for r in by_url.values():
            if r.get("resourceType") != "DocumentReference":
                continue
            col = _vcf_type_key(r)
            if col is None:
                continue
            vcf_url = r.get("content", [{}])[0].get("attachment", {}).get("url")
            if vcf_url:
                row[col] = prefix + vcf_url
        rows.append(row)

    col_order = [
        "subject_id", "tumour_library_id", "sample_location",
        "somatic_snv_vcf", "germline_snv_vcf",
        "somatic_sv_vcf", "germline_sv_vcf",
    ]
    df = pd.DataFrame(rows)
    df = df[[c for c in col_order if c in df.columns]]
    return df.sort_values("subject_id").reset_index(drop=True)
