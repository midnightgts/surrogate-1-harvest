# workio / discovery

**High-Value Incremental Improvement for Workio Frontend and Backend**
### Implementation Plan

**Task:** Implement HF CDN Bypass for Training Pipeline and Surrogate-1 Training Pipeline

**Time Estimate:** 1 hour 30 minutes

**Implementation Steps:**

1. **HF CDN Bypass:**
	* Create a new script `hf_cdn_bypass.py` in `workio/server` directory:
	```python
import requests
import json

def get_file_list(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    if response.status_code == 200:
        return json.loads(response.content)
    else:
        return []

def download_file(repo, path, filename):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}/{filename}"
    response = requests.get(url)
    if response.status_code == 200:
        with open(filename, 'wb') as f:
            f.write(response.content)
    else:
        print(f"Failed to download {filename}")

# Example usage:
repo = "your-repo-name"
path = "your-path"
file_list = get_file_list(repo, path)
for file in file_list:
    download_file(repo, path, file)
```
	* Update `train.py` in `workio/server` directory to use the `hf_cdn_bypass.py` script:
	```python
import hf_cdn_bypass

# ...

file_list = hf_cdn_bypass.get_file_list(repo, path)
for file in file_list:
    hf_cdn_bypass.download_file(repo, path, file)

# ...
```
2. **Surrogate-1 Training Pipeline:**
	* Update `surrogate_1_train.py` in `workio/server` directory to use the `hf_cdn_bypass.py` script:
	```python
import hf_cdn_bypass

# ...

file_list = hf_cdn_bypass.get_file_list(repo, path)
for file in file_list:
    hf_cdn_bypass.download_file(repo, path, file)

# ...
```
3. **Testing:**
	* Run `train.py` and `surrogate_1_train.py` to test the HF CDN Bypass implementation.

**Commit Message:**
```
Implement HF CDN Bypass for Training Pipeline and Surrogate-1 Training Pipeline
```
