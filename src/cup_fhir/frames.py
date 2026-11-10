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
    keep = ["_source", "id", *[c for c in columns if c in patients and c != "id"]]
    by_id = patients[keep].assign(_ref="Patient/" + patients["id"].astype(str))
    by_url = patients[keep].assign(_ref=patients["_fullUrl"])
    lookup = pd.concat([by_id, by_url]).dropna(subset=["_ref"])
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
