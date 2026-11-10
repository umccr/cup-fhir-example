# cup-fhir-example

Load FHIR R4 Bundles from an S3 bucket (or a local folder) into pandas DataFrames, and query them in a Jupyter notebook.

It's aimed at people who know pandas but not FHIR. The `cup_fhir` package handles finding and parsing the bundles, and turns FHIR's nested JSON into one DataFrame per resource type. It also adds helpers for the parts that are awkward in pandas: coded values, identifiers, and references between resources.

## What's in here

```
├── notebooks/
│   └── 01_explore_fhir_bundles.ipynb   # start here: load, validate, query
├── src/cup_fhir/
│   ├── loader.py                       # find + load Bundles from S3 or local disk
│   └── frames.py                       # DataFrames and query helpers
├── data/sample/                        # small synthetic bundles (no real patient data)
├── tests/                              # pytest tests run against the sample data
└── pyproject.toml                      # package metadata and dependencies
```

The code that does the work is in `src/cup_fhir/`, and the notebook calls it. This is a common Jupyter convention: notebooks are good for exploring and explaining, but code kept in a normal Python package can be tested, reviewed in pull requests, and shared by several notebooks.

## Setup

You need Python 3.10 or newer.

### Option A: on your own machine

```bash
git clone git@github.com:umccr/cup-fhir-example.git
cd cup-fhir-example

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[notebook]"

# Register this environment as a Jupyter "kernel" so notebooks can use it
python -m ipykernel install --user --name cup-fhir --display-name "Python (cup-fhir)"

jupyter lab
```

In JupyterLab, open `notebooks/01_explore_fhir_bundles.ipynb` and check that the kernel name in the top right says **Python (cup-fhir)**. If it doesn't, click it and choose that kernel.

`pip install -e` installs the package in editable mode, so changes to the files in `src/cup_fhir/` take effect without reinstalling. The notebook also turns on `autoreload`, so you don't even need to restart the kernel.

### Option B: on a hosted Jupyter (JupyterHub, SageMaker, etc.)

Open a terminal from the Jupyter launcher, then run:

```bash
git clone https://github.com/umccr/cup-fhir-example.git
cd cup-fhir-example
pip install -e .
```

Then open the notebook. If `import cup_fhir` fails, the notebook's kernel is probably using a different Python from the terminal. In that case, run this in a notebook cell and then restart the kernel:

```python
%pip install -e ..
```

### Option C: just the package, no clone

To use the helpers from a notebook somewhere else:

```python
%pip install "git+https://github.com/umccr/cup-fhir-example.git"
```

## AWS credentials

`load_bundles("s3://...")` uses the standard AWS credential chain: environment variables, `~/.aws/credentials`, SSO, or an instance role. To choose a profile, pass `profile="my-profile"`. For a public bucket, pass `unsigned=True`. If you use AWS SSO, run `aws sso login --profile my-profile` in a terminal before running the notebook.

## Quick start

```python
from cup_fhir import load_bundles, to_dataframes, has_code, with_patient, patient_names_and_ages, LOINC

result = load_bundles("s3://my-bucket/path/to/fhir/")     # or a local folder, e.g. "data/sample"
dfs = to_dataframes(result.bundles)                        # {"Patient": DataFrame, "Observation": ...}

patients = patient_names_and_ages(dfs["Patient"])
obs = dfs["Observation"]

hba1c = obs[has_code(obs["code.coding"], "4548-4", LOINC)]
with_patient(hba1c, patients)[["patient_family", "patient_age", "effectiveDateTime", "valueQuantity.value"]]
```

The notebook goes through many more examples.

## Committing notebooks: strip outputs first

Notebook files (`.ipynb`) save cell outputs, including tables of whatever data you loaded. **With real patient data, those outputs must never be committed.** Install [nbstripout](https://github.com/kynan/nbstripout) once per clone. It removes outputs automatically whenever you commit:

```bash
pip install -e ".[dev]"
nbstripout --install
```

The `data/` folder is also git-ignored, except for `data/sample/`, so files you download for exploring don't get committed by accident.

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

## Notes on FHIR R4

- Validation uses `fhir.resources`' **R4B** models. They are the closest set the library ships to R4 and accept almost all R4 data. Bundles that fail validation are still available as raw JSON in `result.bundles`.
- Columns only exist if the data uses them. For example, `valueQuantity.value` is missing if every Observation uses `valueCodeableConcept`. Run `populated(df)` to see which fields a dataset actually fills in.
- References in transaction bundles are often `urn:uuid:...` rather than `Patient/123`. `with_patient` handles both.
