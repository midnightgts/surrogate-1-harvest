# surrogate-1 / backend

**Surrogate-1 Backend Improvement**

### **Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted quota and potential downtime.
* The project may not be utilizing the Hugging Face CDN for dataset downloads, which can bypass API rate limits and improve training efficiency.
* The existing implementation may not be handling errors and exceptions properly, leading to potential crashes and data corruption.
* The project may not be taking full advantage of the Hugging Face API, potentially leading to missed opportunities for optimization and improvement.

### **Proposed change**
* Implement a robust Hugging Face API rate limit handling mechanism to prevent dataset training from being blocked.
* Reuse existing Lightning Studio instances efficiently to minimize quota waste and potential downtime.

### **Implementation**
* **Step 1:** Update the `training.py` file to use the Hugging Face CDN for dataset downloads by modifying the `load_dataset` function to use `hf_hub_download` instead of `load_dataset(streaming=True)`.
* **Step 2:** Implement a rate limit handling mechanism using the `requests` library to track API calls and wait for the rate limit to reset before making further API calls.
* **Step 3:** Update the `studio.py` file to reuse existing Lightning Studio instances efficiently by checking for existing running studios and reusing them instead of creating new ones.

**Implementation Code**
```python
# training.py
import os
import requests

def load_dataset(repo, path):
    # Use Hugging Face CDN for dataset downloads
    file_path = hf_hub_download(repo, path)
    return pd.read_parquet(file_path)

def handle_rate_limit(api_calls):
    if api_calls >= 1000:
        # Wait for rate limit to reset
        time.sleep(360)
        api_calls = 0
    return api_calls

# studio.py
import lightning as L

def get_studio(studio_name):
    # Check for existing running studios and reuse them
    for s in L.Teamspace.studios:
        if s.name == studio_name and s.status == 'Running':
            return s
    return L.Studio.create_ok(studio_name)
```

### **Verification**
* Verify that the Hugging Face API rate limit handling mechanism is working correctly by monitoring API calls and ensuring that dataset training is not being blocked.
* Verify that existing Lightning Studio instances are being reused efficiently by checking the studio status and ensuring that new studios are not being created unnecessarily.
* Verify that dataset downloads are being performed using the Hugging Face CDN by checking the file paths and ensuring that they match the expected format.
