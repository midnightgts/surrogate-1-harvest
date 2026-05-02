# Costinel / quality

### Highest-Value Incremental Improvement
Based on the provided information, the highest-value incremental improvement that can ship in <2h is to implement a fix for the HF API rate limit issue. This issue is causing problems with dataset training and can be resolved by using the HF CDN to bypass the API rate limit.

### Implementation Plan
1. **Identify the affected code**: Locate the code that is responsible for downloading datasets from the HF API.
2. **Modify the code to use HF CDN**: Update the code to use the HF CDN to download datasets instead of the HF API.
3. **Test the changes**: Verify that the changes fix the rate limit issue and do not introduce any new problems.

### Code Snippets
```python
import requests

# Before
dataset = load_dataset("dataset_name", streaming=True)

# After
url = f"https://huggingface.co/datasets/dataset_name/resolve/main/data.csv"
response = requests.get(url)
data = response.content
```
Note: The above code snippet is a simplified example and may need to be adapted to fit the specific requirements of the Costinel project.

### Benefits
The benefits of this improvement include:

* **Increased throughput**: By bypassing the API rate limit, the system can download datasets more quickly.
* **Improved reliability**: The system will be less prone to errors caused by rate limiting.
* **Better performance**: The system will be able to handle larger datasets and more complex workloads.

### Tags
#huggingface #cdn #rate-limit-bypass #training
