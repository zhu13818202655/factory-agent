"""The mounted path of the statistics surface.

One constant, imported by the router that mounts it and by the export service
that hands out download links, so a mount change can never leave a link
pointing at a path nothing serves.
"""

STATISTICS_PREFIX = "/v1/statistics"

__all__ = ["STATISTICS_PREFIX"]
