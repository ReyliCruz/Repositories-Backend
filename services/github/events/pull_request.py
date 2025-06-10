from datetime import datetime
import json
from dotenv import load_dotenv
import requests
from services.llm.gemini import call_llm
import re
import os

load_dotenv()
GEMINI_KEY_1 = os.getenv("GEMINI_API_KEY_1")
GEMINI_KEY_2 = os.getenv("GEMINI_API_KEY_2")
GITHUB_API = "https://api.github.com"

def generate_prompt(structured_lines: list[dict]) -> str:
    lines_formatted = "\n".join(
        f'Line {l["line"]} ({l["type"]}): {l["code"]}' for l in structured_lines
    )

    return f"""
        You are a senior software engineer reviewing changes made to a code file.

        Below are the modified lines, each with its line number, type (insert, delete, or normal), and the code content:

        {lines_formatted}

        Please analyze these lines and return constructive feedback for any line you consider relevant.

        Return ONLY a JSON array. Each item in the array must have:

        - "type": insert, delete, or normal (depending on the line analyzed)
        - "comment": a short and clear code review message
        - "lineNumber": the number of the line being commented

        ⚠️ DO NOT return the original input or code again.
        ⚠️ DO NOT include any explanation.
        ⚠️ ONLY return a clean JSON array as shown below.

        Example:

        [
        {{
            "type": "insert",
            "comment": "✅ Good use of try/except block.",
            "lineNumber": 3
        }},
        {{
            "type": "delete",
            "comment": "⚠️ This function was removed — was it intentional?",
            "lineNumber": 12
        }}
        ]
        """.strip()

def generate_summary_prompt(repo_name: str, ref_id: str, feedback_by_file: list[dict], diff_lines: int) -> str:
    joined_feedback = "\n".join(
        f"- {f['filePath']}: " + "; ".join(c['comment'] for c in f['comments'])
        for f in feedback_by_file
    )
    return f"""
        You are a senior engineer reviewing pull request `{ref_id}` in the repository `{repo_name}`.
        You will receive several file comments and a count of changed lines.

        Here are the feedback comments across all files:
        {joined_feedback}

        🧠 TASKS:
        1. Write a brief but useful **summary** of the pull request quality. Include insights, best practices, and red flags.
        2. Provide a **code quality rating from 0 to 10** (float allowed), based on clean code, structure, modularity, and naming.
        3. Suggest **at least 3 recommended resources** (videos, articles, docs) for the author to improve.

        ⚠️ FORMAT STRICTLY AS JSON:

        {{
        "summary": "Your summary here...",
        "quality": 8.5,
        "recommended_resources": [
            {{
            "link": "https://example.com",
            "title": "Clean Code Guide"
            }},
            {{
            "link": "https://example.com",
            "title": "FastAPI Best Practices"
            }}
        ]
        }}
        """.strip()

def parse_diff_to_lines(diff_text: str):
    lines = diff_text.splitlines()
    result = []

    current_old = None
    current_new = None

    for line in lines:
        header_match = re.match(r"@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
        if header_match:
            current_old = int(header_match.group(1))
            current_new = int(header_match.group(2))
            continue

        if line.startswith("-"):
            result.append({
                "line": current_old,
                "type": "delete",
                "code": line[1:].strip()
            })
            current_old += 1
        elif line.startswith("+"):
            result.append({
                "line": current_new,
                "type": "insert",
                "code": line[1:].strip()
            })
            current_new += 1
        elif line.startswith(" "):
            result.append({
                "line": current_new,
                "type": "normal",
                "code": line[1:].strip()
            })
            current_old += 1
            current_new += 1
        else:
            continue  # Skips index/hash/file headers

    return result

def clean_llm_response(raw: str) -> str:
    """
    Extrae el contenido JSON de una respuesta en bloque de código markdown.
    """
    match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    return match.group(1).strip() if match else raw.strip()

def fetch_pull_request_files(repo: str, pr_number: int, token: str) -> list[dict]:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    return res.json()

