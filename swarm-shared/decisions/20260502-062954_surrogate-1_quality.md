# surrogate-1 / quality

## **Surrogate-1 Quality Improvement**

### **Diagnosis**
* The project lacks a robust implementation for handling Hugging Face API rate limits, which can block dataset training.
* The existing implementation may not be reusing existing Lightning Studio instances efficiently, leading to wasted quota and potential downtime.
* The project may not be utilizing the Hugging Face CDN for rate-limit bypass, resulting in unnecessary API calls and potential rate limit errors.
* The existing implementation may not be handling errors and exceptions properly, leading to unpredictable behavior and potential data corruption.

### **Proposed change**
* Implement a robust Hugging Face API rate limit handling mechanism to prevent dataset training from being blocked by rate limits.

### **Implementation**
* Update the `dataset-mirror` script to use the Hugging Face CDN for rate-limit bypass by downloading files from `https://huggingface.co/datasets/{repo}/resolve/main/{path}` instead of making API calls to `list_repo_files` and `list_repo_tree`.
* Implement a retry mechanism with exponential backoff to handle rate limit errors and exceptions.
* Update the `train.py` script to use the cached file list from the previous step to avoid making unnecessary API calls.

```diff
# dataset-mirror.py
- import hf_api
+ import requests
+ import json

# ...

- files = hf_api.list_repo_files(repo, path)
+ file_list_url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
+ response = requests.get(file_list_url)
+ file_list = json.loads(response.content)

# ...
```

```diff
# train.py
- import hf_api
+ import json

# ...

- file_list = hf_api.list_repo_tree(repo, path)
+ with open("file_list.json", "r") as f:
+     file_list = json.load(f)
```

### **Verification**
* Verify that the `dataset-mirror` script is successfully downloading files from the Hugging Face CDN.
* Verify that the `train.py` script is using the cached file list without making unnecessary API calls.
* Verify that the rate limit handling mechanism is correctly handling rate limit errors and exceptions.
* Verify that dataset training is not being blocked by rate limits.
