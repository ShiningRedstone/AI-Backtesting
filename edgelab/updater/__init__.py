"""EdgeLab application updater (packaged Windows app; ADR-58, DESKTOP_PACKAGING.md "Updates").

core     - version format, release-manifest schema + strict validation, UpdateError
source   - release metadata/artifact sources (GitHub Releases; local folder for testing) + transports
manager  - check / skip / later / download + SHA-256 verification / staging / hand-off
apply    - the separate helper process that swaps the installed folder and relaunches
service  - the process-wide manager and the /api/version + /api/update/* routes
"""
