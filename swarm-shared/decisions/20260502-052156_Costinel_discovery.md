# Costinel / discovery

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the **HF CDN Bypass** pattern to avoid rate-limit blocks during dataset training.

### Implementation Plan
1. **Identify the dataset repository**: Determine the repository containing the dataset to be used for training.
2. **Get the list of file paths**: Use the `list_repo_tree` API to retrieve the list of file paths for the dataset repository. This can be done with a single API call.
3. **Save the list to a JSON file**: Store the list of file paths in a JSON file to be used in the training script.
4. **Modify the training script**: Update the training script to use the CDN URLs for downloading the dataset files. This can be done by replacing the `load_dataset` function with a custom implementation that downloads the files from the CDN.

### Code Snippets
```python
import json
import requests

# Get the list of file paths
repo_id = "dataset/repo"
file_paths = []
response = requests.get(f"https://huggingface.co/{repo_id}/tree/main")
for file in response.json():
    file_paths.append(file["path"])

# Save the list to a JSON file
with open("file_paths.json", "w") as f:
    json.dump(file_paths, f)

# Modify the training script
import pandas as pd

# Load the list of file paths
with open("file_paths.json", "r") as f:
    file_paths = json.load(f)

# Download the dataset files from the CDN
dataset_files = []
for file_path in file_paths:
    response = requests.get(f"https://huggingface.co/{repo_id}/resolve/main/{file_path}")
    dataset_files.append(response.content)

# Load the dataset into a Pandas dataframe
df = pd.DataFrame(dataset_files)
```
### Benefits
The HF CDN Bypass pattern allows for faster dataset training by avoiding rate-limit blocks. This improvement can be shipped in <2h and provides a significant benefit to the Costinel project.
