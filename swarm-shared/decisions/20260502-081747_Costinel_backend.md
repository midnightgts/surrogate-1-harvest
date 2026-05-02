# Costinel / backend

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate limits when downloading dataset files.

### Implementation Plan
1. **Identify the dataset files to download**: Use the `list_repo_tree` API to get the list of files in the dataset repository.
2. **Create a JSON file with the file paths**: Save the list of file paths to a JSON file.
3. **Modify the training script to use the CDN**: Update the training script to download the dataset files from the CDN using the file paths in the JSON file.
4. **Test the modified training script**: Run the modified training script to ensure that it downloads the dataset files correctly and avoids API rate limits.

### Code Snippets
```python
import json
import requests

# Get the list of files in the dataset repository
repo_tree = requests.get(f"https://huggingface.co/datasets/{repo}/tree/main").json()

# Create a JSON file with the file paths
file_paths = [file["path"] for file in repo_tree["files"]]
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# Modify the training script to use the CDN
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

for file_path in file_paths:
    file_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
    response = requests.get(file_url)
    with open(file_path, "wb") as f:
        f.write(response.content)
```
### Benefits
The HF CDN Bypass pattern avoids API rate limits when downloading dataset files, allowing for faster and more efficient training. This improvement can be shipped in <2h and has a significant impact on the performance of the Costinel platform.
