"""UW Helper: a local-first, personal commercial loan underwriting workbench.

This package never sends borrower data, documents, prompts or results to
any external service. The only network peer it talks to is a local Ollama
instance on a loopback address.
"""

__version__ = "0.5.0"
ENGINE_VERSION = "commercial-engine/1.0.0"
