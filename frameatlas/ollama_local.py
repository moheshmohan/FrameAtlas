"""Send private inference inputs directly to the installed loopback service."""
from http.client import HTTPConnection, HTTPException
import json

MAX_RESPONSE_BYTES = 2 * 1024 ** 2


def request_json(path, payload=None, *, timeout):
    if path not in ("/api/tags", "/api/chat"):
        raise ValueError("Unsupported local Ollama operation")
    conn = HTTPConnection("127.0.0.1", 11434, timeout=timeout)
    try:
        if payload is None:
            conn.request("GET", path)
        else:
            conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"Local Ollama returned HTTP {response.status}")
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("Local Ollama reply exceeded the response limit")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Local Ollama returned an invalid JSON object")
        return result
    except (OSError, HTTPException, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Local Ollama could not complete the request. Check that it is running on this Mac.") from exc
    finally:
        conn.close()
