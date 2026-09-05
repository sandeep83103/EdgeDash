# edgedash.sources package
# Import all source modules here so their @register decorators run
# and populate SOURCES before any agent tries to look them up.
from edgedash.sources import arbeitnow  # noqa: F401
from edgedash.sources import apify      # noqa: F401
from edgedash.sources import naukri     # noqa: F401
