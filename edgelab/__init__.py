"""edgelab - research engine for testing whether a trading edge exists."""
# THE application version (MAJOR.MINOR.PATCH). The single source of truth: the frontend build
# (web/build.mjs) refuses to build unless web/package.json carries the same version, the build
# manifest, the Windows executable metadata, the release manifest and the updater all read it from
# here. To release: change it here AND in web/package.json (+ package-lock.json), rebuild. See
# DESKTOP_PACKAGING.md "Releasing".
__version__ = "0.2.0"
