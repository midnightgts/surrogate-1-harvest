# surrogate-1 / discovery

### Diagnosis
* The project lacks a robust implementation for data ingestion, relying heavily on the Hugging Face API with rate limits, which can block dataset training.
* There is inadequate reuse of existing Lightning Studio instances, leading to wasted quota and potential training interruptions.
* The project does not utilize the HF CDN bypass effectively, resulting in unnecessary API calls and rate limit issues.
* The training pipeline is not optimized for performance, with potential bottlenecks in data loading and processing.
* The project does not have a clear strategy for handling errors and exceptions, particularly in the context of data ingestion and training.

### Proposed change
The proposed change is to implement a more efficient data ingestion pipeline using the HF CDN bypass, and to optimize the training pipeline for performance. Specifically, the change will be made in the `train.py` file, which is responsible for data ingestion and training.

### Implementation
To implement the proposed change, the following steps will be taken:
1. Modify the `train.py` file to use the HF CDN bypass for data ingestion, by downloading dataset files directly from the CDN instead of using the Hugging Face API.
2. Optimize the data loading and processing pipeline to reduce bottlenecks and improve performance.
3. Implement a retry mechanism to handle errors and exceptions during data ingestion and training.
4. Use the `list_repo_tree` API call to pre-list file paths and embed them in the training script, to reduce the number of API calls and avoid rate limit issues.

Example code snippet:
```python
import requests
import json

# Define the dataset repository and file path
repo_id = "dataset/repo"
file_path = "path/to/file"

# Use the HF CDN bypass to download the dataset file
url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{file_path}"
response = requests.get(url)

# Load the dataset file and process it
dataset = json.loads(response.content)

# Train the model using the processed dataset
model = train_model(dataset)
```
### Verification
To verify that the proposed change works, the following steps will be taken:
1. Run the modified `train.py` file and monitor the data ingestion and training pipeline for errors and exceptions.
2. Check the number of API calls made during data ingestion and verify that it is reduced.
3. Verify that the training pipeline is optimized for performance and that bottlenecks are reduced.
4. Test the retry mechanism to ensure that it handles errors and exceptions correctly.

Example verification script:
```python
import logging

# Run the modified train.py file
logging.info("Running train.py file...")
train.py

# Monitor the data ingestion and training pipeline for errors and exceptions
logging.info("Monitoring pipeline for errors and exceptions...")
if errors:
    logging.error("Errors occurred during pipeline execution")
else:
    logging.info("Pipeline executed successfully")
```
