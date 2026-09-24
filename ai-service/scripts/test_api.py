from pathlib import Path
import requests, json

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "examples" / "service_sample.pdf"
with open(SAMPLE_PDF, "rb") as f:
    r = requests.post("http://localhost:8000/api/contract/extract",
                      files={"file": ("s.pdf", f.read())}, timeout=60)
raw = r.json()
data = raw.get("data", {})
print("=== data keys ===")
print(list(data.keys()))
print("\n=== extraction keys (should be FLAT now) ===")
ext = data.get("extraction", {})
print(f"keys: {list(ext.keys())}")
print(f"has nested extraction? {'extraction' in ext and isinstance(ext['extraction'], dict)}")
print("\n=== sample fields ===")
print(f"contract_name = {ext.get('contract_name')}")
print(f"amount        = {ext.get('amount')}")
print(f"sign_date     = {ext.get('sign_date')}")
print(f"partner_a     = {ext.get('partner_a')}")
print(f"confidence    = {ext.get('confidence')}")
print(f"attempt_count = {ext.get('attempt_count')}")
print("\n=== classify ===")
cl = data.get("classify", {})
print(json.dumps(cl, ensure_ascii=False, indent=2))
