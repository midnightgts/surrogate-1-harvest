# surrogate-1 / discovery

**Surrogate-1 Discovery Improvement**

### **Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted quota and potential downtime.
* The project may not be utilizing the Hugging Face CDN to bypass API rate limits during training.
* The existing implementation may not be properly handling errors and retries when encountering API rate limits.

### **Proposed change**
Implement Hugging Face CDN bypass for dataset training by downloading files from the CDN instead of the API.

### **Implementation**
1. Update `train.py` to use `hf_hub_download` to download files from the CDN instead of the API.
2. Modify the `load_dataset` function to use the downloaded files from the CDN instead of loading them from the API.
3. Implement a retry mechanism with exponential backoff to handle API rate limit errors.

**Implementation Code**
```python
import os
import requests
from huggingface_hub import hf_hub_download

# ...

def load_dataset():
    # Download files from CDN
    files = []
    for file in os.listdir("data"):
        files.append(hf_hub_download("https://huggingface.co/datasets/{repo}/resolve/main/{path}", file))

    # Load dataset from downloaded files
    dataset = Dataset.from_pandas(pd.read_csv(files[0]))
    for file in files[1:]:
        dataset = dataset.concatenate(Dataset.from_pandas(pd.read_csv(file)))

    return dataset
```

### **Verification**
1. Run the training script and verify that it completes successfully without encountering API rate limit errors.
2. Check the training logs to ensure that files are being downloaded from the CDN instead of the API.
3. Verify that the dataset is being loaded correctly from the downloaded files.
