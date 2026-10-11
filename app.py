
import os
import sys
import traceback
from io import StringIO
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="AI Code Interpreter")

# Allow requests from browser-based assignment testers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CodeRequest(BaseModel):
    code: str


class CodeResponse(BaseModel):
    error: List[int]
    result: str


class ErrorAnalysis(BaseModel):
    error_lines: List[int] = Field(
        description="1-based line numbers in the submitted Python code "
                    "that caused the error"
    )


def execute_python_code(code: str) -> dict:
    """Execute code and return its output or traceback."""
    old_stdout = sys.stdout
    captured_stdout = StringIO()
    sys.stdout = captured_stdout

    try:
        compiled_code = compile(code, "<string>", "exec")
        exec(compiled_code, {"__name__": "__main__"})

        output = captured_stdout.getvalue()
        return {"success": True, "output": output}

    except Exception:
        error_output = traceback.format_exc()
        return {"success": False, "output": error_output}

    finally:
        sys.stdout = old_stdout


def analyze_error_with_ai(code: str, error_traceback: str) -> List[int]:
    """Ask the LLM for structured error-line analysis."""
    client = OpenAI()

    response = client.chat.completions.create(
        model="google/gemini-2.5-flash-lite",
        messages=[
            {
                "role": "system",
                "content": (
                    "Analyze Python code and its traceback. Identify the "
                    "1-based line number(s) in the submitted code where "
                    "the error occurred. Use the traceback and code, not "
                    "line numbers belonging to library internals. Return "
                    "only valid JSON with an error_lines integer array."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"CODE:\n{code}\n\n"
                    f"TRACEBACK:\n{error_traceback}"
                ),
            },
        ],
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("The AI returned an empty response.")

    analysis = ErrorAnalysis.model_validate_json(content)

    # Only return valid line numbers from the submitted code.
    return sorted(
        set(
            line for line in analysis.error_lines
            if 1 <= line <= len(code.splitlines())
        )
    )


@app.post("/code-interpreter", response_model=CodeResponse)
def code_interpreter(request: CodeRequest):
    execution = execute_python_code(request.code)

    if execution["success"]:
        return CodeResponse(error=[], result=execution["output"])

    # Invoke AI only when execution fails.
    lines = analyze_error_with_ai(
        request.code,
        execution["output"],
    )

    return CodeResponse(
        error=lines,
        result=execution["output"],
    )
