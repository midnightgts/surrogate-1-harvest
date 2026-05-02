# axiomops / discovery

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass pattern to avoid API rate limits when downloading dataset files.

### Implementation Plan
1. **Identify dataset files to download**: Review the `list_repo_tree` API calls in the training scripts and identify the dataset files that need to be downloaded.
2. **Modify training scripts to use CDN**: Update the training scripts to use the HF CDN to download dataset files instead of the API. This can be done by replacing the `load_dataset` function with a custom function that downloads the files from the CDN using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern.
3. **Embed file list in training script**: Pre-list the file paths once and embed them in the training script to avoid making API calls during training.

### Code Snippets
```python
import requests

def download_dataset_file(repo, path):
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    response = requests.get(url)
    with open(path, 'wb') as f:
        f.write(response.content)

# Example usage:
repo = "my-dataset"
path = "data/train.csv"
download_dataset_file(repo, path)
```
```python
import json

def get_file_list(repo, date_folder):
    file_list_url = f"https://huggingface.co/datasets/{repo}/tree/main/{date_folder}"
    response = requests.get(file_list_url)
    file_list = response.json()
    with open('file_list.json', 'w') as f:
        json.dump(file_list, f)

# Example usage:
repo = "my-dataset"
date_folder = "2026-04-29"
get_file_list(repo, date_folder)
```
### Benefits
Implementing the HF CDN Bypass pattern will allow the training scripts to download dataset files without being limited by the API rate limits, reducing the overall training time and improving the efficiency of the Surrogate System.
