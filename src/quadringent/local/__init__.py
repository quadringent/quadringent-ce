"""``quadringent local``: one IBM i table copied to Snowflake from one machine with Docker.

The host-side CLI only drives ``docker run`` of the two published images over
one shared data directory. Nothing here imports modules that need ``fcntl``
at import time or any cloud SDK: the host installs only the base package.
"""
