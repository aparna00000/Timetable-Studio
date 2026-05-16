import time, requests, json

# Wait for server to start
time.sleep(2)

data = {
    "entries": [
        {
            "dept": "CS",
            "sem": "First Year",
            "section": "CS1",
            "lab1": {"subject": "Physics Lab", "teacher": "Dr. X"},
            "lab2": {"subject": "Chem Lab", "teacher": "Dr. Y"}
        }
    ]
}

response = requests.post('http://127.0.0.1:5000/generate_bulk_labs', json=data)
print('Status:', response.status_code)
print('Response:', response.text)
