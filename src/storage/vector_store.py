"""Optional Qdrant + embedding endpoint. SQLite remains authoritative.

No credentials or remote endpoints are guessed. Missing configuration uses
local lexical recall, and service failures never prevent a user turn.
"""
import json
import os
import uuid
import urllib.request
import urllib.error


class VectorStore:
    def __init__(self):
        self.url = os.getenv("QDRANT_URL", "").rstrip("/")
        self.embedding_url = os.getenv("EMBEDDING_URL", "")
        self.model = os.getenv("EMBEDDING_MODEL", "")
        self.enabled = bool(self.url and self.embedding_url and self.model)
        self.last_error = None

    def request(self, url, body, method="POST", key=None, qdrant=False):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["api-key" if qdrant else "Authorization"] = key if qdrant else "Bearer " + key
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def embed(self, text):
        return self.request(self.embedding_url, {"model": self.model, "input": text},
                            key=os.getenv("EMBEDDING_API_KEY"))["data"][0]["embedding"]

    def index(self, kind, key, text):
        if not self.enabled:
            return
        try:
            vector = self.embed(text)
            url = self.url + "/collections/" + kind
            token = os.getenv("QDRANT_API_KEY")
            exists = self.request(url + "/exists", None, "GET", token, True)["result"]["exists"]
            if not exists:
                try:
                    self.request(url, {"vectors": {"size": len(vector), "distance": "Cosine"}}, "PUT", token, True)
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
            self.request(url + "/points?wait=true", {"points": [{
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, kind + ":" + key)),
                "vector": vector, "payload": {"source_id": key}}]}, "PUT", token, True)
        except Exception as exc:
            self.last_error = str(exc)

    def search(self, kind, text, limit=5):
        if not self.enabled:
            return []
        try:
            result = self.request(self.url + f"/collections/{kind}/points/query",
                                  {"query": self.embed(text), "limit": limit, "with_payload": True},
                                  key=os.getenv("QDRANT_API_KEY"), qdrant=True)
            return [item["payload"]["source_id"] for item in result["result"]["points"]]
        except Exception as exc:
            self.last_error = str(exc)
            return []
