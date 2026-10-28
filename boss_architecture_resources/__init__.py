"""Read-only release assets; their authoritative sources remain in config/prompts.

Only the build command copies these two files into the wheel.  No user plans,
provider settings, browser profiles, logs, or collection results belong here.
"""

RESOURCE_SOURCES = {
    "architecture_collection.json": "config/architecture_collection.json",
    "jd_semantic_review.md": "prompts/jd_semantic_review.md",
}
DATA_DIRECTORY = "data"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
PROJECT_NAME = "boss-architecture-collector"
