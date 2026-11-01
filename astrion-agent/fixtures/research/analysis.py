"""Fixed stage-3 task, executed only in the configured isolated runtime."""

import csv
import hashlib
import json
from pathlib import Path
from statistics import mean
import matplotlib.pyplot as plt

source = Path("/inputs/measurements.csv")
groups = {}
with source.open() as handle:
    for row in csv.DictReader(handle):
        groups.setdefault(row["group"], []).append(float(row["value"]))
result = {key: mean(values) for key, values in sorted(groups.items())}
sha = hashlib.sha256(source.read_bytes()).hexdigest()
Path("result.json").write_text(
    json.dumps({"means": result, "source": source.name, "sha256": sha}, indent=2)
)
plt.bar(list(result), list(result.values()))
plt.ylabel("Mean value")
plt.title("Group means — synthetic fixture")
plt.savefig("chart.png", dpi=120, bbox_inches="tight")
plt.close()
Path("report.md").write_text(
    f'# Group means\n\nA = {result["A"]}; B = {result["B"]}.\n\n'
    f"Source: `{source.name}`, SHA-256 `{sha}`.\n\n"
    "These four synthetic observations demonstrate the workflow; they do not establish a scientific effect.\n"
)
print(json.dumps(result))
