# surrogate-1 / quality

**Surrogate-1 Quality Improvement**

### **Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently.
* The dataset-mirror writes mixed-schema files to enriched/, which can cause issues during upload.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently.
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.

### **Proposed change**
Implement HF CDN Bypass for dataset training by downloading public dataset files at `https://huggingface.co/datasets/{repo}/resolve/main/{path}` with NO Authorization header.

### **Implementation**
1. Update `train.py` to use the HF CDN Bypass:
```python
import requests
from huggingface_hub import hf_hub_download

# ...

# Download public dataset files from HF CDN
file_paths = requests.get(f"https://huggingface.co/datasets/{repo}/resolve/main/{path}").json()
for file_path in file_paths:
    hf_hub_download(repo, file_path, force_filename=True)
```
2. Update `dataset-mirror` to project to {prompt, response} only before upload:
```python
import pandas as pd

# ...

# Project to {prompt, response} only before upload
df = pd.read_parquet("batches/mirror-merged/{date}/{slug}.parquet")
df = df[["prompt", "response"]]
df.to_parquet("batches/mirror-merged/{date}/{slug}-projected.parquet", index=False)
```
3. Update `dataset-mirror` to move attribution to filename pattern (`batches/mirror-merged/{date}/{slug}.parquet`):
```python
import os

# ...

# Move attribution to filename pattern
os.rename("batches/mirror-merged/{date}/{slug}.parquet", "batches/mirror-merged/{date}/{slug}-projected.parquet")
```
4. Update `dataset-mirror` to don't add `source` / `ts` cols:
```python
import pandas as pd

# ...

# Don't add `source` / `ts` cols
df = df[["prompt", "response"]]
```
### **Verification**
1. Run `train.py` with the updated HF CDN Bypass implementation.
2. Verify that the dataset-mirror writes mixed-schema files to enriched/ are fixed.
3. Verify that the existing Lightning Studio instances are reused efficiently.
4. Verify that the dataset training is not blocked by Hugging Face API rate limits.
