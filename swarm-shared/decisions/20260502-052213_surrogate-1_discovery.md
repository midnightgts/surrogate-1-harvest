# surrogate-1 / discovery

### Diagnosis
* The project lacks a robust implementation for data ingestion, relying heavily on the Hugging Face API with rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential training interruptions.
* The project does not utilize the HF CDN bypass effectively, resulting in unnecessary API calls and rate limit issues.
* The training pipeline is not optimized for performance, with potential bottlenecks in data loading and processing.
* The project does not have a clear strategy for handling errors and exceptions, which can lead to training failures and wasted resources.

### Proposed change
The proposed change is to implement a robust data ingestion pipeline using the HF CDN bypass, with effective reuse of existing Lightning Studio instances and optimized training performance. The scope of this change includes:
* Modifying the `train.py` script to use the HF CDN bypass for data loading
* Implementing a wrapper script to reuse existing Lightning Studio instances and handle errors and exceptions
* Optimizing the training pipeline for performance, including data loading and processing

### Implementation
The implementation involves the following steps:
1. Modify the `train.py` script to use the HF CDN bypass for data loading:
```python
import requests

# Define the HF CDN URL and dataset path
cdn_url = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
dataset_path = "path/to/dataset"

# Download the dataset using the HF CDN bypass
response = requests.get(cdn_url.format(repo="repo", path=dataset_path))
with open("dataset.parquet", "wb") as f:
    f.write(response.content)
```
2. Implement a wrapper script to reuse existing Lightning Studio instances and handle errors and exceptions:
```python
import lightning

# Define the Lightning Studio instance and wrapper script
studio = lightning.Studio()
wrapper_script = "wrapper.sh"

# Reuse existing Lightning Studio instances
for s in studio.teamspace.studios:
    if s.name == "studio-name" and s.status == "Running":
        studio = s
        break

# Handle errors and exceptions
try:
    # Run the training script
    studio.run("train.py")
except Exception as e:
    # Handle errors and exceptions
    print(f"Error: {e}")
```
3. Optimize the training pipeline for performance, including data loading and processing:
```python
import pyarrow

# Define the dataset and data loading parameters
dataset = "dataset.parquet"
batch_size = 32

# Load the dataset using pyarrow
table = pyarrow.parquet.read_table(dataset)

# Process the data in batches
for batch in table.to_batches(batch_size):
    # Process the batch
    process_batch(batch)
```
### Verification
To verify that the proposed change works, the following steps can be taken:
1. Run the modified `train.py` script and verify that the dataset is loaded correctly using the HF CDN bypass.
2. Verify that the wrapper script reuses existing Lightning Studio instances correctly and handles errors and exceptions.
3. Monitor the training pipeline performance and verify that the optimized data loading and processing improve the overall performance.
4. Test the training pipeline with different datasets and parameters to ensure that it works correctly and efficiently.
