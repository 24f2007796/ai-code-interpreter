
import csv
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Allow browser requests from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Find the CSV file in the same folder as server.py
CSV_FILE = Path(__file__).parent / "q-fastapi.csv"

# Read the CSV once, preserving its original row order
students = []

with open(CSV_FILE, "r", newline="", encoding="utf-8-sig") as file:
    reader = csv.DictReader(file)

    for row in reader:
        students.append({
            "studentId": int(row["studentId"]),
            "class": row["class"],
        })


@app.get("/api")
def get_students(
    class_: list[str] = Query(default=[], alias="class")
):
    if class_:
        requested_classes = set(class_)
        filtered_students = [
            student for student in students
            if student["class"] in requested_classes
        ]
    else:
        filtered_students = students

    return {"students": filtered_students}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
