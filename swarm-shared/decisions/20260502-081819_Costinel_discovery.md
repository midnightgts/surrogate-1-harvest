# Costinel / discovery

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid rate limits when downloading dataset files.

### Implementation Plan
1. **Identify dataset files**: Pre-list file paths once using a single API call to `list_repo_tree(path, recursive=False)` for one date folder.
2. **Save file list to JSON**: Embed the list in the training script `train.py`.
3. **Modify training script**: Use CDN-only fetches with zero API calls during data load by downloading files from `https://huggingface.co/datasets/{repo}/resolve/main/{path}`.

### Code Snippets
```python
import json
import requests

# Pre-list file paths once
def get_file_list(repo, path):
    response = requests.get(f"https://huggingface.co/api/v1/repo/{repo}/tree/{path}", params={"recursive": False})
    file_list = response.json()["files"]
    return file_list

# Save file list to JSON
file_list = get_file_list("axentx/dataset", "2023-02-20")
with open("file_list.json", "w") as f:
    json.dump(file_list, f)

# Modify training script to use CDN-only fetches
def download_file(file_path):
    url = f"https://huggingface.co/datasets/axentx/dataset/resolve/main/{file_path}"
    response = requests.get(url)
    return response.content

# Load file list from JSON
with open("file_list.json", "r") as f:
    file_list = json.load(f)

# Download files using CDN-only fetches
for file_path in file_list:
    file_content = download_file(file_path)
    # Process file content
```
### Benefits
This improvement avoids rate limits when downloading dataset files, reducing the time spent on data loading and increasing the overall efficiency of the training process.
