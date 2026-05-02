# Costinel / discovery

### Highest-Value Incremental Improvement
Based on the provided patterns and lessons learned, the highest-value incremental improvement that can ship in <2h is to implement the HF CDN Bypass for dataset training. This involves downloading public dataset files from the HF CDN without using the HF API, which bypasses the rate limit entirely.

### Implementation Plan
1. **Identify datasets**: Determine which datasets are being used for training and are available on the HF CDN.
2. **Modify training script**: Update the training script to download dataset files from the HF CDN using the `https://huggingface.co/datasets/{repo}/resolve/main/{path}` URL pattern.
3. **Remove API calls**: Remove any HF API calls that are used to download dataset files, as these are no longer needed.
4. **Test and verify**: Test the updated training script to ensure that it can download dataset files from the HF CDN successfully and train the model without any issues.

### Code Snippet
```python
import requests

# Define the dataset repository and file path
repo = "dataset-repo"
file_path = "path/to/file.parquet"

# Download the dataset file from the HF CDN
url = f"https://huggingface.co/datasets/{repo}/resolve/main/{file_path}"
response = requests.get(url)

# Save the dataset file to a local file
with open(file_path, "wb") as f:
    f.write(response.content)
```
This code snippet demonstrates how to download a dataset file from the HF CDN using the `requests` library. The `url` variable is constructed using the dataset repository and file path, and the `response` variable contains the downloaded file contents. The file is then saved to a local file using the `with` statement.

### Benefits
Implementing the HF CDN Bypass for dataset training provides several benefits, including:

* **Bypasses rate limits**: By downloading dataset files from the HF CDN, we can bypass the rate limits imposed by the HF API.
* **Improves training speed**: Downloading dataset files from the HF CDN can be faster than using the HF API, which can improve training speed.
* **Reduces API usage**: By removing HF API calls, we can reduce our API usage and avoid hitting rate limits.
