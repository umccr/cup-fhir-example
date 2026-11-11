"""AMT (Australian Medicines Terminology) helpers via CSIRO Ontoserver.

Uses the public CSIRO Ontoserver instance which carries SNOMED CT-AU including
AMT content. No authentication is required for reads.
"""

from __future__ import annotations

import re

import pandas as pd

ONTOSERVER = "https://tx.ontoserver.csiro.au/fhir"
SNOMED_SYSTEM = "http://snomed.info/sct"

# SNOMED ECL root for the entire medicinal product hierarchy
_MEDICINAL_PRODUCT_ECL = "<<763158003"


def amt_lookup(code: str, ontoserver: str = ONTOSERVER) -> dict:
    """Return display name and selected properties for a SNOMED CT-AU / AMT concept.

    Parameters
    ----------
    code:
        SNOMED concept code to look up.
    ontoserver:
        Base URL of a FHIR terminology server. Defaults to the CSIRO public instance.
    """
    import requests

    resp = requests.get(
        f"{ontoserver}/CodeSystem/$lookup",
        params={"system": SNOMED_SYSTEM, "code": code, "_format": "json"},
        timeout=15,
    )
    resp.raise_for_status()
    params = resp.json().get("parameter", [])

    result: dict = {}
    for p in params:
        name = p["name"]
        value = p.get("valueString") or p.get("valueCode") or p.get("valueUri")
        if name in ("display", "version") and value:
            result[name] = value
        elif name == "property" and "part" in p:
            parts = {x["name"]: x.get("valueString") or x.get("valueCode") for x in p["part"]}
            prop_code = parts.get("code")
            prop_val = parts.get("value")
            if prop_code in ("moduleId", "effectiveTime", "inactive") and prop_val:
                result[prop_code] = prop_val
    return result


def amt_search(
    query: str,
    ecl: str = _MEDICINAL_PRODUCT_ECL,
    max_results: int = 10,
    ontoserver: str = ONTOSERVER,
) -> list[dict]:
    """Free-text search within an ECL-defined scope.

    Parameters
    ----------
    query:
        Free-text search string, e.g. ``"paracetamol 500 tablet"``.
    ecl:
        SNOMED ECL expression. Defaults to ``<<763158003`` (all medicinal products).
    max_results:
        Maximum number of concepts to return.
    ontoserver:
        Base URL of a FHIR terminology server.

    Returns a list of ``{code, display}`` dicts.
    """
    import requests

    resp = requests.get(
        f"{ontoserver}/ValueSet/$expand",
        params={
            "url": f"{SNOMED_SYSTEM}?fhir_vs=ecl/{ecl}",
            "filter": query,
            "count": max_results,
            "_format": "json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    contains = resp.json().get("expansion", {}).get("contains", [])
    return [{"code": c["code"], "display": c["display"]} for c in contains]


def resolve_mp_concept(
    code: str,
    display: str,
    ontoserver: str = ONTOSERVER,
) -> dict | None:
    """Return ``{code, display}`` for the medicinal product concept on the server.

    Tries ``$lookup`` by code first. If the code isn't present (version mismatch),
    falls back to a name search within the medicinal product hierarchy (``<<763158003``).

    Returns ``None`` if neither approach finds a match.
    """
    import requests

    resp = requests.get(
        f"{ontoserver}/CodeSystem/$lookup",
        params={"system": SNOMED_SYSTEM, "code": code, "_format": "json"},
        timeout=15,
    )
    data = resp.json()
    if data.get("resourceType") != "OperationOutcome":
        disp = next(
            (p["valueString"] for p in data["parameter"] if p["name"] == "display"),
            display,
        )
        return {"code": code, "display": disp}

    # Fallback: name search in the medicinal product hierarchy
    resp2 = requests.get(
        f"{ontoserver}/ValueSet/$expand",
        params={
            "url": f"{SNOMED_SYSTEM}?fhir_vs=ecl/{_MEDICINAL_PRODUCT_ECL}",
            "filter": display,
            "count": 1,
            "_format": "json",
        },
        timeout=15,
    )
    contains = resp2.json().get("expansion", {}).get("contains", [])
    return {"code": contains[0]["code"], "display": contains[0]["display"]} if contains else None


def ctpp_for_concept(
    concept_code: str,
    max_results: int = 50,
    ontoserver: str = ONTOSERVER,
) -> pd.DataFrame:
    """Return a DataFrame of CTPP concepts that are descendants of the given medicinal product.

    CTPPs are identified by having a dose/form descriptor in their display name
    (e.g. ``'350 mg/7 mL injection, vial'``) while excluding abstract AMT hierarchy
    nodes like ``'X only product'`` or ``'X-containing product'``.

    Parameters
    ----------
    concept_code:
        SNOMED code of the parent medicinal product concept.
    max_results:
        Maximum number of descendant concepts to retrieve before filtering.
    ontoserver:
        Base URL of a FHIR terminology server.

    Returns columns ``ctpp_code`` and ``ctpp_display``.
    """
    import requests

    resp = requests.get(
        f"{ontoserver}/ValueSet/$expand",
        params={
            "url": f"{SNOMED_SYSTEM}?fhir_vs=ecl/<<{concept_code}",
            "count": max_results,
            "_format": "json",
        },
        timeout=15,
    )
    contains = resp.json().get("expansion", {}).get("contains", [])

    def _is_ctpp(display: str) -> bool:
        d = display.lower()
        if "only product" in d or "containing product" in d:
            return False
        has_dose = bool(re.search(r"\d+\s*(mg|mcg|g|mL|mmol|IU|unit)", d, re.I))
        has_form = bool(re.search(
            r"(tablet|capsule|injection|infusion|solution|vial|ampoule|patch|cream|ointment|spray|inhaler|powder)",
            d, re.I,
        ))
        return has_dose or has_form

    return pd.DataFrame(
        [{"ctpp_code": c["code"], "ctpp_display": c["display"]}
         for c in contains if _is_ctpp(c["display"])]
    )


def med_ingredients_to_ctpp(
    med_ingredients: pd.DataFrame,
    ontoserver: str = ONTOSERVER,
) -> pd.DataFrame:
    """Resolve each ingredient row to its CTPP product forms.

    Expects a DataFrame with at least ``study_id``, ``ingredient_code``, and
    ``ingredient_display`` columns (as returned by :func:`extract_med_ingredients`).

    Returns a DataFrame with ``study_id``, ``ingredient``, ``ctpp_code``, and
    ``ctpp_display`` columns, or an empty DataFrame if nothing could be resolved.
    """
    all_rows = []
    for _, row in med_ingredients.iterrows():
        mp = resolve_mp_concept(row["ingredient_code"], row["ingredient_display"], ontoserver)
        if mp is None:
            continue
        ctpps = ctpp_for_concept(mp["code"], ontoserver=ontoserver)
        if ctpps.empty:
            continue
        ctpps.insert(0, "ingredient", mp["display"])
        ctpps.insert(0, "study_id", row["study_id"])
        all_rows.append(ctpps)
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
