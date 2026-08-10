from dpmp_gtfs.exceptions import DpmpApiError

# ``client.py`` still imports the pre-migration model names (Task 3 rewrites
# it against the new models). Re-exporting it here would make importing
# ``dpmp_gtfs.api.models`` -- needed by tests/test_models.py -- fail too, so
# the eager import is dropped until Task 3 lands.
__all__ = ["DpmpApiError"]
