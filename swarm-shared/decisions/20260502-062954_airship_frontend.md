# airship / frontend

**Highest-Value Incremental Improvement for Airship Frontend**

**Task:** Improve user experience by displaying a loading indicator while data is being fetched from the backend.

**Implementation Plan:**

1. **Add a loading indicator component**: Create a new React component `LoadingIndicator` that displays a spinning animation and a message indicating that data is being fetched.
2. **Use the `LoadingIndicator` component in the frontend**: In the relevant components (e.g., `Dashboard`, `Settings`, etc.), wrap the data-fetching code with the `LoadingIndicator` component.
3. **Display the loading indicator when data is being fetched**: Use a state variable to track whether data is being fetched, and display the loading indicator accordingly.

**Code Snippets:**

**LoadingIndicator component:**
```jsx
// components/LoadingIndicator.js
import React from 'react';
import Spinner from 'react-spinner';

const LoadingIndicator = () => {
  return (
    <div className="loading-indicator">
      <Spinner size={24} />
      <p>Loading data...</p>
    </div>
  );
};

export default LoadingIndicator;
```

**Using the `LoadingIndicator` component in the frontend:**
```jsx
// components/Dashboard.js
import React, { useState, useEffect } from 'react';
import LoadingIndicator from './LoadingIndicator';
import axios from 'axios';

const Dashboard = () => {
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    axios.get('/api/data')
      .then(response => {
        setData(response.data);
        setLoading(false);
      })
      .catch(error => {
        console.error(error);
        setLoading(false);
      });
  }, []);

  return (
    <div>
      {loading ? (
        <LoadingIndicator />
      ) : (
        <div>
          {data.map(item => (
            <div key={item.id}>{item.name}</div>
          ))}
        </div>
      )}
    </div>
  );
};
```

**Time estimate:** 30 minutes

**Tags:** #frontend #loading-indicator #user-experience
