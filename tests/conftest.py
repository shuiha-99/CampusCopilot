import os


# Unit tests must not silently call paid or machine-local model services because
# a developer's .env selects real providers. Real dependencies are verified by
# the explicit scripts.check_* integration commands instead.
os.environ["MODEL_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "hash"
